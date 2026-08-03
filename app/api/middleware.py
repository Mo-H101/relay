"""
Pure-ASGI middleware for HTTP-level metrics and operations events.

Records request count/success/failure, latency to completion,
time-to-first-byte, and the active-request gauge, plus a rolling
metadata event for diagnostics. It never buffers the response body,
never inspects payloads, and never raises. Unmatched routes are labeled
"unmatched" to keep the label set bounded.
"""

import time

from app.services.metrics import relay_metrics
from app.services.ops_store import ops_store

UNMATCHED_ROUTE = "unmatched"


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
                )
            except Exception:
                pass
