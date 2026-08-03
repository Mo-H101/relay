"""
Ollama client (setup-capable).

Ollama is a local, keyless OpenAI-adjacent endpoint. Implements only what
the P1 setup wizard needs: model catalog listing (``/api/tags``) and
availability probes (``/api/chat``). Chat methods raise
``NotImplementedError`` until the async-first provider client work in P4.
"""

import time

import httpx

from app.providers.availability import safe_error_body
from app.providers.base import ModelProbe
from app.providers.exceptions import ProviderHTTPError, ProviderTimeout


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
            "Ollama chat lands with the async provider clients in P4."
        )

    def chat_messages(self, *args, **kwargs):
        raise NotImplementedError(
            "Ollama chat lands with the async provider clients in P4."
        )

    def chat_stream(self, *args, **kwargs):
        raise NotImplementedError(
            "Ollama chat lands with the async provider clients in P4."
        )
