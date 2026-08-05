"""
Google Gemini (Generative Language API) client.

The setup-wizard surface (model catalog listing, availability probes,
API-key validation) is synchronous. The runtime surface is fully
implemented (P4.2.3): sync and async single-message chat, sync and async
full OpenAI-payload chat translated to Gemini's native ``:generateContent``
wire format, streaming variants via ``:streamGenerateContent?alt=sse``,
model listing, probes, proxy support, and a connectivity probe used by the
health checker.

The API key is passed as a query parameter (``key=...``) per the Gemini
REST convention, and model ids are URL-quoted. Error bodies are bounded
and redacted.
"""

import json
import time
import uuid
from typing import AsyncIterator, Generator
from urllib.parse import quote

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


def _image_part(url: str) -> dict | None:
    """
    Translate an OpenAI image_url into a Gemini content part.

    Data URIs become ``inline_data``; http(s) URLs become ``file_data``.
    """
    if not url:
        return None
    if url.startswith("data:"):
        media_type, _, b64 = url[5:].partition(",")
        return {
            "inline_data": {
                "mime_type": media_type.partition(";")[0] or "image/png",
                "data": b64,
            }
        }
    return {
        "file_data": {
            "mime_type": "application/octet-stream",
            "file_uri": url,
        }
    }


def _user_parts(content) -> list:
    """
    Translate OpenAI user content (string or parts) into Gemini parts.
    """
    if isinstance(content, str):
        return [{"text": content}]
    parts = []
    for part in content or []:
        if isinstance(part, str):
            parts.append({"text": part})
            continue
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            parts.append({"text": part.get("text", "")})
        elif ptype == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            image = _image_part(url)
            if image:
                parts.append(image)
    return parts


def _gemini_contents(messages: list) -> tuple[str | None, list]:
    """
    Translate an OpenAI messages array into Gemini ``contents`` plus a
    top-level system instruction.

    System content moves to ``systemInstruction``, assistant ``tool_calls``
    become ``functionCall`` parts in ``model`` role messages, and ``tool``
    results become ``functionResponse`` parts in ``user`` role messages.
    Tool-call ids are tracked so results can reference the right function
    name.
    """
    system = []
    contents = []
    call_id_to_name = {}

    for message in messages or []:
        role = message.get("role")
        content = message.get("content")

        if role == "system":
            text = _text_content(content)
            if text:
                system.append(text)
            continue

        if role == "tool":
            name = call_id_to_name.get(
                message.get("tool_call_id"), f"call_{uuid.uuid4().hex}"
            )
            contents.append({
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": name,
                            "response": {
                                "result": _text_content(content) or "",
                            },
                        }
                    }
                ],
            })
            continue

        if role == "assistant":
            parts = []
            text = _text_content(content)
            if text:
                parts.append({"text": text})
            for tool_call in message.get("tool_calls") or []:
                fn = tool_call.get("function") or {}
                try:
                    arguments = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                call_id = tool_call.get("id") or f"call_{uuid.uuid4().hex}"
                call_id_to_name[call_id] = fn.get("name", "")
                parts.append({
                    "functionCall": {
                        "name": fn.get("name", ""),
                        "args": arguments,
                    }
                })
            if parts:
                contents.append({"role": "model", "parts": parts})
            continue

        parts = _user_parts(content)
        if parts:
            contents.append({"role": "user", "parts": parts})

    return ("\n\n".join(system)) if system else None, contents


def _gemini_tools(tools: list) -> list:
    """
    Translate OpenAI tools into Gemini function declarations.
    """
    declarations = []

    for tool in tools or []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        entry = {
            "name": fn["name"],
            "parameters": fn.get("parameters") or {},
        }
        if fn.get("description"):
            entry["description"] = fn["description"]
        declarations.append(entry)

    if not declarations:
        return []

    return [{"functionDeclarations": declarations}]


def _gemini_tool_choice(tool_choice) -> dict | None:
    """
    Translate an OpenAI tool_choice into Gemini function calling config.
    """
    if tool_choice is None:
        return None

    mode = "AUTO"
    allowed = None

    if isinstance(tool_choice, str):
        mode = {
            "none": "NONE",
            "required": "ANY",
        }.get(tool_choice, "AUTO")
    elif isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        mode = "ANY"
        fn = tool_choice.get("function") or {}
        name = fn.get("name") if isinstance(fn, dict) else None
        if name:
            allowed = [name]

    config = {"mode": mode}
    if allowed:
        config["allowedFunctionNames"] = allowed

    return {"functionCallingConfig": config}


def _gemini_payload(payload: dict) -> dict:
    """
    Translate an OpenAI chat-completions payload into a Gemini
    ``:generateContent`` body. Parameters Gemini has no equivalent for are
    dropped, matching the single-message ``_gemini_chat_payload``.
    """
    system, contents = _gemini_contents(payload.get("messages", []))

    body = {"contents": contents}

    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}

    config = {}

    if payload.get("temperature") is not None:
        config["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        config["topP"] = payload["top_p"]
    if payload.get("max_tokens") is not None:
        config["maxOutputTokens"] = payload["max_tokens"]

    stop = payload.get("stop")
    if stop is not None:
        config["stopSequences"] = (
            [stop] if isinstance(stop, str) else list(stop)
        )

    if payload.get("seed") is not None:
        config["seed"] = payload["seed"]

    if config:
        body["generationConfig"] = config

    tools = _gemini_tools(payload.get("tools"))
    if tools:
        body["tools"] = tools

    tool_choice = _gemini_tool_choice(payload.get("tool_choice"))
    if tool_choice:
        body["toolConfig"] = tool_choice

    return body


def _gemini_chat_payload(
    message: str,
    temperature=None,
    top_p=None,
    max_tokens=None,
    stop=None,
) -> dict:
    """
    Build a single-message Gemini ``:generateContent`` body.
    """
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": message}],
            }
        ]
    }

    config = {}

    if temperature is not None:
        config["temperature"] = temperature
    if top_p is not None:
        config["topP"] = top_p
    if max_tokens is not None:
        config["maxOutputTokens"] = max_tokens

    if stop is not None:
        config["stopSequences"] = (
            [stop] if isinstance(stop, str) else list(stop)
        )

    if config:
        body["generationConfig"] = config

    return body


def _probe_payload() -> dict:
    """
    Minimal Gemini body for a health/availability probe.
    """
    return {
        "contents": [
            {"parts": [{"text": "ping"}]},
        ]
    }


def _gemini_model_ids(data: dict) -> list:
    """
    Extract model ids, stripping the ``models/`` prefix Gemini uses.
    """
    models = []

    for entry in data.get("models", []):
        name = entry.get("name", "")
        if name.startswith("models/"):
            name = name[len("models/"):]
        if name:
            models.append(name)

    return models


def _gemini_finish_reason(finish_reason) -> str:
    """
    Map a Gemini finish reason to the OpenAI finish_reason.
    """
    return {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
        "RECITATION": "content_filter",
        "MALFORMED_FUNCTION_CALL": "content_filter",
        "OTHER": "stop",
    }.get(finish_reason, "stop")


def _gemini_usage(usage_metadata: dict) -> dict:
    """
    Translate Gemini usage metadata into OpenAI usage.
    """
    result = {}

    if usage_metadata.get("promptTokenCount") is not None:
        result["prompt_tokens"] = usage_metadata["promptTokenCount"]
    if usage_metadata.get("candidatesTokenCount") is not None:
        result["completion_tokens"] = usage_metadata["candidatesTokenCount"]

    if "prompt_tokens" in result and "completion_tokens" in result:
        result["total_tokens"] = (
            result["prompt_tokens"] + result["completion_tokens"]
        )

    return result


def _gemini_tool_calls(content_parts: list) -> list:
    """
    Translate Gemini ``functionCall`` parts into OpenAI tool_calls.
    """
    result = []

    for part in content_parts or []:
        if not isinstance(part, dict):
            continue
        fc = part.get("functionCall")
        if not isinstance(fc, dict):
            continue
        result.append({
            "id": f"call_{uuid.uuid4().hex}",
            "type": "function",
            "function": {
                "name": fc.get("name", ""),
                "arguments": json.dumps(fc.get("args") or {}),
            },
        })

    return result


def _join_candidates(data: dict) -> str:
    """
    Concatenate all candidate text parts.
    """
    parts: list = []

    for candidate in data.get("candidates", []):
        content = candidate.get("content") or {}
        for part in content.get("parts", []):
            text = part.get("text") if isinstance(part, dict) else None
            if text:
                parts.append(text)

    return "".join(parts)


def _openai_response(data: dict) -> dict:
    """
    Translate a Gemini ``:generateContent`` body into an OpenAI chat
    response.
    """
    text = _join_candidates(data)

    tool_calls = []
    finish_reason = "stop"

    for candidate in data.get("candidates") or []:
        parts = (candidate.get("content") or {}).get("parts") or []
        tool_calls.extend(_gemini_tool_calls(parts))
        if candidate.get("finishReason"):
            finish_reason = _gemini_finish_reason(candidate["finishReason"])

    message = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls

    response = {
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ]
    }

    if data.get("model"):
        response["model"] = data["model"]

    usage = _gemini_usage(data.get("usageMetadata") or {})
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


class _GeminiStreamState:
    """
    Cross-event state for translating a Gemini SSE stream.
    """

    def __init__(self) -> None:
        self.part_to_tool = {}
        self.next_tool_index = 0
        self.args_emitted = set()
        self.usage_emitted = False


def _translate_gemini_line(line: str, provider, state) -> list:
    """
    Translate one Gemini SSE data line into OpenAI streaming chunks.

    Gemini streams whole ``GenerateContentResponse`` frames (not per-token
    deltas), so text is emitted per frame and function-call arguments are
    emitted once as a complete JSON string. Returns [] for metadata-only,
    malformed, or event-framing lines. Raises ProviderHTTPError when Gemini
    reports an in-stream error.
    """
    if not line.startswith("data: "):
        return []

    try:
        data = json.loads(line[6:])
    except json.JSONDecodeError:
        return []

    if "error" in data:
        raise ProviderHTTPError(
            0,
            safe_error_body(provider, 0, str(data["error"])),
        )

    out: list = []
    finish_reason = None

    for candidate in data.get("candidates") or []:
        parts = (candidate.get("content") or {}).get("parts") or []

        for index, part in enumerate(parts):
            if not isinstance(part, dict):
                continue

            text = part.get("text")
            if text:
                out.append(_openai_delta_chunk(content=text))

            fc = part.get("functionCall")
            if not isinstance(fc, dict):
                continue

            tool_index = state.part_to_tool.get(index)

            if tool_index is None:
                tool_index = state.next_tool_index
                state.next_tool_index += 1
                state.part_to_tool[index] = tool_index
                out.append(_openai_delta_chunk(tool_calls=[
                    {
                        "index": tool_index,
                        "id": f"call_{uuid.uuid4().hex}",
                        "type": "function",
                        "function": {
                            "name": fc.get("name", ""),
                            "arguments": "",
                        },
                    }
                ]))

            if fc.get("args") and index not in state.args_emitted:
                state.args_emitted.add(index)
                out.append(_openai_delta_chunk(tool_calls=[
                    {
                        "index": tool_index,
                        "function": {
                            "arguments": json.dumps(fc["args"]),
                        },
                    }
                ]))

        if candidate.get("finishReason"):
            finish_reason = _gemini_finish_reason(candidate["finishReason"])

    if finish_reason:
        out.append(_openai_delta_chunk(finish_reason=finish_reason))

        usage = _gemini_usage(data.get("usageMetadata") or {})
        if usage and not state.usage_emitted:
            state.usage_emitted = True
            out.append({"choices": [], "usage": usage})

    return out


class GeminiClient:
    """
    Sends Google Generative Language API requests.
    """

    def __init__(self) -> None:
        self.name = "Google Gemini"

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
        }

    def _model_list_url(self, provider) -> str:
        return f"{provider.base_url}/models?key={provider.api_key}"

    def _generate_url(self, provider, model: str) -> str:
        return (
            f"{provider.base_url}/models/{quote(model, safe='')}"
            f":generateContent?key={provider.api_key}"
        )

    def _stream_url(self, provider, model: str) -> str:
        return (
            f"{provider.base_url}/models/{quote(model, safe='')}"
            f":streamGenerateContent?alt=sse&key={provider.api_key}"
        )

    def proxy_request_kwargs(self, provider, url: str) -> dict:
        """
        Compute httpx proxy kwargs, matching the OpenAI-compatible client.
        """
        return proxy_request_kwargs(provider, url)

    def connectivity_probe(self, provider) -> tuple:
        """
        Probe provider connectivity using the ``?key=`` query convention.

        Returns ``(ok, details, latency_ms)`` for the health checker.
        """
        url = (
            f"{provider.base_url.rstrip('/')}{provider.health_endpoint}"
            f"?key={provider.api_key}"
        )
        start = time.perf_counter()

        try:
            response = httpx.get(
                url,
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
        Fetch the models available from the Gemini API.
        """
        url = self._model_list_url(provider)

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
            raise ProviderHTTPError(0, str(exc)) from exc

        if response.status_code >= 400:
            raise ProviderHTTPError(
                response.status_code,
                safe_error_body(
                    provider, response.status_code, response.text
                ),
            )

        return _gemini_model_ids(response.json())

    def key_check(self, provider):
        """
        Return ``(status_code, body_text)`` for a key validation request,
        or ``(None, error)`` when the provider is unreachable.
        """
        url = self._model_list_url(provider)

        try:
            response = httpx.get(
                url,
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
        url = self._generate_url(provider, model)
        payload = _probe_payload()

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
        Sync chat completion against ``:generateContent``.

        Mirrors achat(): returns the concatenated candidate text and raises
        ProviderTimeout or ProviderHTTPError on failure. Error bodies are
        bounded and redacted.
        """
        url = self._generate_url(provider, model)
        payload = _gemini_chat_payload(
            message,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            stop=stop,
        )

        start = time.perf_counter()

        try:
            response = httpx.post(
                url,
                headers=self._headers(),
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

        return _join_candidates(response.json())

    def chat_messages(self, provider, payload: dict) -> dict:
        """
        Send a full OpenAI chat-completions payload against
        ``:generateContent``.

        The payload is translated to Gemini's native wire format and the
        response is returned as an OpenAI-shaped chat response, so the
        OpenAI-compatible /v1 surface can forward it verbatim. Raises
        ProviderTimeout or ProviderHTTPError on failure.
        """
        url = self._generate_url(provider, payload["model"])
        body = _gemini_payload(payload)

        start = time.perf_counter()

        try:
            response = httpx.post(
                url,
                headers=self._headers(),
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
        Sync streaming chat completion via ``:streamGenerateContent``.

        Yields text from candidate content parts as they arrive. Raises
        ProviderTimeout or ProviderHTTPError on failure.
        """
        url = self._stream_url(provider, model)
        payload = _gemini_chat_payload(
            message,
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
                headers=self._headers(),
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
                        chunk = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    text = _join_candidates(chunk)
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
        Stream a full OpenAI chat-completions payload against
        ``:streamGenerateContent``.

        Yields OpenAI-shaped chunk dicts (content deltas, tool_call
        deltas, finish_reason, and usage) translated from Gemini's SSE
        stream. Raises ProviderTimeout or ProviderHTTPError on failure.
        """
        url = self._stream_url(provider, payload["model"])
        body = _gemini_payload(payload)

        try:
            start = time.perf_counter()

            with httpx.stream(
                "POST",
                url,
                headers=self._headers(),
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

                state = _GeminiStreamState()
                for line in response.iter_lines():
                    for out in _translate_gemini_line(line, provider, state):
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
        Async chat completion against the Generative Language API.

        Returns the concatenated text of the first candidate's content
        parts. Raises ProviderTimeout or ProviderHTTPError on failure;
        error bodies are bounded and redacted. Generation parameters not
        supported by the API are ignored.
        """
        url = self._generate_url(provider, model)
        payload = _gemini_chat_payload(
            message,
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
                    headers=self._headers(),
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

        return _join_candidates(response.json())

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
        payload = _gemini_chat_payload(
            message,
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
                    headers=self._headers(),
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
                            chunk = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue
                        text = _join_candidates(chunk)
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
        Async full-payload chat completion against ``:generateContent``.

        Mirrors chat_messages() over httpx.AsyncClient: the payload is
        translated to Gemini's native wire format and the response is
        returned as an OpenAI-shaped chat response.
        """
        url = self._generate_url(provider, payload["model"])
        body = _gemini_payload(payload)

        start = time.perf_counter()

        try:
            async with httpx.AsyncClient(
                **proxy_request_kwargs(provider, url)
            ) as client:
                response = await client.post(
                    url,
                    headers=self._headers(),
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
        Async full-payload streaming chat completion against
        ``:streamGenerateContent``.

        Yields OpenAI-shaped chunk dicts translated from Gemini's SSE
        stream. Raises ProviderTimeout or ProviderHTTPError on failure.
        """
        url = self._stream_url(provider, payload["model"])
        body = _gemini_payload(payload)

        try:
            start = time.perf_counter()

            async with httpx.AsyncClient(
                **proxy_request_kwargs(provider, url)
            ) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers=self._headers(),
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

                    state = _GeminiStreamState()
                    async for line in response.aiter_lines():
                        for out in _translate_gemini_line(line, provider, state):
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
        url = self._model_list_url(provider)

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
            raise ProviderHTTPError(0, str(exc)) from exc

        if response.status_code >= 400:
            raise ProviderHTTPError(
                response.status_code,
                safe_error_body(
                    provider, response.status_code, response.text
                ),
            )

        return _gemini_model_ids(response.json())

    async def aprobe_model(self, provider, model: str) -> ModelProbe:
        """
        Async model probe. Mirrors probe_model().
        """
        url = self._generate_url(provider, model)
        payload = _probe_payload()

        start = time.perf_counter()

        try:
            async with httpx.AsyncClient(
                **proxy_request_kwargs(provider, url)
            ) as client:
                response = await client.post(
                    url, json=payload, timeout=10
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
