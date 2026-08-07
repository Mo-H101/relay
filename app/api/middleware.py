"""
Pure-ASGI middleware for HTTP-level metrics, operations events, and
request-size hardening.

Records request count/success/failure, latency to completion,
time-to-first-byte, and the active-request gauge, plus a rolling
metadata event for diagnostics. It never buffers the response body,
never inspects payloads, and never raises. Unmatched routes are labeled
"unmatched" to keep the label set bounded.

``BodySizeLimitMiddleware`` bounds request bodies (declared
Content-Length and chunked transfers) with a 413 so a runaway or hostile
client cannot force the process to buffer unbounded input.
"""

import time

from app.security.auth import auth_configured, auth_scheme
from app.services import request_log as request_log_module
from app.services.client_detection import classify_client
from app.services.metrics import relay_metrics
from app.services.ops_store import ops_store

UNMATCHED_ROUTE = "unmatched"

# Generous ceiling for a single request body (chat payloads, base64
# image parts, long-context replays); far above any legitimate gateway
# load, purely a memory-hardening floor.
REQUEST_BODY_MAX_BYTES = 64 * 1024 * 1024


class _BodyTooLarge(Exception):
    """Internal signal: the request body exceeded the configured limit."""


async def _send_413(send, max_bytes: int) -> None:
    body = b"request body too large"
    headers = [(b"content-type", b"text/plain; charset=utf-8")]
    headers.append((b"content-length", str(len(body)).encode("ascii")))
    await send({"type": "http.response.start", "status": 413, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class BodySizeLimitMiddleware:
    """
    Reject request bodies larger than ``max_bytes`` with 413.

    A declared Content-Length over the limit fails fast; otherwise the
    receive stream is wrapped so a chunked transfer that grows past the
    limit aborts with 413 before any response bytes are sent. Bodies under
    the limit flow through untouched.
    """

    def __init__(self, app, max_bytes: int = REQUEST_BODY_MAX_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        declared = None
        for name, value in scope.get("headers") or []:
            if name.lower() == b"content-length":
                try:
                    declared = int(value)
                except (TypeError, ValueError):
                    declared = None
                break

        if declared is not None and declared > self.max_bytes:
            await _send_413(send, self.max_bytes)
            return

        received = 0
        started = False

        async def guarded_send(message):
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request" and message.get("body"):
                received += len(message["body"])
                if received > self.max_bytes:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            if not started:
                await _send_413(send, self.max_bytes)


def _scope_headers(scope) -> dict[str, str]:
    """
    Flatten the ASGI headers list into a lower-cased string map. Header
    names and values are treated as bytes and decoded lossily; only the
    trimmed User-Agent and the presented auth-scheme label are kept.
    """
    result: dict[str, str] = {}

    for raw_key, raw_value in scope.get("headers") or []:
        key = raw_key.decode("latin-1").lower()

        if key in ("user-agent", "authorization", "x-relay-api-key"):
            result[key] = raw_value.decode("latin-1").strip()

    return result


class MetricsMiddleware:
    """
    ASGI middleware wrapping the application.

    Registered with app.add_middleware(MetricsMiddleware). The scope
    dict is shared with the application, so after the inner app returns,
    scope["route"] holds the matched Route and its template path.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        method = scope.get("method", "")
        start = time.perf_counter()
        status = None
        ttfb = None
        response_started = False

        relay_metrics.http_active.inc()

        async def send_wrapper(message):
            nonlocal status, ttfb, response_started
            if message.get("type") == "http.response.start":
                status = message.get("status")
                if not response_started:
                    response_started = True
                    ttfb = time.perf_counter() - start
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            status = status or 500
            raise
        finally:
            route = scope.get("route")
            route_path = getattr(route, "path", None)
            if not route_path:
                route_path = UNMATCHED_ROUTE

            duration = time.perf_counter() - start

            try:
                relay_metrics.record_http(
                    method,
                    route_path,
                    status,
                    duration,
                    ttfb,
                )
                ops_store.record_http(
                    method=method,
                    route=route_path,
                    status=status or 500,
                    latency_ms=duration * 1000.0,
                    key_id=scope.get("relay_key_id", ""),
                )
                self._record_client(scope, route_path, status or 500, duration)
            except Exception:
                pass

    def _record_client(
        self, scope, route_path: str, status: int, duration: float
    ) -> None:
        """
        Buffer one metadata-only request-log row (client bucket, trimmed
        UA, route, status, latency, opaque key id, and the presented
        auth-scheme label). The Authorization header value itself is
        never stored, and the write path never raises.
        """
        headers = _scope_headers(scope)
        user_agent = (headers.get("user-agent") or "")[:200]
        bucket = classify_client(user_agent)
        scheme = auth_scheme(
            path=route_path,
            authorization=headers.get("authorization", ""),
            x_api_key=headers.get("x-relay-api-key", ""),
            auth_enabled=auth_configured(),
        )
        request_log_module.request_log().record(
            route=route_path,
            method=scope.get("method", ""),
            status=status,
            latency_ms=duration * 1000.0,
            key_id=scope.get("relay_key_id"),
            client_bucket=bucket,
            client_ua=user_agent,
            auth_scheme=scheme,
        )
