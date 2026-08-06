"""
Durable, metadata-only request log backed by the shared ``platform.db``.

``RequestLogStore`` captures one row per routed HTTP request (schema v6)
through a bounded in-memory buffer drained by a background writer, so the
ASGI hot path never touches SQLite and never raises. Rows land in the
``request_log`` table on the shared ``platform.db`` (WAL + ``0600`` via
``PlatformStore``).

Privacy contract: rows hold metadata only - ts, route, method, status,
latency, opaque key id, client bucket, trimmed User-Agent, and the
auth-scheme label. Prompts, bodies, responses, raw keys, hash material,
provider keys, and correlation ids are never stored.

Capture semantics (P6.5 decision A): ``record`` is non-blocking and never
raises; a full buffer drops the oldest buffered row and a store outage
drops the drained batch. Readers are bounded and best-effort, so an
unavailable store degrades to an empty Applications view.

The module-level ``request_log()`` singleton is built lazily (tests inject
an isolated store by monkeypatching the accessor), matching the
event-log and auth KeyStore patterns.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import deque
from typing import Optional

from app.services import platform_store

# Bounded in-memory capture buffer so the hot path never blocks on SQLite.
_MAX_BUFFER = 1000

# Bounded read ceiling so the projection/CLI surfaces never dump the table.
_MAX_QUERY_LIMIT = 5000

# Bounded diagnostics/debug read ceiling for internal tests.
_MAX_DEBUG_LIMIT = 10000


class RequestLogStore:
    """
    Write-behind request-log store on the shared ``platform.db``.

    Capture is buffered in memory and flushed by a background daemon
    thread (or an explicit ``flush()``). The connection is opened lazily
    and best-effort: a failure leaves it None and every operation retries
    the open, so the request hot path degrades to a dropped counter
    instead of raising.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        flush_interval_seconds: Optional[int] = None,
        retention_days: Optional[int] = None,
    ) -> None:
        from app.core.config import settings

        self._path = path or str(platform_store.default_path())
        self._flush_interval_seconds = (
            flush_interval_seconds
            if flush_interval_seconds is not None
            else settings.request_log_flush_interval_seconds
        )
        self._retention_days = (
            retention_days
            if retention_days is not None
            else settings.request_log_retention_days
        )
        self._lock = threading.Lock()
        self._buffer: deque = deque(maxlen=_MAX_BUFFER)
        self._conn: Optional[sqlite3.Connection] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._flushed = 0
        self._dropped = 0
        self._flush_errors: deque = deque(maxlen=20)
        self._last_flush_at: Optional[float] = None
        self.start()

    @property
    def path(self) -> str:
        return self._path

    def record(
        self,
        *,
        route: str,
        client_bucket: str,
        client_ua: str = "",
        key_id: Optional[str] = None,
        method: str = "",
        status: Optional[int] = None,
        latency_ms: Optional[float] = None,
        auth_scheme: str = "",
        ts: Optional[float] = None,
    ) -> None:
        """
        Buffer one request's metadata. Never raises and never blocks on
        SQLite; a full buffer drops the oldest buffered row.
        """
        with self._lock:
            if len(self._buffer) >= _MAX_BUFFER:
                self._dropped += 1

            self._buffer.append(
                (
                    ts if ts is not None else time.time(),
                    route or "unmatched",
                    method or "",
                    status,
                    round(latency_ms, 3) if latency_ms is not None else None,
                    (key_id or "").strip() or None,
                    (client_bucket or "other").strip() or "other",
                    (client_ua or "").strip()[:200],
                    (auth_scheme or "none").strip() or "none",
                )
            )

    def start(self) -> None:
        """
        Begin the periodic flush loop. Safe to call multiple times and
        from any thread; a zero flush interval leaves the loop stopped.
        """
        if self._flush_interval_seconds <= 0:
            return

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return

            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="request-log-flusher",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """
        Stop the loop and flush any pending rows before closing.
        """
        self._stop.set()

        with self._lock:
            thread = self._thread

        if thread is not None:
            thread.join(timeout=self._flush_interval_seconds + 5)

    def close(self) -> None:
        """
        Flush pending rows best-effort, then release the connection and
        stop the background loop.
        """
        self._stop.set()

        with self._lock:
            thread = self._thread

        if thread is not None:
            thread.join(timeout=self._flush_interval_seconds + 5)

        self.flush()

        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def flush(self) -> int:
        """
        Drain the buffer into ``request_log`` and prune retention.
        Returns the number of rows written; a store outage drops the
        batch and returns 0. Never raises.
        """
        with self._lock:
            rows = list(self._buffer)
            self._buffer.clear()

        if not rows:
            self._prune()
            return 0

        try:
            if not self._ensure_open():
                self._drop_batch(len(rows))
                return 0

            with self._lock:
                with self._conn:
                    self._conn.executemany(
                        "INSERT INTO request_log ("
                        "  ts, route, method, status, latency_ms, key_id,"
                        "  client_bucket, ua, auth_scheme"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        rows,
                    )

            self._prune()

            with self._lock:
                self._flushed += len(rows)
                self._last_flush_at = time.time()

            return len(rows)
        except Exception:  # noqa: BLE001 - capture must never break a request
            self._drop_batch(len(rows))
            return 0

    def query(
        self,
        *,
        limit: int = 500,
        route: Optional[str] = None,
        client_bucket: Optional[str] = None,
        key_id: Optional[str] = None,
        since: Optional[float] = None,
    ) -> list:
        """
        Return the newest ``limit`` rows (bounded), optionally filtered by
        route / client bucket / key id / a wall-clock ``since`` cutoff.
        Raises OSError when the store is unavailable.
        """
        limit = max(1, min(int(limit), _MAX_QUERY_LIMIT))

        clauses: list = []
        params: list = []

        if route:
            clauses.append("route = ?")
            params.append(route)
        if client_bucket:
            clauses.append("client_bucket = ?")
            params.append(client_bucket)
        if key_id:
            clauses.append("key_id = ?")
            params.append(key_id)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)

        where = " WHERE " + " AND ".join(clauses) if clauses else ""

        if not self._ensure_open():
            raise OSError("request log is not available")

        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, route, method, status, latency_ms, key_id,"
                "  client_bucket, ua, auth_scheme"
                f" FROM request_log{where}"
                " ORDER BY ts DESC, id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()

        return [
            {
                "ts": row[0],
                "route": row[1],
                "method": row[2],
                "status": row[3],
                "latency_ms": row[4],
                "key_id": row[5],
                "client_bucket": row[6],
                "ua": row[7],
                "auth_scheme": row[8],
            }
            for row in rows
        ]

    def auth_totals(self) -> dict:
        """
        Counts of requests by presented auth-scheme label. Best-effort:
        an unavailable store yields an empty mapping.
        """
        if not self._ensure_open():
            return {}

        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT auth_scheme, count(*) FROM request_log"
                    " GROUP BY auth_scheme"
                ).fetchall()
        except Exception:  # noqa: BLE001 - read path is best-effort
            return {}

        return {row[0] or "none": row[1] for row in rows}

    def prune_retention(self, days: int) -> int:
        """
        Delete request-log rows older than ``days`` days and return the
        number removed. ``days <= 0`` disables pruning (returns 0).
        """
        if days <= 0:
            return 0

        cutoff = time.time() - int(days) * 86400

        if not self._ensure_open():
            raise OSError("request log is not available")

        with self._lock:
            with self._conn:
                cursor = self._conn.execute(
                    "DELETE FROM request_log WHERE ts < ?", (cutoff,)
                )

        return cursor.rowcount

    def count(self) -> int:
        """
        Total number of persisted request-log rows (diagnostics/tests).
        """
        if not self._ensure_open():
            raise OSError("request log is not available")

        with self._lock:
            return self._conn.execute(
                "SELECT count(*) FROM request_log"
            ).fetchone()[0]

    def stats(self) -> dict:
        """
        Diagnostics about the write-behind flush loop.
        """
        with self._lock:
            return {
                "path": self._path,
                "buffered": len(self._buffer),
                "flushed": self._flushed,
                "dropped": self._dropped,
                "last_flush_at": self._last_flush_at,
                "flush_errors": list(self._flush_errors),
                "running": self._thread is not None and self._thread.is_alive(),
            }

    # ============================
    # Internals
    # ============================

    def _loop(self) -> None:
        while not self._stop.wait(self._flush_interval_seconds):
            try:
                self.flush()
            except Exception:
                pass

    def _prune(self) -> None:
        if self._retention_days <= 0:
            return

        try:
            self.prune_retention(self._retention_days)
        except Exception:
            pass

    def _drop_batch(self, count: int) -> None:
        with self._lock:
            self._dropped += count
            self._flush_errors.append(
                {"at": time.time(), "rows": count}
            )

    def _ensure_open(self) -> bool:
        if self._conn is not None:
            return True

        with self._lock:
            if self._conn is None:
                try:
                    self._conn = platform_store.open_connection(self._path)
                except Exception:
                    self._conn = None

        return self._conn is not None


_LOG_SINGLETON: Optional[RequestLogStore] = None


def request_log() -> RequestLogStore:
    """
    The process-wide request-log store (lazily built). Tests monkeypatch
    this accessor to point at an isolated store.
    """
    global _LOG_SINGLETON

    if _LOG_SINGLETON is None:
        _LOG_SINGLETON = RequestLogStore()

    return _LOG_SINGLETON


def reset_request_log() -> None:
    """
    Close and drop the request-log singleton (test isolation).
    """
    global _LOG_SINGLETON

    if _LOG_SINGLETON is not None:
        _LOG_SINGLETON.close()
        _LOG_SINGLETON = None
