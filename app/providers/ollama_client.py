"""
Ollama client.

Ollama is a local, keyless OpenAI-adjacent endpoint. The setup-wizard
surface (model catalog listing via ``/api/tags`` and availability probes
via ``/api/chat``) is synchronous. The chat surface is async-first:
``achat`` / ``achat_stream`` land in P3 alongside the async provider
clients. The sync chat methods keep raising ``NotImplementedError``.
"""

import json
import time
from typing import AsyncIterator

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


class OllamaClient:
    """
    Sends Ollama API requests.
    """

    def __init__(self) -> None:
        self.name = "Ollama"

    def list_models(self, provider) -> list:
        """
        Fetch the models available from the local Ollama server.
        """
        url = f"{provider.base_url}/api/tags"

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

        return [
            entry.get("name", "")
            for entry in response.json().get("models", [])
            if entry.get("name")
        ]

    def key_check(self, provider):
        """
        Ollama is keyless; there is nothing to validate.
        """
        return None, "no api key required"

    def probe_model(self, provider, model: str) -> ModelProbe:
        """
        Probe a model, returning health, latency, and failure detail.
        """
        url = f"{provider.base_url}/api/chat"

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": "ping",
                }
            ],
            "stream": False,
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
            "Ollama sync chat is not implemented; use achat (P3)."
        )

    def chat_messages(self, *args, **kwargs):
        raise NotImplementedError(
            "Ollama sync chat is not implemented; use achat (P3)."
        )

    def chat_stream(self, *args, **kwargs):
        raise NotImplementedError(
            "Ollama sync chat is not implemented; use achat_stream (P3)."
        )

    def _chat_payload(self, model: str, message: str, stream: bool, **gen_kwargs) -> dict:
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": message,
                }
            ],
            "stream": stream,
        }

        options = {}

        if gen_kwargs.get("temperature") is not None:
            options["temperature"] = gen_kwargs["temperature"]
        if gen_kwargs.get("top_p") is not None:
            options["top_p"] = gen_kwargs["top_p"]
        if gen_kwargs.get("max_tokens") is not None:
            options["num_predict"] = gen_kwargs["max_tokens"]

        stop = gen_kwargs.get("stop")

        if stop is not None:
            options["stop"] = [stop] if isinstance(stop, str) else list(stop)

        if options:
            payload["options"] = options

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
        Async chat completion against the local Ollama server.

        Returns the assistant message content. Raises ProviderTimeout or
        ProviderHTTPError on failure; error bodies are bounded and
        redacted. Generation parameters not supported by Ollama are
        ignored.
        """
        url = f"{provider.base_url}/api/chat"
        payload = self._chat_payload(
            model,
            message,
            stream=False,
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

        data = response.json()

        return (data.get("message") or {}).get("content", "")

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
        Async streaming chat completion against the local Ollama server.

        Yields assistant content deltas from the newline-delimited JSON
        stream. Raises ProviderTimeout or ProviderHTTPError on failure.
        """
        url = f"{provider.base_url}/api/chat"
        payload = self._chat_payload(
            model,
            message,
            stream=True,
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
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        message = chunk.get("message") or {}
                        content = message.get("content")
                        if content:
                            yield content

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
        url = f"{provider.base_url}/api/tags"

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

        return [
            entry.get("name", "")
            for entry in response.json().get("models", [])
            if entry.get("name")
        ]

    async def aprobe_model(self, provider, model: str) -> ModelProbe:
        """
        Async model probe. Mirrors probe_model().
        """
        url = f"{provider.base_url}/api/chat"

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": "ping",
                }
            ],
            "stream": False,
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
