"""
Bounded in-memory rolling window of request metadata for diagnostics.

Stores request metadata only: timestamps, routes, statuses, latencies,
provider/model, and streaming flags. Never stores prompts, responses,
API keys, proxy credentials, or user data. In-memory only; SQLite is
never touched for operations data, and the store is independent of
persistence configuration.
"""

import threading
import time
from collections import deque
from dataclasses import dataclass, field

from app.core.config import settings


@dataclass
class OpsEvent:
    """
    A single completed request, metadata only.
    """

    ts: float
    kind: str  # "http" or "chat"
    method: str = ""
    route: str = ""
    status: int | None = None
    latency_ms: float = 0.0
    endpoint: str = ""
    provider: str = ""
    model: str = ""
    stream: str = ""
    success: bool = True
    fallback: bool = False
    attempts: int = 0


class RequestStatsStore:
    """
    Thread-safe rolling window of completed request events.

    Bounded by a maximum event count and pruned by age on access, so
    memory stays flat regardless of request volume.
    """

    def __init__(
        self,
        window_seconds: int | None = None,
        max_events: int | None = None,
    ) -> None:
        self._window_seconds = (
            window_seconds
            if window_seconds is not None
            else settings.ops_window_seconds
        )
        self._max_events = (
            max_events
            if max_events is not None
            else settings.ops_max_events
        )
        self._lock = threading.Lock()
        self._events: deque = deque()

    def _prune(self, now: float) -> None:
        window = self._window_seconds

        if window and window > 0:
            cutoff = now - window

            while self._events and self._events[0].ts < cutoff:
                self._events.popleft()

    def record_http(
        self,
        method: str,
        route: str,
        status: int,
        latency_ms: float,
    ) -> None:
        """
        Record a completed HTTP request event.
        """
        self._append(
            OpsEvent(
                ts=time.monotonic(),
                kind="http",
                method=method,
                route=route,
                status=status,
                latency_ms=max(0.0, latency_ms),
            )
        )

    def record_chat(
        self,
        endpoint: str,
        stream: bool,
        provider: str,
        model: str,
        success: bool,
        fallback: bool,
        latency_ms: float,
        attempts: int = 0,
    ) -> None:
        """
        Record a completed chat request event.
        """
        self._append(
            OpsEvent(
                ts=time.monotonic(),
                kind="chat",
                endpoint=endpoint,
                stream="true" if stream else "false",
                provider=provider,
                model=model,
                success=bool(success),
                fallback=bool(fallback),
                latency_ms=max(0.0, latency_ms),
                attempts=max(0, int(attempts)),
            )
        )

    def _append(self, event: OpsEvent) -> None:
        with self._lock:
            self._prune(time.monotonic())
            self._events.append(event)

            overflow = len(self._events) - self._max_events

            for _ in range(max(0, overflow)):
                self._events.popleft()

    def events(self) -> list:
        """
        Snapshot of events still inside the window.
        """
        with self._lock:
            self._prune(time.monotonic())
            return list(self._events)

    def clear(self) -> None:
        """
        Remove all events (test isolation and manual reset).
        """
        with self._lock:
            self._events.clear()

    def stats(self) -> dict:
        """
        Aggregate the rolling window into a diagnostics-ready summary.
        Returns an all-zero/None shape when the window is empty.
        """
        events = self.events()

        latencies = sorted(event.latency_ms for event in events)
        requests = len(events)

        successes = 0
        stream_events = []
        providers: dict = {}
        endpoints: dict = {}
        chats = 0
        chat_attempts = 0
        chat_fallbacks = 0

        for event in events:
            if event.kind == "chat":
                chats += 1
                chat_attempts += event.attempts

                if event.fallback:
                    chat_fallbacks += 1

                if event.success:
                    successes += 1

                if event.stream == "true":
                    stream_events.append(event)

                entry = providers.setdefault(
                    event.provider,
                    {"requests": 0, "successes": 0, "latencies": []},
                )
                entry["requests"] += 1
                if event.success:
                    entry["successes"] += 1
                entry["latencies"].append(event.latency_ms)
            else:
                if event.status is not None and event.status < 400:
                    successes += 1

                entry = endpoints.setdefault(
                    event.route,
                    {"requests": 0, "latencies": []},
                )
                entry["requests"] += 1
                entry["latencies"].append(event.latency_ms)

        return {
            "window_seconds": self._window_seconds,
            "max_events": self._max_events,
            "requests": requests,
            "successes": successes,
            "failures": requests - successes,
            "success_rate": (
                round(successes / requests, 4) if requests else None
            ),
            "failure_rate": (
                round((requests - successes) / requests, 4)
                if requests
                else None
            ),
            "average_latency_ms": (
                round(sum(latencies) / len(latencies), 2) if latencies else None
            ),
            "p50_latency_ms": self._quantile(latencies, 0.5),
            "p95_latency_ms": self._quantile(latencies, 0.95),
            "chats": chats,
            "chat_attempts": chat_attempts,
            "chat_fallbacks": chat_fallbacks,
            "streaming": self._streaming_summary(stream_events),
            "providers": [
                {
                    "provider": provider,
                    "requests": data["requests"],
                    "success_rate": (
                        round(data["successes"] / data["requests"], 4)
                        if data["requests"]
                        else None
                    ),
                    "average_latency_ms": (
                        round(
                            sum(data["latencies"]) / len(data["latencies"]),
                            2,
                        )
                        if data["latencies"]
                        else None
                    ),
                }
                for provider, data in sorted(providers.items())
            ],
            "endpoints": [
                {
                    "route": route,
                    "requests": data["requests"],
                    "average_latency_ms": (
                        round(
                            sum(data["latencies"]) / len(data["latencies"]),
                            2,
                        )
                        if data["latencies"]
                        else None
                    ),
                }
                for route, data in sorted(endpoints.items())
            ],
        }

    @staticmethod
    def _quantile(latencies: list, q: float):
        if not latencies:
            return None
        index = min(len(latencies) - 1, int(q * (len(latencies) - 1)))
        return round(latencies[index], 2)

    @staticmethod
    def _streaming_summary(stream_events: list) -> dict:
        if not stream_events:
            return {
                "requests": 0,
                "successes": 0,
                "average_latency_ms": None,
            }

        latencies = [event.latency_ms for event in stream_events]
        successes = sum(1 for event in stream_events if event.success)

        return {
            "requests": len(stream_events),
            "successes": successes,
            "average_latency_ms": round(
                sum(latencies) / len(latencies), 2
            ),
        }


ops_store = RequestStatsStore()
