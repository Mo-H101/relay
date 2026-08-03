"""
Anthropic Messages API client.

The setup-wizard surface (model catalog listing, availability probes,
API-key validation) is synchronous. The chat surface is async-first:
``achat`` / ``achat_stream`` land in P3 alongside the async provider
clients. The sync chat methods keep raising ``NotImplementedError``.

The API key is sent via the ``x-api-key`` header plus the required
``anthropic-version`` header. Error bodies are bounded and redacted.
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


class AnthropicClient:
    """
    Sends Anthropic Messages API requests.
    """
    def __init__(self) -> None:
        self.name = "Anthropic"

    def _headers(self, provider) -> dict:
        return {
            "x-api-key": provider.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def list_models(self, provider) -> list:
        """
        Fetch the models available from the Anthropic API.
        """
        url = f"{provider.base_url}/models"

        try:
            response = httpx.get(
                url,
                headers=self._headers(provider),
                timeout=30,
            )
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
            model["id"]
            for model in response.json().get("data", [])
        ]

    def key_check(self, provider):
        """
        Return ``(status_code, body_text)`` for a key validation request,
        or ``(None, error)`` when the provider is unreachable.
        """
        url = f"{provider.base_url}/models"

        try:
            response = httpx.get(
                url,
                headers=self._headers(provider),
                timeout=30,
            )
        except httpx.HTTPError as exc:
            return None, str(exc)

        return response.status_code, response.text

    def probe_model(self, provider, model: str) -> ModelProbe:
        """
        Probe a model, returning health, latency, and failure detail.
        """
        url = f"{provider.base_url}/messages"

        payload = {
            "model": model,
            "max_tokens": 1,
            "messages": [
                {
                    "role": "user",
                    "content": "ping",
                }
            ],
        }

        start = time.perf_counter()

        try:
            response = httpx.post(
                url,
                headers=self._headers(provider),
                json=payload,
                timeout=10,
            )
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
            "Anthropic sync chat is not implemented; use achat (P3)."
        )

    def chat_messages(self, *args, **kwargs):
        raise NotImplementedError(
            "Anthropic sync chat is not implemented; use achat (P3)."
        )

    def chat_stream(self, *args, **kwargs):
        raise NotImplementedError(
            "Anthropic sync chat is not implemented; use achat_stream (P3)."
        )

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
        Async chat completion against the Messages API.

        Returns the concatenated assistant text. Raises ProviderTimeout
        or ProviderHTTPError on failure; error bodies are bounded and
        redacted. Generation parameters not supported by the Messages
        API are ignored.
        """
        url = f"{provider.base_url}/messages"

        payload = {
            "model": model,
            "max_tokens": 512 if max_tokens is None else max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": message,
                }
            ],
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        if stop is not None:
            payload["stop_sequences"] = (
                [stop] if isinstance(stop, str) else list(stop)
            )

        start = time.perf_counter()

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    headers=self._headers(provider),
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

        return "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        )

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
        Async streaming chat completion against the Messages API.

        Yields text deltas from ``content_block_delta`` events. Raises
        ProviderTimeout or ProviderHTTPError on failure.
        """
        url = f"{provider.base_url}/messages"

        payload = {
            "model": model,
            "max_tokens": 512 if max_tokens is None else max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": message,
                }
            ],
            "stream": True,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        if stop is not None:
            payload["stop_sequences"] = (
                [stop] if isinstance(stop, str) else list(stop)
            )

        try:
            start = time.perf_counter()

            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST",
                    url,
                    headers=self._headers(provider),
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
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        if event.get("type") != "content_block_delta":
                            continue
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text = delta.get("text")
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
        url = f"{provider.base_url}/models"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers=self._headers(provider),
                    timeout=30,
                )
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
            model["id"]
            for model in response.json().get("data", [])
        ]

    async def aprobe_model(self, provider, model: str) -> ModelProbe:
        """
        Async model probe. Mirrors probe_model().
        """
        url = f"{provider.base_url}/messages"

        payload = {
            "model": model,
            "max_tokens": 1,
            "messages": [
                {
                    "role": "user",
                    "content": "ping",
                }
            ],
        }

        start = time.perf_counter()

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    headers=self._headers(provider),
                    json=payload,
                    timeout=10,
                )
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
