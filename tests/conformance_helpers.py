"""
Shared in-process infrastructure for OpenAI-wire conformance tests.

MockOpenAIProvider is a threaded HTTP server that mimics the OpenAI
Chat Completions REST surface for Relay's provider clients: it records
the exact request body it receives and serves scripted JSON, SSE, and
error responses. It binds to an ephemeral loopback port and never
touches the network outside the test process.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

import httpx

from app.providers.base import Provider
from app.providers.registry import PROVIDER_REGISTRY, RUNTIME_READY

DEFAULT_MODEL = "gpt-test"


class MockOpenAIProvider:
    """
    Scriptable OpenAI-compatible /chat/completions server.

    Usage:
        mock = MockOpenAIProvider()
        mock.script(json_body={...})
        mock.script(stream=[...])
        mock.script(error=500, body={...})
        base_url = mock.start()
        ...
        mock.stop()
        assert mock.requests == [...]
    """

    def __init__(self) -> None:
        self.requests: List[dict] = []
        self._script: List[dict] = []
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- scripting ----------------------------------------------------

    def script(self, **spec: Any) -> "MockOpenAIProvider":
        """
        Append one response to the script queue.

        Specs:
            json_body=dict, status=int       -> JSON response
            raw_body=str|bytes, status=int   -> raw (possibly malformed) body
            stream=list[dict]                -> SSE chunk stream + [DONE]
            stream_then_hang=list[dict]      -> SSE chunks, then hold the
                                               connection open (no [DONE]) so a
                                               client read times out mid-stream
            error=int (status), body=dict    -> JSON error response

        Modifiers:
            delay=seconds                    -> sleep before responding (used to
                                               trip the relay's read timeout)
            headers=dict                     -> extra response headers (e.g.
                                               Retry-After) on the response
        """
        if "json_body" in spec:
            entry = {
                "type": "json",
                "status": spec.get("status", 200),
                "body": spec["json_body"],
            }
        elif "raw_body" in spec:
            entry = {
                "type": "raw",
                "status": spec.get("status", 200),
                "body": spec["raw_body"],
            }
        elif "stream" in spec:
            entry = {
                "type": "stream",
                "chunks": spec["stream"],
                "hang_after": spec.get("hang_after", 0),
            }
        elif "stream_then_hang" in spec:
            entry = {
                "type": "stream_then_hang",
                "chunks": spec["stream_then_hang"],
                "hang_after": spec.get("hang_after", 60),
            }
        elif "error" in spec:
            entry = {
                "type": "error",
                "status": spec["error"],
                "body": spec.get(
                    "body",
                    {
                        "error": {
                            "message": "provider error",
                            "type": "server_error",
                            "code": "server_error",
                        }
                    },
                ),
            }
        else:
            raise ValueError(
                "script() needs json_body=, raw_body=, stream=, "
                "stream_then_hang=, or error="
            )

        if "delay" in spec:
            entry["delay"] = float(spec["delay"])

        if "headers" in spec:
            entry["headers"] = dict(spec["headers"])

        self._script.append(entry)
        return self

    # -- lifecycle ----------------------------------------------------

    def start(self) -> str:
        if self._httpd is not None:
            return self.base_url
        self._httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), self._make_handler()
        )
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True
        )
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    @property
    def base_url(self) -> str:
        if self._httpd is None:
            raise RuntimeError("MockOpenAIProvider not started")
        host, port = self._httpd.server_address
        return f"http://{host}:{port}"

    def reset(self) -> None:
        with self._lock:
            self.requests.clear()
            self._script.clear()

    # -- internals ----------------------------------------------------

    def _make_handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                owner._handle(self)

            def do_GET(self) -> None:
                owner._handle_get(self)

            def log_message(self, *args) -> None:
                pass

        return Handler

    def _record(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length).decode("utf-8", "replace")
        body: Any = None
        if raw.strip():
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
        record = {
            "path": handler.path,
            "headers": {
                key.lower(): value for key, value in handler.headers.items()
            },
            "raw": raw,
            "body": body,
        }
        with self._lock:
            self.requests.append(record)

    def _next(self) -> Dict[str, Any] | None:
        with self._lock:
            if not self._script:
                return None
            return self._script.pop(0)

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        self._record(handler)
        spec = self._next() or {
            "type": "error",
            "status": 500,
            "body": {
                "error": {
                    "message": "no scripted response",
                    "type": "server_error",
                    "code": "server_error",
                }
            },
        }
        try:
            if spec.get("delay"):
                time.sleep(spec["delay"])

            headers = spec.get("headers")

            if spec["type"] == "json":
                self._send_json(
                    handler, spec.get("status", 200), spec["body"], headers
                )
            elif spec["type"] == "raw":
                self._send_raw(
                    handler, spec.get("status", 200), spec["body"], headers
                )
            elif spec["type"] == "error":
                self._send_json(
                    handler, spec["status"], spec["body"], headers
                )
            elif spec["type"] == "stream":
                self._send_stream(
                    handler, spec["chunks"], headers, spec.get("hang_after", 0)
                )
            elif spec["type"] == "stream_then_hang":
                self._send_stream(
                    handler, spec["chunks"], headers, spec.get("hang_after", 60)
                )
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        self._record(handler)
        self._send_json(handler, 200, {"object": "list", "data": []})

    @staticmethod
    def _send_headers(
        handler: BaseHTTPRequestHandler,
        status: int,
        content_type: str,
        content_length: int,
        extra: Dict[str, Any] | None = None,
    ) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(content_length))
        for key, value in (extra or {}).items():
            handler.send_header(key, str(value))
        handler.end_headers()

    @classmethod
    def _send_json(
        cls,
        handler: BaseHTTPRequestHandler,
        status: int,
        body: Dict[str, Any],
        headers: Dict[str, Any] | None = None,
    ) -> None:
        data = json.dumps(body).encode("utf-8")
        cls._send_headers(handler, status, "application/json", len(data), headers)
        handler.wfile.write(data)

    @classmethod
    def _send_raw(
        cls,
        handler: BaseHTTPRequestHandler,
        status: int,
        body: Any,
        headers: Dict[str, Any] | None = None,
    ) -> None:
        if isinstance(body, str):
            data = body.encode("utf-8")
        elif isinstance(body, bytes):
            data = body
        else:
            data = json.dumps(body).encode("utf-8")
        cls._send_headers(handler, status, "application/json", len(data), headers)
        handler.wfile.write(data)

    @staticmethod
    def _send_stream(
        handler: BaseHTTPRequestHandler,
        chunks: List[dict],
        headers: Dict[str, Any] | None = None,
        hang_after: float = 0,
    ) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Connection", "close")
        handler.send_header("Cache-Control", "no-cache")
        for key, value in (headers or {}).items():
            handler.send_header(key, str(value))
        handler.end_headers()
        for chunk in chunks:
            handler.wfile.write(
                f"data: {json.dumps(chunk)}\n\n".encode("utf-8")
            )
            handler.wfile.flush()

        if hang_after:
            # Hold the connection open past the client's read timeout so
            # the client raises a mid-stream read timeout. Waking up to
            # write the terminal chunk fails once the client disconnects,
            # which is expected and swallowed by the caller.
            time.sleep(hang_after)

        try:
            handler.wfile.write(b"data: [DONE]\n\n")
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def parse_sse(text: str) -> List[Any]:
    """
    Split a text/event-stream body into parsed events, keeping the
    literal "[DONE]" marker as a string so terminal ordering can be
    asserted.
    """
    events: List[Any] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if payload == "[DONE]":
            events.append("[DONE]")
        else:
            events.append(json.loads(payload))
    return events


def make_provider(
    name: str = "OpenAI",
    base_url: str = "",
    models: List[str] | None = None,
    api_key: str = "test-key",
) -> Provider:
    """
    Build a Provider pointing at a mock OpenAI-compatible server.
    proxy="" explicitly bypasses any proxy configuration so the test
    always talks to the loopback mock.
    """
    return Provider(
        name=name,
        base_url=base_url,
        api_key=api_key,
        enabled=True,
        priority=1,
        proxy="",
        models=list(models or [DEFAULT_MODEL]),
    )


# ---------------------------------------------------------------------------
# Provider conformance suite (P4.3.4): capability matrix and wire fixtures.
#
# The conformance suite is driven by the RUNTIME_READY registry plus this
# capability matrix. Tests never branch per provider; the matrix (with the
# wire family as the only secondary axis) selects behavior.
# ---------------------------------------------------------------------------

OPENAI_WIRE = "openai"
OLLAMA_WIRE = "ollama"
ANTHROPIC_WIRE = "anthropic"
GEMINI_WIRE = "gemini"

# Capability matrix, keyed by registry provider id.
#
#   wire                 : wire family implementing the surface
#   auth                 : credential convention (bearer | x-api-key |
#                          x-goog-api-key | none)
#   tools                : tool-call round-trip supported on chat/chat_messages
#   stream_usage         : streamed chunks carry a terminal usage chunk
#   check_model          : client exposes the check_model() shortcut
#   gen_params           : single-message chat() forwards temperature/top_p/max_tokens/stop
#   discovery_normalize  : list_models() normalizes wire ids (strips prefixes)
#   tool_finish_reason   : finish_reason on a tool-call response
#   chat_forwarded_verbatim : chat_messages() forwards the payload verbatim
#   discovery_endpoint   : wire path used by list_models()/connectivity_probe()
#   retry_after          : errors honor the Retry-After response header
PROVIDER_CAPABILITIES: Dict[str, Dict[str, Any]] = {
    "nvidia": {
        "wire": OPENAI_WIRE,
        "auth": "bearer",
        "tools": True,
        "stream_usage": True,
        "check_model": True,
        "gen_params": True,
        "discovery_normalize": False,
        "tool_finish_reason": "tool_calls",
        "chat_forwarded_verbatim": True,
        "discovery_endpoint": "/models",
        "retry_after": True,
    },
    "openai": {
        "wire": OPENAI_WIRE,
        "auth": "bearer",
        "tools": True,
        "stream_usage": True,
        "check_model": True,
        "gen_params": True,
        "discovery_normalize": False,
        "tool_finish_reason": "tool_calls",
        "chat_forwarded_verbatim": True,
        "discovery_endpoint": "/models",
        "retry_after": True,
    },
    "lmstudio": {
        "wire": OPENAI_WIRE,
        "auth": "bearer",
        "tools": True,
        "stream_usage": True,
        "check_model": True,
        "gen_params": True,
        "discovery_normalize": False,
        "tool_finish_reason": "tool_calls",
        "chat_forwarded_verbatim": True,
        "discovery_endpoint": "/models",
        "retry_after": True,
    },
    "ollama": {
        "wire": OLLAMA_WIRE,
        "auth": "none",
        "tools": True,
        "stream_usage": True,
        "check_model": False,
        "gen_params": False,
        "discovery_normalize": False,
        "tool_finish_reason": "stop",
        "chat_forwarded_verbatim": False,
        "discovery_endpoint": "/api/tags",
        "retry_after": True,
    },
    "anthropic": {
        "wire": ANTHROPIC_WIRE,
        "auth": "x-api-key",
        "tools": True,
        "stream_usage": True,
        "check_model": False,
        "gen_params": False,
        "discovery_normalize": False,
        "tool_finish_reason": "tool_calls",
        "chat_forwarded_verbatim": False,
        "discovery_endpoint": "/models",
        "retry_after": True,
    },
    "gemini": {
        "wire": GEMINI_WIRE,
        "auth": "x-goog-api-key",
        "tools": True,
        "stream_usage": True,
        "check_model": False,
        "gen_params": False,
        "discovery_normalize": True,
        "tool_finish_reason": "stop",
        "chat_forwarded_verbatim": False,
        "discovery_endpoint": "/models",
        "retry_after": True,
    },
}


def cap(provider_id: str, key: str) -> Any:
    """Read one capability value for a provider id."""
    return PROVIDER_CAPABILITIES[provider_id][key]


def wire_of(provider_id: str) -> str:
    """Return the wire family implementing a provider's surface."""
    return PROVIDER_CAPABILITIES[provider_id]["wire"]


def build_provider_instance(provider_id: str, api_key: str = "sk-test") -> Provider:
    """Build a runtime Provider from the registry definition."""
    definition = PROVIDER_REGISTRY[provider_id]
    return definition.build_provider(
        api_key=api_key,
        base_url=definition.base_url_default,
    )


class MockResponse:
    """
    Minimal httpx.Response stand-in for non-streaming requests.
    """

    def __init__(
        self,
        status_code: int = 200,
        json_data: Any = None,
        text: str = "",
        headers: Dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json = json_data
        self.text = text if text else (json.dumps(json_data) if json_data is not None else "")
        self.headers = headers or {}
        self.url = ""

    def json(self) -> Any:
        return self._json

    def read(self) -> str:
        return self.text


class MockStreamResponse:
    """
    httpx.stream response stand-in usable with both ``with`` and
    ``async with`` so one fixture serves sync and async stream surfaces.
    """

    def __init__(
        self,
        lines: List[str],
        status_code: int = 200,
        text: str = "",
        headers: Dict[str, Any] | None = None,
    ) -> None:
        self.lines = lines
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def __enter__(self) -> "MockStreamResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    async def __aenter__(self) -> "MockStreamResponse":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def iter_lines(self):
        yield from self.lines

    async def aiter_lines(self):
        for line in self.lines:
            yield line

    def read(self) -> str:
        return self.text

    async def aread(self) -> str:
        return self.text


def json_response(
    body: Any,
    status: int = 200,
    headers: Dict[str, Any] | None = None,
) -> MockResponse:
    """Wrap a dict/JSON body as a mock HTTP response."""
    return MockResponse(
        status_code=status,
        json_data=body,
        text=json.dumps(body),
        headers=headers,
    )


def sse(line_body: str) -> str:
    """Prefix a JSON payload as an SSE data line."""
    return f"data: {line_body}"


def error_body(provider_id: str, status: int, message: str) -> dict:
    """Build a wire-shaped error body for a provider."""
    wire = wire_of(provider_id)
    if wire == OPENAI_WIRE:
        return {"error": {"message": message, "type": "rate_limit_error"}}
    if wire == OLLAMA_WIRE:
        return {"error": message}
    if wire == ANTHROPIC_WIRE:
        return {
            "type": "error",
            "error": {"type": "rate_limit_error", "message": message},
        }
    return {
        "error": {
            "code": status,
            "message": message,
            "status": "RESOURCE_EXHAUSTED",
        }
    }


def error_response(
    provider_id: str,
    status: int = 429,
    message: str = "rate limited",
    retry_after: str = "3",
) -> MockResponse:
    """Build a mock error response carrying Retry-After."""
    body = error_body(provider_id, status, message)
    headers = {"Content-Type": "application/json"}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return json_response(body, status=status, headers=headers)


def chat_body(
    provider_id: str,
    *,
    text: str = "Hello from the relay!",
    tool_calls: bool = False,
) -> dict:
    """Build a successful non-stream chat response body for a provider."""
    wire = wire_of(provider_id)

    if wire == OLLAMA_WIRE:
        message = {"role": "assistant", "content": text}
        if tool_calls:
            message["tool_calls"] = [
                {
                    "function": {
                        "name": "get_weather",
                        "arguments": {"city": "Paris"},
                    }
                }
            ]
        body = {
            "model": DEFAULT_MODEL,
            "message": message,
            "done": True,
            "prompt_eval_count": 7,
            "eval_count": 9,
        }
    elif wire == ANTHROPIC_WIRE:
        if tool_calls:
            content = [
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "get_weather",
                    "input": {"city": "Paris"},
                }
            ]
            stop_reason = "tool_use"
        else:
            content = [{"type": "text", "text": text}]
            stop_reason = "end_turn"
        body = {
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "content": content,
            "model": DEFAULT_MODEL,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 7, "output_tokens": 9},
        }
    elif wire == GEMINI_WIRE:
        parts = [{"text": text}]
        if tool_calls:
            parts = [{"functionCall": {"name": "get_weather", "args": {"city": "Paris"}}}]
        body = {
            "candidates": [
                {
                    "content": {"parts": parts, "role": "model"},
                    "finishReason": "STOP",
                    "index": 0,
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 7,
                "candidatesTokenCount": 9,
                "totalTokenCount": 16,
            },
        }
    else:
        message = {"role": "assistant", "content": text}
        finish_reason = "stop"
        if tool_calls:
            message["tool_calls"] = [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": json.dumps({"city": "Paris"}),
                    },
                }
            ]
            finish_reason = "tool_calls"
        body = {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1700000000,
            "model": DEFAULT_MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": 7,
                "completion_tokens": 9,
                "total_tokens": 16,
            },
        }

    return body


def chat_stream_lines(
    provider_id: str,
    *,
    chunks: tuple = ("Hello ", "world!"),
    tool_calls: bool = False,
) -> List[str]:
    """
    Build the raw stream lines a chat-stream surface expects for a provider.

    OpenAI/Anthropic/Gemini frames use SSE ``data: `` lines; Ollama uses
    plain NDJSON lines.
    """
    wire = wire_of(provider_id)

    if wire == OLLAMA_WIRE:
        lines = [
            json.dumps(
                {
                    "model": DEFAULT_MODEL,
                    "message": {"role": "assistant", "content": chunk},
                    "done": False,
                }
            )
            for chunk in chunks
        ]
        done_message = {"role": "assistant", "content": ""}
        if tool_calls:
            done_message["tool_calls"] = [
                {
                    "function": {
                        "name": "get_weather",
                        "arguments": {"city": "Paris"},
                    }
                }
            ]
        lines.append(
            json.dumps(
                {
                    "model": DEFAULT_MODEL,
                    "message": done_message,
                    "done": True,
                    "prompt_eval_count": 7,
                    "eval_count": 9,
                }
            )
        )
        return lines

    if wire == ANTHROPIC_WIRE:
        lines = [
            sse(
                json.dumps(
                    {
                        "type": "message_start",
                        "message": {
                            "role": "assistant",
                            "usage": {"input_tokens": 7, "output_tokens": 0},
                        },
                    }
                )
            )
        ]
        if tool_calls:
            lines.append(
                sse(
                    json.dumps(
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {
                                "type": "tool_use",
                                "id": "toolu_01",
                                "name": "get_weather",
                                "input": {},
                            },
                        }
                    )
                )
            )
            lines.append(
                sse(
                    json.dumps(
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": json.dumps({"city": "Paris"}),
                            },
                        }
                    )
                )
            )
            lines.append(sse(json.dumps({"type": "content_block_stop", "index": 0})))
        else:
            for chunk in chunks:
                lines.append(
                    sse(
                        json.dumps(
                            {
                                "type": "content_block_delta",
                                "index": 0,
                                "delta": {"type": "text_delta", "text": chunk},
                            }
                        )
                    )
                )
        lines.append(
            sse(
                json.dumps(
                    {
                        "type": "message_delta",
                        "delta": {
                            "stop_reason": "tool_use" if tool_calls else "end_turn",
                            "stop_sequence": None,
                        },
                        "usage": {"output_tokens": 9},
                    }
                )
            )
        )
        lines.append(sse(json.dumps({"type": "message_stop"})))
        return lines

    if wire == GEMINI_WIRE:
        text = "".join(chunks)
        parts = [{"text": text}] if text else []
        if tool_calls:
            parts = [{"functionCall": {"name": "get_weather", "args": {"city": "Paris"}}}]
        frame = {
            "candidates": [
                {
                    "content": {"parts": parts, "role": "model"},
                    "finishReason": "STOP",
                    "index": 0,
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 7,
                "candidatesTokenCount": 9,
                "totalTokenCount": 16,
            },
        }
        return [sse(json.dumps(frame))]

    # OpenAI wire: SSE chunks plus a terminal finish/usage chunk.
    lines = []
    for chunk in chunks:
        lines.append(
            sse(
                json.dumps(
                    {
                        "id": "chatcmpl-1",
                        "object": "chat.completion.chunk",
                        "created": 1700000000,
                        "model": DEFAULT_MODEL,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": chunk},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
            )
        )
    if tool_calls:
        lines.append(
            sse(
                json.dumps(
                    {
                        "id": "chatcmpl-1",
                        "object": "chat.completion.chunk",
                        "created": 1700000000,
                        "model": DEFAULT_MODEL,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call_1",
                                            "type": "function",
                                            "function": {
                                                "name": "get_weather",
                                                "arguments": json.dumps(
                                                    {"city": "Paris"}
                                                ),
                                            },
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                )
            )
        )
    lines.append(
        sse(
            json.dumps(
                {
                    "id": "chatcmpl-1",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": DEFAULT_MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": (
                                "tool_calls" if tool_calls else "stop"
                            ),
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 7,
                        "completion_tokens": 9,
                        "total_tokens": 16,
                    },
                }
            )
        )
    )
    # OpenAI-compatible providers are required to terminate a stream with a
    # literal [DONE] marker (see E1 truncation detection in
    # openai_compat_client). Emitting it keeps this mock a well-behaved
    # provider; a stream that omits it is a genuine truncation.
    lines.append("data: [DONE]")
    return lines


def models_body(provider_id: str) -> dict:
    """
    Build a discovery response body for a provider's wire endpoint.

    Includes a mix of plain and prefix-wrapped ids so list_models()
    normalization can be observed per wire.
    """
    wire = wire_of(provider_id)
    if wire == OLLAMA_WIRE:
        return {
            "models": [
                {"name": "llama3:8b", "model": "llama3:8b", "size": 1024},
                {"name": "gpt-test", "model": "gpt-test", "size": 2048},
            ]
        }
    if wire == ANTHROPIC_WIRE:
        return {
            "object": "list",
            "data": [
                {"id": "claude-3-5-sonnet", "type": "model"},
                {"id": "claude-4", "type": "model"},
            ],
        }
    if wire == GEMINI_WIRE:
        return {
            "models": [
                {"name": "models/gemini-1.5-pro", "displayName": "Gemini"},
                {"name": "models/gpt-test", "displayName": "Probe"},
            ]
        }
    return {
        "object": "list",
        "data": [
            {"id": "gpt-4", "object": "model"},
            {"id": DEFAULT_MODEL, "object": "model"},
        ],
    }


class HTTPRecorder:
    """
    Scriptable httpx stand-in that routes calls to URL-pattern handlers
    and records every request (method, url, headers, json, timeout).
    """

    def __init__(self, handlers: Dict[str, Any]) -> None:
        self.handlers = handlers
        self.requests: List[dict] = []

    def _find(self, url: str) -> Any:
        for pattern, handler in self.handlers.items():
            if pattern in url:
                return handler
        raise AssertionError(f"No mock handler registered for URL: {url}")

    def _run(self, method: str, url: str, kwargs: Dict[str, Any]) -> Any:
        self.requests.append({"method": method, "url": url, **kwargs})
        return self._find(url)(method, url, **kwargs)

    def get(self, url: str, **kwargs) -> Any:
        return self._run("GET", url, kwargs)

    def post(self, url: str, **kwargs) -> Any:
        return self._run("POST", url, kwargs)

    def stream(self, method: str, url: str, **kwargs) -> Any:
        return self._run(method, url, kwargs)


class _SpyAsyncClient:
    """
    httpx.AsyncClient stand-in sharing the HTTPRecorder's handlers so
    async surfaces exercise the same scripts and request log.
    """

    def __init__(self, recorder: HTTPRecorder, **kwargs) -> None:
        self.recorder = recorder
        self.init_kwargs = kwargs

    def _route(self, method: str, url: str, kwargs: Dict[str, Any]) -> Any:
        self.recorder.requests.append({"method": method, "url": url, **kwargs})
        return self.recorder._find(url)(method, url, **kwargs)

    async def get(self, url: str, **kwargs) -> Any:
        return self._route("GET", url, kwargs)

    async def post(self, url: str, **kwargs) -> Any:
        return self._route("POST", url, kwargs)

    def stream(self, method: str, url: str, **kwargs) -> Any:
        return self._route(method, url, kwargs)

    async def __aenter__(self) -> "_SpyAsyncClient":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _SpySyncClient:
    """
    httpx.Client stand-in sharing the HTTPRecorder's handlers so sync
    surfaces exercise the same scripts and request log.
    """

    def __init__(self, recorder: HTTPRecorder, **kwargs) -> None:
        self.recorder = recorder
        self.init_kwargs = kwargs

    def get(self, url: str, **kwargs) -> Any:
        return self.recorder.get(url, **kwargs)

    def post(self, url: str, **kwargs) -> Any:
        return self.recorder.post(url, **kwargs)

    def stream(self, method: str, url: str, **kwargs) -> Any:
        return self.recorder.stream(method, url, **kwargs)

    def close(self) -> None:
        pass

    def __enter__(self) -> "_SpySyncClient":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def install_http_mocks(
    monkeypatch,
    handlers: Dict[str, Any],
) -> HTTPRecorder:
    """
    Monkeypatch the sync and async wire surfaces with the recorder for
    the duration of a test.

    Async surfaces construct ``httpx.AsyncClient`` and sync surfaces
    construct ``httpx.Client`` (via the bounded-request stand-ins,
    because the top-level ``httpx`` helpers cannot carry the response
    budget hook), so two constructor patches cover every module. The
    legacy top-level ``httpx.get/post/stream`` names stay patched for
    any remaining direct callers.
    """
    recorder = HTTPRecorder(handlers)
    monkeypatch.setattr(httpx, "get", recorder.get)
    monkeypatch.setattr(httpx, "post", recorder.post)
    monkeypatch.setattr(httpx, "stream", recorder.stream)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _SpyAsyncClient(recorder, **kw))
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: _SpySyncClient(recorder, **kw)
    )
    return recorder


def endpoint_pattern(provider_id: str) -> Dict[str, str]:
    """Map wire surfaces to URL patterns for handler registration."""
    wire = wire_of(provider_id)
    if wire == OLLAMA_WIRE:
        return {"chat": "/api/chat", "discovery": "/api/tags"}
    if wire == ANTHROPIC_WIRE:
        return {"chat": "/messages", "discovery": "/models"}
    if wire == GEMINI_WIRE:
        return {"chat": ("generateContent", "streamGenerateContent"), "discovery": "/models"}
    return {"chat": "/chat/completions", "discovery": "/models"}


def build_handlers(
    provider_id: str,
    chat_factory: Any = None,
    discovery_factory: Any = None,
) -> Dict[str, Any]:
    """
    Build a handler dict for the provider's endpoints. The chat pattern is
    registered first so gemini's ``:generateContent`` URLs never fall
    through to the broader ``/models`` discovery pattern.
    """
    patterns = endpoint_pattern(provider_id)
    handlers: Dict[str, Any] = {}
    if chat_factory is not None:
        if isinstance(patterns["chat"], (list, tuple)):
            for pattern in patterns["chat"]:
                handlers[pattern] = chat_factory
        else:
            handlers[patterns["chat"]] = chat_factory
    if discovery_factory is not None:
        handlers[patterns["discovery"]] = discovery_factory
    return handlers
