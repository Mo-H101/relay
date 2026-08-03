"""
Google Gemini (Generative Language API) client.

The setup-wizard surface (model catalog listing, availability probes,
API-key validation) is synchronous. The chat surface is async-first:
``achat`` / ``achat_stream`` land in P3 alongside the async provider
clients. The sync chat methods keep raising ``NotImplementedError``.

The API key is passed as a query parameter (``key=...``) per the Gemini
REST convention. Error bodies are bounded and redacted.
"""

import json
import time
from typing import AsyncIterator
from urllib.parse import quote

import httpx

from app.core.config import settings
from app.providers.availability import safe_error_body
from app.providers.base import ModelProbe
from app.providers.exceptions import ProviderHTTPError, ProviderTimeout
from app.providers.openai_compat_client import (
    _retry_after_seconds,
    _stream_error_text_async,
)
from app.services.metrics import relay_metrics


class GeminiClient:
    """
    Sends Google Generative Language API requests.
    """

    def __init__(self) -> None:
        self.name = "Google Gemini"

    def _model_list_url(self, provider) -> str:
        return f"{provider.base_url}/models?key={provider.api_key}"

    def _probe_url(self, provider, model: str) -> str:
        return (
            f"{provider.base_url}/models/{quote(model, safe='')}"
            f":generateContent?key={provider.api_key}"
        )

    def _stream_url(self, provider, model: str) -> str:
        return (
            f"{provider.base_url}/models/{quote(model, safe='')}"
            f":streamGenerateContent?alt=sse&key={provider.api_key}"
        )

    def list_models(self, provider) -> list:
        """
        Fetch the models available from the Gemini API.
        """
        url = self._model_list_url(provider)

        try:
            response = httpx.get(url, timeout=30)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(
                f"{self.name} model discovery timed out."
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderHTTPError(0, str(exc)) from exc

        if response.status_code >= 400:
            raise ProviderHTTPError(
                response.status_code,
                safe_error_body(
                    provider, response.status_code, response.text
                ),
            )

        models = []

        for entry in response.json().get("models", []):
            name = entry.get("name", "")

            if name.startswith("models/"):
                name = name[len("models/"):]

            if name:
                models.append(name)

        return models

    def key_check(self, provider):
        """
        Return ``(status_code, body_text)`` for a key validation request,
        or ``(None, error)`` when the provider is unreachable.
        """
        url = self._model_list_url(provider)

        try:
            response = httpx.get(url, timeout=30)
        except httpx.HTTPError as exc:
            return None, str(exc)

        return response.status_code, response.text

    def probe_model(self, provider, model: str) -> ModelProbe:
        """
        Probe a model, returning health, latency, and failure detail.
        """
        url = self._probe_url(provider, model)

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": "ping"},
                    ],
                }
            ],
        }

        start = time.perf_counter()

        try:
            response = httpx.post(url, json=payload, timeout=10)
        except httpx.TimeoutException as exc:
            return ModelProbe(
                False,
                int((time.perf_counter() - start) * 1000),
                0,
                "timeout",
            )
        except httpx.HTTPError as exc:
            return ModelProbe(
                False,
                int((time.perf_counter() - start) * 1000),
                0,
                str(exc),
            )

        latency = int((time.perf_counter() - start) * 1000)

        if response.status_code == 200:
            return ModelProbe(True, latency, 200, "")

        return ModelProbe(
            False,
            latency,
            response.status_code,
            safe_error_body(provider, response.status_code, response.text),
        )

    def chat(self, *args, **kwargs):
        raise NotImplementedError(
            "Google Gemini sync chat is not implemented; use achat (P3)."
        )

    def chat_messages(self, *args, **kwargs):
        raise NotImplementedError(
            "Google Gemini sync chat is not implemented; use achat (P3)."
        )

    def chat_stream(self, *args, **kwargs):
        raise NotImplementedError(
            "Google Gemini sync chat is not implemented; use achat_stream (P3)."
        )

    def _chat_payload(self, message: str, **gen_kwargs) -> dict:
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": message},
                    ],
                }
            ],
        }

        config = {}

        if gen_kwargs.get("temperature") is not None:
            config["temperature"] = gen_kwargs["temperature"]
        if gen_kwargs.get("top_p") is not None:
            config["topP"] = gen_kwargs["top_p"]
        if gen_kwargs.get("max_tokens") is not None:
            config["maxOutputTokens"] = gen_kwargs["max_tokens"]

        stop = gen_kwargs.get("stop")

        if stop is not None:
            config["stopSequences"] = (
                [stop] if isinstance(stop, str) else list(stop)
            )

        if config:
            payload["generationConfig"] = config

        return payload

    async def achat(
        self,
        provider,
        model: str,
        message: str,
        temperature=None,
        top_p=None,
        max_tokens=None,
        stop=None,
        frequency_penalty=None,
        presence_penalty=None,
        seed=None,
    ) -> str:
        """
        Async chat completion against the Generative Language API.

        Returns the concatenated text of the first candidate's content
        parts. Raises ProviderTimeout or ProviderHTTPError on failure;
        error bodies are bounded and redacted. Generation parameters not
        supported by the API are ignored.
        """
        url = self._probe_url(provider, model)
        payload = self._chat_payload(
            message,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            stop=stop,
        )

        start = time.perf_counter()

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    json=payload,
                    timeout=settings.request_timeout,
                )

        except httpx.ReadTimeout as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.TimeoutException as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.HTTPError as exc:
            relay_metrics.record_provider(
                provider.name,
                "chat",
                0,
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderHTTPError(0, str(exc)) from exc

        latency_ms = (time.perf_counter() - start) * 1000

        if response.status_code >= 400:
            relay_metrics.record_provider(
                provider.name, "chat", response.status_code, latency_ms
            )
            raise ProviderHTTPError(
                response.status_code,
                safe_error_body(
                    provider, response.status_code, response.text
                ),
                retry_after=_retry_after_seconds(response),
            )

        relay_metrics.record_provider(
            provider.name, "chat", response.status_code, latency_ms
        )

        return self._join_candidates(response.json())

    def _join_candidates(self, data: dict) -> str:
        parts: list = []

        for candidate in data.get("candidates", []):
            content = candidate.get("content") or {}
            for part in content.get("parts", []):
                text = part.get("text")
                if text:
                    parts.append(text)

        return "".join(parts)

    async def achat_stream(
        self,
        provider,
        model: str,
        message: str,
        temperature=None,
        top_p=None,
        max_tokens=None,
        stop=None,
        frequency_penalty=None,
        presence_penalty=None,
        seed=None,
    ) -> AsyncIterator[str]:
        """
        Async streaming chat completion via ``:streamGenerateContent``.

        Yields text from candidate content parts as they arrive. Raises
        ProviderTimeout or ProviderHTTPError on failure.
        """
        url = self._stream_url(provider, model)
        payload = self._chat_payload(
            message,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            stop=stop,
        )

        try:
            start = time.perf_counter()

            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST",
                    url,
                    json=payload,
                    timeout=settings.request_timeout,
                ) as response:
                    if response.status_code >= 400:
                        relay_metrics.record_provider(
                            provider.name,
                            "chat_stream",
                            response.status_code,
                            (time.perf_counter() - start) * 1000,
                        )
                        raise ProviderHTTPError(
                            response.status_code,
                            safe_error_body(
                                provider,
                                response.status_code,
                                await _stream_error_text_async(response),
                            ),
                            retry_after=_retry_after_seconds(response),
                        )

                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        text = self._join_candidates(chunk)
                        if text:
                            yield text

                    relay_metrics.record_provider(
                        provider.name,
                        "chat_stream",
                        200,
                        (time.perf_counter() - start) * 1000,
                    )

        except httpx.ReadTimeout as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_stream",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.TimeoutException as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_stream",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.HTTPError as exc:
            relay_metrics.record_provider(
                provider.name,
                "chat_stream",
                0,
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderHTTPError(0, str(exc)) from exc

    async def alist_models(self, provider) -> list:
        """
        Async model catalog listing. Mirrors list_models().
        """
        url = self._model_list_url(provider)

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=30)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(
                f"{self.name} model discovery timed out."
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderHTTPError(0, str(exc)) from exc

        if response.status_code >= 400:
            raise ProviderHTTPError(
                response.status_code,
                safe_error_body(
                    provider, response.status_code, response.text
                ),
            )

        models = []

        for entry in response.json().get("models", []):
            name = entry.get("name", "")

            if name.startswith("models/"):
                name = name[len("models/"):]

            if name:
                models.append(name)

        return models

    async def aprobe_model(self, provider, model: str) -> ModelProbe:
        """
        Async model probe. Mirrors probe_model().
        """
        url = self._probe_url(provider, model)

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": "ping"},
                    ],
                }
            ],
        }

        start = time.perf_counter()

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, timeout=10)
        except httpx.TimeoutException as exc:
            return ModelProbe(
                False,
                int((time.perf_counter() - start) * 1000),
                0,
                "timeout",
            )
        except httpx.HTTPError as exc:
            return ModelProbe(
                False,
                int((time.perf_counter() - start) * 1000),
                0,
                str(exc),
            )

        latency = int((time.perf_counter() - start) * 1000)

        if response.status_code == 200:
            return ModelProbe(True, latency, 200, "")

        return ModelProbe(
            False,
            latency,
            response.status_code,
            safe_error_body(provider, response.status_code, response.text),
        )
