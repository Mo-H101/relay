"""
Bounded in-memory client activity metadata for the Applications surface.

Records, per (client bucket, endpoint route), request counters plus the
trimmed User-Agent and the presented auth-scheme label. Metadata only:
the ``Authorization`` header value, API keys, request bodies, prompts,
messages, and generated responses are never stored.

This is the interim "connected applications" store. It is deliberately
isolated (write-only tracker + read projection) so the P6 ``platform.db``
swap (``apps`` = labeled keys x ``request_log``) can replace it locally.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading
import time

from app.core.config import settings

# UA stored as entered by the capture path (already trimmed there too).
_MAX_UA = 200
_MAX_ENTRIES = 200


@dataclass
class ClientActivityEntry:
    """
    Read-only snapshot of one (client bucket, route) activity row.
    """

    bucket: str
    ua: str
    route: str
    requests: int
    successes: int
    failures: int
    auth_schemes: tuple[str, ...]
    last_seen: float


class _Row:
    __slots__ = (
        "requests",
        "successes",
        "failures",
        "auth_schemes",
        "ua",
        "last_seen",
    )

    def __init__(self) -> None:
        self.requests = 0
        self.successes = 0
        self.failures = 0
        self.auth_schemes: set[str] = set()
        self.ua = ""
        self.last_seen = 0.0


class ClientTracker:
    """
    Thread-safe rolling window of per-client endpoint activity.

    Bounded by ``_MAX_ENTRIES`` and pruned by age (``ops_window_seconds``)
    on access, mirroring the ops store conventions so memory stays flat.
    """

    def __init__(
        self,
        window_seconds: int | None = None,
        max_entries: int | None = None,
    ) -> None:
        self._window_seconds = (
            window_seconds
            if window_seconds is not None
            else settings.ops_window_seconds
        )
        self._max_entries = max_entries if max_entries is not None else _MAX_ENTRIES
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str], _Row] = {}
        self._last_seen: dict[str, float] = {}
        self._scheme_totals: dict[str, int] = {}

    def _prune(self, now: float) -> None:
        window = self._window_seconds

        if window and window > 0:
            cutoff = now - window

            for key in [key for key, row in self._rows.items() if row.last_seen < cutoff]:
                bucket = key[0]
                self._rows.pop(key, None)

                if not any(existing[0] == bucket for existing in self._rows):
                    self._last_seen.pop(bucket, None)

        overflow = len(self._rows) - self._max_entries

        if overflow > 0:
            oldest = sorted(self._rows.keys(), key=lambda key: self._rows[key].last_seen)
            for key in oldest[:overflow]:
                bucket = key[0]
                self._rows.pop(key, None)

                if not any(existing[0] == bucket for existing in self._rows):
                    self._last_seen.pop(bucket, None)

    def record(
        self,
        bucket: str,
        ua: str,
        route: str,
        status: int | None,
        auth_scheme: str,
    ) -> None:
        """
        Record one completed request's client metadata.
        """
        ua = (ua or "").strip()[: _MAX_UA]
        scheme = auth_scheme or "none"

        with self._lock:
            now = time.monotonic()
            self._prune(now)

            row = self._rows.setdefault((bucket, route), _Row())
            row.requests += 1
            row.successes += 1 if status is not None and status < 400 else 0
            row.failures += 1 if status is not None and status >= 400 else 0
            row.auth_schemes.add(scheme)
            row.ua = ua or row.ua
            row.last_seen = now

            self._last_seen[bucket] = max(self._last_seen.get(bucket, 0.0), now)
            self._scheme_totals[scheme] = self._scheme_totals.get(scheme, 0) + 1

    def activity(self) -> list[ClientActivityEntry]:
        """
        Snapshot of active (bucket, route) rows, newest bucket first.
        """
        with self._lock:
            now = time.monotonic()
            self._prune(now)

            rows = [
                ClientActivityEntry(
                    bucket=bucket,
                    ua=row.ua,
                    route=route,
                    requests=row.requests,
                    successes=row.successes,
                    failures=row.failures,
                    auth_schemes=tuple(sorted(row.auth_schemes)),
                    last_seen=row.last_seen,
                )
                for (bucket, route), row in self._rows.items()
            ]

        rows.sort(
            key=lambda entry: (
                self._last_seen.get(entry.bucket, 0.0),
                entry.bucket,
                entry.route,
            ),
            reverse=True,
        )
        return rows

    def auth_totals(self) -> dict[str, int]:
        """
        Counts of requests by presented auth-scheme label. Metadata only.
        """
        with self._lock:
            self._prune(time.monotonic())
            return dict(self._scheme_totals)

    def clear(self) -> None:
        """
        Remove all rows (test isolation and manual reset).
        """
        with self._lock:
            self._rows.clear()
            self._last_seen.clear()
            self._scheme_totals.clear()


client_tracking = ClientTracker()
