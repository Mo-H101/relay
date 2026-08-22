"""
Ollama client.

Ollama is a local, keyless OpenAI-adjacent endpoint. The setup-wizard
surface (model catalog listing via ``/api/tags`` and availability probes
via ``/api/chat``) is synchronous. The runtime surface is fully
implemented (P4.2.1): sync and async single-message chat, sync and async
full OpenAI-payload chat translated to Ollama's native ``/api/chat`` wire
format, streaming variants, model listing, probes, and proxy support.
"""

import json
import time
import uuid
from typing import AsyncIterator, Generator

import httpx

from app.core.config import settings
from app.providers.availability import safe_error_body
from app.providers.base import ModelProbe
from app.providers.exceptions import ProviderHTTPError, ProviderTimeout
from app.providers.openai_compat_client import (
    _retry_after_seconds,
    _stream_error_text,
    _stream_error_text_async,
    bounded_aiter_lines,
    bounded_iter_lines,
    proxy_request_kwargs,
)
from app.services.metrics import relay_metrics


def _ollama_payload(payload: dict) -> dict:
    """
    Translate an OpenAI chat-completions payload to Ollama's /api/chat
    wire body.

    Messages (including tool_calls in assistant history and multimodal
    content parts) are passed through; generation parameters map into
    Ollama's ``options`` block. Parameters Ollama has no equivalent for
    (frequency_penalty, presence_penalty) are dropped, matching the
    single-message ``_chat_payload`` behavior.
    """
    options = {}

    if payload.get("temperature") is not None:
        options["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        options["top_p"] = payload["top_p"]
    if payload.get("max_tokens") is not None:
        options["num_predict"] = payload["max_tokens"]
    if payload.get("seed") is not None:
        options["seed"] = payload["seed"]

    stop = payload.get("stop")
    if stop is not None:
        options["stop"] = [stop] if isinstance(stop, str) else list(stop)

    body = {
        "model": payload["model"],
        "messages": payload.get("messages", []),
        "stream": bool(payload.get("stream", False)),
    }
    if payload.get("tools") is not None:
        body["tools"] = payload["tools"]
    if options:
        body["options"] = options
    return body


def _ollama_tool_calls(message: dict) -> list:
    """
    Translate Ollama tool_calls into OpenAI function-call dicts.

    Ollama's non-streaming responses carry ``arguments`` as an object and
    streaming responses carry it as a JSON string; OpenAI expects the
    string form, so objects are serialized. Missing ids are generated so
    a later tool message can reference them.
    """
    result = []

    for tool_call in message.get("tool_calls") or []:
        fn = tool_call.get("function") or {}
        arguments = fn.get("arguments")

        if isinstance(arguments, dict):
            arguments = json.dumps(arguments)

        result.append({
            "id": tool_call.get("id") or f"call_{uuid.uuid4().hex}",
            "type": "function",
            "function": {
                "name": fn.get("name", ""),
                "arguments": arguments or "",
            },
        })

    return result


def _ollama_usage(data: dict) -> dict:
    """
    Extract token usage from an Ollama body when the counts are present.
    """
    usage = {}

    if data.get("prompt_eval_count") is not None:
        usage["prompt_tokens"] = data["prompt_eval_count"]
    if data.get("eval_count") is not None:
        usage["completion_tokens"] = data["eval_count"]

    if "prompt_tokens" in usage and "completion_tokens" in usage:
        usage["total_tokens"] = (
            usage["prompt_tokens"] + usage["completion_tokens"]
        )

    return usage


def _openai_response(data: dict) -> dict:
    """
    Translate an Ollama /api/chat body into an OpenAI chat response.
    """
    message = data.get("message") or {}
    assistant = {"role": "assistant"}

    if "content" in message:
        assistant["content"] = message.get("content") or ""
    if message.get("tool_calls"):
        assistant["tool_calls"] = _ollama_tool_calls(message)

    response = {
        "choices": [
            {
                "index": 0,
                "message": assistant,
                "finish_reason": "stop",
            }
        ]
    }

    if data.get("model"):
        response["model"] = data["model"]

    usage = _ollama_usage(data)
    if usage:
        response["usage"] = usage

    return response


def _openai_delta_chunk(
    content=None,
    tool_calls=None,
    finish_reason=None,
) -> dict:
    """
    Build one OpenAI streaming chunk carrying a content/tool delta.
    """
    delta = {}

    if content is not None:
        delta["content"] = content
    if tool_calls:
        delta["tool_calls"] = tool_calls

    return {
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ]
    }


def _translate_ollama_line(line: str, provider) -> list:
    """
    Translate one Ollama NDJSON line into OpenAI streaming chunks.

    Returns [] for blank, malformed, or metadata-only lines. Raises
    ProviderHTTPError when Ollama reports an in-stream error so the
    caller can surface it instead of ending the stream silently.
    """
    if not line:
        return []

    try:
        chunk = json.loads(line)
    except json.JSONDecodeError:
        return []

    if "error" in chunk:
        raise ProviderHTTPError(
            0,
            safe_error_body(provider, 0, str(chunk["error"])),
        )

    message = chunk.get("message") or {}
    out: list = []

    content = message.get("content")
    if content:
        out.append(_openai_delta_chunk(content=content))

    if chunk.get("done"):
        tool_calls = (
            _ollama_tool_calls(message) if message.get("tool_calls") else None
        )
        if tool_calls:
            out.append(_openai_delta_chunk(tool_calls=tool_calls))
        out.append(_openai_delta_chunk(finish_reason="stop"))

        usage = _ollama_usage(chunk)
        if usage:
            out.append({"choices": [], "usage": usage})

    return out


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
            response = httpx.get(
                url,
                timeout=30,
                **proxy_request_kwargs(provider, url),
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(
                f"{self.name} model discovery timed out."
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderHTTPError(0, "provider transport failure") from exc

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
            response = httpx.post(
                url,
                json=payload,
                timeout=10,
                **proxy_request_kwargs(provider, url),
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
                "provider transport failure",
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

    def proxy_request_kwargs(self, provider, url: str) -> dict:
        """
        Compute httpx proxy kwargs, matching the OpenAI-compatible client.
        """
        return proxy_request_kwargs(provider, url)

    def connectivity_probe(self, provider) -> tuple:
        """
        Probe provider connectivity using the keyless convention.

        Returns ``(ok, details, latency_ms)`` for the health checker.
        """
        url = f"{provider.base_url.rstrip('/')}{provider.health_endpoint}"
        start = time.perf_counter()

        try:
            response = httpx.get(
                url,
                timeout=10,
                **proxy_request_kwargs(provider, url),
            )
            ok = response.status_code == 200
            details = f"HTTP {response.status_code}"
        except Exception:
            ok = False
            details = "provider unavailable"

        latency = int((time.perf_counter() - start) * 1000)
        return ok, details, latency

    def chat(
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
        Sync chat completion against the local Ollama server.

        Mirrors achat(): returns the assistant message content and raises
        ProviderTimeout or ProviderHTTPError on failure.
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

        headers = {"Content-Type": "application/json"}

        start = time.perf_counter()

        try:
            response = httpx.post(
                url,
                headers=headers,
                json=payload,
                timeout=settings.request_timeout,
                **proxy_request_kwargs(provider, url),
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
            raise ProviderHTTPError(0, "provider transport failure") from exc

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

        return (response.json().get("message") or {}).get("content", "")

    def chat_messages(self, provider, payload: dict) -> dict:
        """
        Send a full OpenAI chat-completions payload against /api/chat.

        The payload is translated to Ollama's native wire format and the
        response is returned as an OpenAI-shaped chat response, so the
        OpenAI-compatible /v1 surface can forward it verbatim. Raises
        ProviderTimeout or ProviderHTTPError on failure.
        """
        url = f"{provider.base_url}/api/chat"
        body = _ollama_payload(payload)

        headers = {"Content-Type": "application/json"}

        start = time.perf_counter()

        try:
            response = httpx.post(
                url,
                headers=headers,
                json=body,
                timeout=settings.request_timeout,
                **proxy_request_kwargs(provider, url),
            )

        except httpx.ReadTimeout as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_messages",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.TimeoutException as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_messages",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.HTTPError as exc:
            relay_metrics.record_provider(
                provider.name,
                "chat_messages",
                0,
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderHTTPError(0, "provider transport failure") from exc

        latency_ms = (time.perf_counter() - start) * 1000

        if response.status_code >= 400:
            relay_metrics.record_provider(
                provider.name,
                "chat_messages",
                response.status_code,
                latency_ms,
            )
            raise ProviderHTTPError(
                response.status_code,
                safe_error_body(
                    provider, response.status_code, response.text
                ),
                retry_after=_retry_after_seconds(response),
            )

        relay_metrics.record_provider(
            provider.name,
            "chat_messages",
            response.status_code,
            latency_ms,
        )

        return _openai_response(response.json())

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

    def chat_stream(
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
    ) -> Generator[str, None, None]:
        """
        Sync streaming chat completion against the local Ollama server.

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

        headers = {"Content-Type": "application/json"}

        try:
            start = time.perf_counter()

            with httpx.stream(
                "POST",
                url,
                headers=headers,
                json=payload,
                timeout=settings.request_timeout,
                **proxy_request_kwargs(provider, url),
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
                            _stream_error_text(response),
                        ),
                        retry_after=_retry_after_seconds(response),
                    )

                for line in bounded_iter_lines(response):
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "error" in chunk:
                        raise ProviderHTTPError(
                            0,
                            safe_error_body(
                                provider, 0, str(chunk["error"])
                            ),
                        )
                    message_chunk = chunk.get("message") or {}
                    content = message_chunk.get("content")
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
            raise ProviderHTTPError(0, "provider transport failure") from exc

    def chat_stream_messages(
        self,
        provider,
        payload: dict,
    ) -> Generator[dict, None, None]:
        """
        Stream a full OpenAI chat-completions payload against /api/chat.

        Yields OpenAI-shaped chunk dicts (content deltas, tool_call
        deltas, finish_reason, and usage) translated from Ollama's NDJSON
        stream. Raises ProviderTimeout or ProviderHTTPError on failure.
        """
        url = f"{provider.base_url}/api/chat"
        body = _ollama_payload(payload)

        headers = {"Content-Type": "application/json"}

        try:
            start = time.perf_counter()

            with httpx.stream(
                "POST",
                url,
                headers=headers,
                json=body,
                timeout=settings.request_timeout,
                **proxy_request_kwargs(provider, url),
            ) as response:
                if response.status_code >= 400:
                    relay_metrics.record_provider(
                        provider.name,
                        "chat_stream_messages",
                        response.status_code,
                        (time.perf_counter() - start) * 1000,
                    )
                    raise ProviderHTTPError(
                        response.status_code,
                        safe_error_body(
                            provider,
                            response.status_code,
                            _stream_error_text(response),
                        ),
                        retry_after=_retry_after_seconds(response),
                    )

                for line in bounded_iter_lines(response):
                    for out in _translate_ollama_line(line, provider):
                        yield out

                relay_metrics.record_provider(
                    provider.name,
                    "chat_stream_messages",
                    200,
                    (time.perf_counter() - start) * 1000,
                )

        except httpx.ReadTimeout as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_stream_messages",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.TimeoutException as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_stream_messages",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.HTTPError as exc:
            relay_metrics.record_provider(
                provider.name,
                "chat_stream_messages",
                0,
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderHTTPError(0, "provider transport failure") from exc

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
            async with httpx.AsyncClient(
                **proxy_request_kwargs(provider, url)
            ) as client:
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
            raise ProviderHTTPError(0, "provider transport failure") from exc

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

            async with httpx.AsyncClient(
                **proxy_request_kwargs(provider, url)
            ) as client:
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

                    async for line in bounded_aiter_lines(response):
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if "error" in chunk:
                            raise ProviderHTTPError(
                                0,
                                safe_error_body(
                                    provider, 0, str(chunk["error"])
                                ),
                            )
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
            raise ProviderHTTPError(0, "provider transport failure") from exc

    async def achat_messages(self, provider, payload: dict) -> dict:
        """
        Async full-payload chat completion against /api/chat.

        Mirrors chat_messages() over httpx.AsyncClient: the payload is
        translated to Ollama's native wire format and the response is
        returned as an OpenAI-shaped chat response.
        """
        url = f"{provider.base_url}/api/chat"
        body = _ollama_payload(payload)

        headers = {"Content-Type": "application/json"}

        start = time.perf_counter()

        try:
            async with httpx.AsyncClient(
                **proxy_request_kwargs(provider, url)
            ) as client:
                response = await client.post(
                    url,
                    headers=headers,
                    json=body,
                    timeout=settings.request_timeout,
                )

        except httpx.ReadTimeout as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_messages",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.TimeoutException as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_messages",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.HTTPError as exc:
            relay_metrics.record_provider(
                provider.name,
                "chat_messages",
                0,
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderHTTPError(0, "provider transport failure") from exc

        latency_ms = (time.perf_counter() - start) * 1000

        if response.status_code >= 400:
            relay_metrics.record_provider(
                provider.name,
                "chat_messages",
                response.status_code,
                latency_ms,
            )
            raise ProviderHTTPError(
                response.status_code,
                safe_error_body(
                    provider, response.status_code, response.text
                ),
                retry_after=_retry_after_seconds(response),
            )

        relay_metrics.record_provider(
            provider.name,
            "chat_messages",
            response.status_code,
            latency_ms,
        )

        return _openai_response(response.json())

    async def achat_stream_messages(
        self,
        provider,
        payload: dict,
    ) -> AsyncIterator[dict]:
        """
        Async full-payload streaming chat completion against /api/chat.

        Yields OpenAI-shaped chunk dicts translated from Ollama's NDJSON
        stream. Raises ProviderTimeout or ProviderHTTPError on failure.
        """
        url = f"{provider.base_url}/api/chat"
        body = _ollama_payload(payload)

        headers = {"Content-Type": "application/json"}

        try:
            start = time.perf_counter()

            async with httpx.AsyncClient(
                **proxy_request_kwargs(provider, url)
            ) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers=headers,
                    json=body,
                    timeout=settings.request_timeout,
                ) as response:
                    if response.status_code >= 400:
                        relay_metrics.record_provider(
                            provider.name,
                            "chat_stream_messages",
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

                    async for line in bounded_aiter_lines(response):
                        for out in _translate_ollama_line(line, provider):
                            yield out

                    relay_metrics.record_provider(
                        provider.name,
                        "chat_stream_messages",
                        200,
                        (time.perf_counter() - start) * 1000,
                    )

        except httpx.ReadTimeout as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_stream_messages",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.TimeoutException as exc:
            relay_metrics.record_provider_timeout(
                provider.name,
                "chat_stream_messages",
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderTimeout(
                f"{self.name} request timed out."
            ) from exc

        except httpx.HTTPError as exc:
            relay_metrics.record_provider(
                provider.name,
                "chat_stream_messages",
                0,
                (time.perf_counter() - start) * 1000,
            )
            raise ProviderHTTPError(0, "provider transport failure") from exc

    async def alist_models(self, provider) -> list:
        """
        Async model catalog listing. Mirrors list_models().
        """
        url = f"{provider.base_url}/api/tags"

        try:
            async with httpx.AsyncClient(
                **proxy_request_kwargs(provider, url)
            ) as client:
                response = await client.get(url, timeout=30)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(
                f"{self.name} model discovery timed out."
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderHTTPError(0, "provider transport failure") from exc

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
            async with httpx.AsyncClient(
                **proxy_request_kwargs(provider, url)
            ) as client:
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
                "provider transport failure",
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
