"""
Anthropic Messages API client.

The setup-wizard surface (model catalog listing, availability probes,
API-key validation) is synchronous. The runtime surface is fully
implemented (P4.2.2): sync and async single-message chat, sync and async
full OpenAI-payload chat translated to Anthropic's native Messages API
wire format, streaming variants, model listing, probes, proxy support,
and a connectivity probe used by the health checker.

The API key is sent via the ``x-api-key`` header plus the required
``anthropic-version`` header. Error bodies are bounded and redacted.
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
    _text_content,
    proxy_request_kwargs,
)
from app.services.metrics import relay_metrics


def _image_source(url: str) -> dict | None:
    """
    Translate an OpenAI image_url data URI into an Anthropic image source.
    """
    if not url:
        return None
    if url.startswith("data:"):
        media_type, _, b64 = url[5:].partition(",")
        return {
            "type": "base64",
            "media_type": media_type.partition(";")[0] or "image/png",
            "data": b64,
        }
    return {"type": "url", "url": url}


def _user_content(content):
    """
    Translate OpenAI user content (string or parts) into Anthropic content.
    """
    if isinstance(content, str):
        return content
    blocks = []
    for part in content or []:
        if isinstance(part, str):
            blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif ptype == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            source = _image_source(url)
            if source:
                blocks.append({"type": "image", "source": source})
    return blocks


def _anthropic_messages(messages: list) -> tuple[str | None, list]:
    """
    Translate an OpenAI messages array into Anthropic messages plus a
    system prompt. System content moves to the top-level ``system`` param,
    assistant ``tool_calls`` become ``tool_use`` blocks, and ``tool``
    results become ``tool_result`` user blocks.
    """
    system = []
    out = []

    for message in messages or []:
        role = message.get("role")
        content = message.get("content")

        if role == "system":
            text = _text_content(content)
            if text:
                system.append(text)
            continue

        if role == "tool":
            out.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": (
                            message.get("tool_call_id")
                            or f"call_{uuid.uuid4().hex}"
                        ),
                        "content": _text_content(content) or "",
                    }
                ],
            })
            continue

        if role == "assistant":
            blocks = []
            text = _text_content(content)
            if text:
                blocks.append({"type": "text", "text": text})
            for tool_call in message.get("tool_calls") or []:
                fn = tool_call.get("function") or {}
                try:
                    arguments = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tool_call.get("id") or f"call_{uuid.uuid4().hex}",
                    "name": fn.get("name", ""),
                    "input": arguments,
                })
            if blocks:
                out.append({"role": "assistant", "content": blocks})
            continue

        converted = _user_content(content)
        if converted:
            out.append({"role": "user", "content": converted})

    return ("\n\n".join(system)) if system else None, out


def _anthropic_tools(tools: list) -> list:
    """
    Translate OpenAI tools into Anthropic tools (``input_schema``).
    """
    result = []
    for tool in tools or []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        entry = {
            "name": fn["name"],
            "input_schema": fn.get("parameters") or {},
        }
        if fn.get("description"):
            entry["description"] = fn["description"]
        result.append(entry)
    return result


def _anthropic_tool_choice(tool_choice):
    """
    Translate an OpenAI tool_choice into Anthropic tool_choice.
    """
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        return {
            "none": {"type": "none"},
            "required": {"type": "any"},
        }.get(tool_choice, {"type": "auto"})
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        fn = tool_choice.get("function") or {}
        name = fn.get("name") if isinstance(fn, dict) else None
        if name:
            return {"type": "tool", "name": name}
    return {"type": "auto"}


def _anthropic_payload(payload: dict) -> dict:
    """
    Translate an OpenAI chat-completions payload into an Anthropic
    Messages API body. Parameters Anthropic has no equivalent for are
    dropped, matching the single-message ``_anthropic_chat_payload``.
    """
    system, messages = _anthropic_messages(payload.get("messages", []))

    body = {
        "model": payload["model"],
        "max_tokens": payload.get("max_tokens") or 512,
        "messages": messages,
        "stream": bool(payload.get("stream", False)),
    }
    if system:
        body["system"] = system
    if payload.get("temperature") is not None:
        body["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        body["top_p"] = payload["top_p"]
    stop = payload.get("stop")
    if stop is not None:
        body["stop_sequences"] = (
            [stop] if isinstance(stop, str) else list(stop)
        )
    tools = _anthropic_tools(payload.get("tools"))
    if tools:
        body["tools"] = tools
    tool_choice = _anthropic_tool_choice(payload.get("tool_choice"))
    if tool_choice:
        body["tool_choice"] = tool_choice
    if payload.get("user"):
        body["metadata"] = {"user_id": payload["user"]}
    return body


def _anthropic_chat_payload(
    model: str,
    message: str,
    stream: bool,
    temperature=None,
    top_p=None,
    max_tokens=None,
    stop=None,
) -> dict:
    """
    Build a single-message Anthropic Messages API body.
    """
    payload = {
        "model": model,
        "max_tokens": 512 if max_tokens is None else max_tokens,
        "messages": [{"role": "user", "content": message}],
        "stream": stream,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if top_p is not None:
        payload["top_p"] = top_p
    if stop is not None:
        payload["stop_sequences"] = (
            [stop] if isinstance(stop, str) else list(stop)
        )
    return payload


def _anthropic_finish_reason(stop_reason) -> str:
    """
    Map an Anthropic stop_reason to the OpenAI finish_reason.
    """
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
    }.get(stop_reason, "stop")


def _anthropic_usage(usage: dict) -> dict:
    """
    Translate Anthropic input/output token counts into OpenAI usage.
    """
    result = {}
    if usage.get("input_tokens") is not None:
        result["prompt_tokens"] = usage["input_tokens"]
    if usage.get("output_tokens") is not None:
        result["completion_tokens"] = usage["output_tokens"]
    if "prompt_tokens" in result and "completion_tokens" in result:
        result["total_tokens"] = (
            result["prompt_tokens"] + result["completion_tokens"]
        )
    return result


def _anthropic_tool_calls(content_blocks: list) -> list:
    """
    Translate Anthropic tool_use content blocks into OpenAI tool_calls.
    """
    result = []
    for block in content_blocks or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        result.append({
            "id": block.get("id") or f"call_{uuid.uuid4().hex}",
            "type": "function",
            "function": {
                "name": block.get("name", ""),
                "arguments": json.dumps(block.get("input") or {}),
            },
        })
    return result


def _openai_response(data: dict) -> dict:
    """
    Translate an Anthropic Messages API body into an OpenAI chat response.
    """
    content_blocks = data.get("content") or []
    text = "".join(
        block.get("text", "")
        for block in content_blocks
        if isinstance(block, dict) and block.get("type") == "text"
    )
    tool_calls = _anthropic_tool_calls(content_blocks)

    message = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls

    response = {
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _anthropic_finish_reason(
                    data.get("stop_reason")
                ),
            }
        ]
    }
    if data.get("model"):
        response["model"] = data["model"]
    usage = _anthropic_usage(data.get("usage") or {})
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


class _AnthropicStreamState:
    """
    Cross-event state for translating an Anthropic SSE stream.
    """

    def __init__(self) -> None:
        self.input_tokens = None
        self.block_to_tool = {}
        self.next_tool_index = 0


def _translate_anthropic_line(line: str, provider, state) -> list:
    """
    Translate one Anthropic SSE data line into OpenAI streaming chunks.

    Returns [] for metadata-only, malformed, or event-framing lines.
    Raises ProviderHTTPError when Anthropic reports an in-stream error.
    """
    if not line.startswith("data: "):
        return []
    try:
        event = json.loads(line[6:])
    except json.JSONDecodeError:
        return []

    etype = event.get("type")
    out: list = []

    if etype == "error":
        raise ProviderHTTPError(
            0,
            safe_error_body(provider, 0, str(event.get("error"))),
        )

    if etype == "message_start":
        usage = (event.get("message") or {}).get("usage") or {}
        if usage.get("input_tokens") is not None:
            state.input_tokens = usage["input_tokens"]
        return []

    if etype == "content_block_start":
        block = event.get("content_block") or {}
        if block.get("type") == "tool_use":
            tool_index = state.next_tool_index
            state.next_tool_index += 1
            state.block_to_tool[event.get("index")] = tool_index
            out.append(_openai_delta_chunk(tool_calls=[
                {
                    "index": tool_index,
                    "id": block.get("id") or f"call_{uuid.uuid4().hex}",
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": "",
                    },
                }
            ]))
        return out

    if etype == "content_block_delta":
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            text = delta.get("text")
            if text:
                out.append(_openai_delta_chunk(content=text))
        elif delta.get("type") == "input_json_delta":
            partial = delta.get("partial_json")
            if partial:
                tool_index = state.block_to_tool.get(event.get("index"))
                out.append(_openai_delta_chunk(tool_calls=[
                    {
                        "index": tool_index,
                        "function": {"arguments": partial},
                    }
                ]))
        return out

    if etype == "message_delta":
        stop_reason = (event.get("delta") or {}).get("stop_reason")
        if stop_reason:
            out.append(_openai_delta_chunk(
                finish_reason=_anthropic_finish_reason(stop_reason)
            ))
        usage = event.get("usage") or {}
        if (
            usage.get("output_tokens") is not None
            and state.input_tokens is not None
        ):
            usage_body = _anthropic_usage({
                "input_tokens": state.input_tokens,
                "output_tokens": usage["output_tokens"],
            })
            if usage_body:
                out.append({"choices": [], "usage": usage_body})
        return out

    return []


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

    def proxy_request_kwargs(self, provider, url: str) -> dict:
        """
        Compute httpx proxy kwargs, matching the OpenAI-compatible client.
        """
        return proxy_request_kwargs(provider, url)

    def connectivity_probe(self, provider) -> tuple:
        """
        Probe provider connectivity using the ``x-api-key`` convention.

        Returns ``(ok, details, latency_ms)`` for the health checker.
        """
        url = f"{provider.base_url.rstrip('/')}{provider.health_endpoint}"
        start = time.perf_counter()

        try:
            response = httpx.get(
                url,
                headers=self._headers(provider),
                timeout=10,
                **proxy_request_kwargs(provider, url),
            )
            ok = response.status_code == 200
            details = f"HTTP {response.status_code}"
        except Exception as exc:
            ok = False
            details = str(exc)

        latency = int((time.perf_counter() - start) * 1000)
        return ok, details, latency

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
                **proxy_request_kwargs(provider, url),
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
                **proxy_request_kwargs(provider, url),
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

    def _assistant_text(self, data: dict) -> str:
        return "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        )

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
        Sync chat completion against the Messages API.

        Mirrors achat(): returns the concatenated assistant text and
        raises ProviderTimeout or ProviderHTTPError on failure. Error
        bodies are bounded and redacted.
        """
        url = f"{provider.base_url}/messages"
        payload = _anthropic_chat_payload(
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
            response = httpx.post(
                url,
                headers=self._headers(provider),
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

        return self._assistant_text(response.json())

    def chat_messages(self, provider, payload: dict) -> dict:
        """
        Send a full OpenAI chat-completions payload against /messages.

        The payload is translated to Anthropic's native wire format and
        the response is returned as an OpenAI-shaped chat response, so the
        OpenAI-compatible /v1 surface can forward it verbatim. Raises
        ProviderTimeout or ProviderHTTPError on failure.
        """
        url = f"{provider.base_url}/messages"
        body = _anthropic_payload(payload)

        start = time.perf_counter()

        try:
            response = httpx.post(
                url,
                headers=self._headers(provider),
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
            raise ProviderHTTPError(0, str(exc)) from exc

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
        Sync streaming chat completion against the Messages API.

        Yields text deltas from ``content_block_delta`` events. Raises
        ProviderTimeout or ProviderHTTPError on failure.
        """
        url = f"{provider.base_url}/messages"
        payload = _anthropic_chat_payload(
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

            with httpx.stream(
                "POST",
                url,
                headers=self._headers(provider),
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

                for line in response.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
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

    def chat_stream_messages(
        self,
        provider,
        payload: dict,
    ) -> Generator[dict, None, None]:
        """
        Stream a full OpenAI chat-completions payload against /messages.

        Yields OpenAI-shaped chunk dicts (content deltas, tool_call
        deltas, finish_reason, and usage) translated from Anthropic's SSE
        stream. Raises ProviderTimeout or ProviderHTTPError on failure.
        """
        url = f"{provider.base_url}/messages"
        body = _anthropic_payload(payload)
        body["stream"] = True

        try:
            start = time.perf_counter()

            with httpx.stream(
                "POST",
                url,
                headers=self._headers(provider),
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

                state = _AnthropicStreamState()
                for line in response.iter_lines():
                    for out in _translate_anthropic_line(line, provider, state):
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
            raise ProviderHTTPError(0, str(exc)) from exc

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

        payload = _anthropic_chat_payload(
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

        return self._assistant_text(response.json())

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

        payload = _anthropic_chat_payload(
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
                        try:
                            event = json.loads(line[6:])
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

    async def achat_messages(self, provider, payload: dict) -> dict:
        """
        Async full-payload chat completion against /messages.

        Mirrors chat_messages() over httpx.AsyncClient: the payload is
        translated to Anthropic's native wire format and the response is
        returned as an OpenAI-shaped chat response.
        """
        url = f"{provider.base_url}/messages"
        body = _anthropic_payload(payload)

        start = time.perf_counter()

        try:
            async with httpx.AsyncClient(
                **proxy_request_kwargs(provider, url)
            ) as client:
                response = await client.post(
                    url,
                    headers=self._headers(provider),
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
            raise ProviderHTTPError(0, str(exc)) from exc

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
        Async full-payload streaming chat completion against /messages.

        Yields OpenAI-shaped chunk dicts translated from Anthropic's SSE
        stream. Raises ProviderTimeout or ProviderHTTPError on failure.
        """
        url = f"{provider.base_url}/messages"
        body = _anthropic_payload(payload)
        body["stream"] = True

        try:
            start = time.perf_counter()

            async with httpx.AsyncClient(
                **proxy_request_kwargs(provider, url)
            ) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers=self._headers(provider),
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

                    state = _AnthropicStreamState()
                    async for line in response.aiter_lines():
                        for out in _translate_anthropic_line(line, provider, state):
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
            raise ProviderHTTPError(0, str(exc)) from exc

    async def alist_models(self, provider) -> list:
        """
        Async model catalog listing. Mirrors list_models().
        """
        url = f"{provider.base_url}/models"

        try:
            async with httpx.AsyncClient(
                **proxy_request_kwargs(provider, url)
            ) as client:
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
            async with httpx.AsyncClient(
                **proxy_request_kwargs(provider, url)
            ) as client:
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
