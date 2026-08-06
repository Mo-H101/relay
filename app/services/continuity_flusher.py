"""
Background writer for the P9 project-continuity layer.

``ContinuityFlusher`` mirrors ``StateFlusher``: a single daemon thread
periodically drains queued continuity rows into the ``ConversationStore``
and applies retention pruning, with a final flush on shutdown and
consecutive-failure tracking. SQLite writes happen only on this thread
(or on explicit ``flush()`` calls) -- never on the chat request path.

In P9a the write-behind buffer is not yet populated (chat integration
arrives in P9c); the flusher owns the lifecycle, the periodic retention
prune, and the shutdown hook that later phases fill.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional

from app.services.metrics import relay_metrics

_logger = logging.getLogger("relay")


class ContinuityFlusher:
    """
    Periodically flushes queued continuity rows and prunes retention.

    Injectable and inert until ``start()`` is called. ``flush()`` is safe
    to call from any thread.
    """

    def __init__(
        self,
        conversation_store,
        interval_seconds: int = 5,
        retention_days: int = 30,
    ) -> None:
        self._store = conversation_store
        self._interval_seconds = max(1, int(interval_seconds))
        self._retention_days = max(0, int(retention_days))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_flush_at: Optional[float] = None
        self._flush_count = 0
        self._flush_errors: deque = deque(maxlen=20)
        self._consecutive_flush_failures = 0

    def flush(self) -> int:
        """
        Drain queued continuity rows and apply retention pruning.

        Returns the number of rows pruned this pass (0 when pruning is
        disabled or the store is unavailable). Never raises: a store
        outage is counted and degraded instead.
        """
        pruned = 0

        try:
            if self._store is not None and self._retention_days > 0:
                pruned = self._store.prune_retention(self._retention_days)
        except Exception as exc:
            self._consecutive_flush_failures += 1
            if self._consecutive_flush_failures >= 5:
                _logger.warning(
                    "continuity flush has failed %d consecutive times; "
                    "last error: %s",
                    self._consecutive_flush_failures,
                    str(exc),
                )
            with self._lock:
                self._flush_errors.append(
                    {"at": time.time(), "message": str(exc)}
                )
            relay_metrics.continuity_flush_failures.inc()
        else:
            self._consecutive_flush_failures = 0
            with self._lock:
                self._last_flush_at = time.time()
                self._flush_count += 1

        relay_metrics.continuity_flushes.inc()
        relay_metrics.continuity_pruned.inc(pruned)
        return pruned

    def prune_now(self) -> int:
        """
        Apply an immediate retention prune (startup path). Returns rows
        pruned; a failure is counted but never raised.
        """
        return self.flush()

    def flush_stats(self) -> dict:
        """Diagnostics about the write-behind flush loop."""
        with self._lock:
            return {
                "interval_seconds": self._interval_seconds,
                "retention_days": self._retention_days,
                "last_flush_at": self._last_flush_at,
                "flush_count": self._flush_count,
                "flush_errors": list(self._flush_errors),
                "running": self._thread is not None and self._thread.is_alive(),
            }

    def start(self) -> None:
        """Begin the periodic flush loop. Safe to call multiple times."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return

            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="continuity-flusher",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Signal the loop to stop and wait for the current pass."""
        self._stop.set()

        with self._lock:
            thread = self._thread

        if thread is not None:
            thread.join(timeout=self._interval_seconds + 5)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self.flush()
            except Exception:
                _logger.exception("continuity flush failed")


__all__ = ["ContinuityFlusher"]
