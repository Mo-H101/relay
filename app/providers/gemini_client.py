"""
Google Gemini (Generative Language API) client (setup-capable).

Implements only what the P1 setup wizard needs: model catalog listing,
availability probes, and API-key validation. Chat methods raise
``NotImplementedError`` until the async-first provider client work in P4.

The API key is passed as a query parameter (``key=...``) per the Gemini
REST convention. Error bodies are bounded and redacted.
"""

import time
from urllib.parse import quote

import httpx

from app.providers.availability import safe_error_body
from app.providers.base import ModelProbe
from app.providers.exceptions import ProviderHTTPError, ProviderTimeout


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
            "Google Gemini chat lands with the async provider clients in P4."
        )

    def chat_messages(self, *args, **kwargs):
        raise NotImplementedError(
            "Google Gemini chat lands with the async provider clients in P4."
        )

    def chat_stream(self, *args, **kwargs):
        raise NotImplementedError(
            "Google Gemini chat lands with the async provider clients in P4."
        )
