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

from app.providers.base import Provider

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
