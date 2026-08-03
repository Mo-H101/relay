"""
Anthropic Messages API client (setup-capable).

Implements only what the P1 setup wizard needs: model catalog listing,
model availability probes, and API-key validation. Chat methods raise
``NotImplementedError`` until the async-first provider client work in P4.

The API key is sent via the ``x-api-key`` header plus the required
``anthropic-version`` header. Error bodies are bounded and redacted.
"""

import time

import httpx

from app.providers.availability import safe_error_body
from app.providers.base import ModelProbe
from app.providers.exceptions import ProviderHTTPError, ProviderTimeout


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
            "Anthropic chat lands with the async provider clients in P4."
        )

    def chat_messages(self, *args, **kwargs):
        raise NotImplementedError(
            "Anthropic chat lands with the async provider clients in P4."
        )

    def chat_stream(self, *args, **kwargs):
        raise NotImplementedError(
            "Anthropic chat lands with the async provider clients in P4."
        )
