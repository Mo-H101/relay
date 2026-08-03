"""
Background writer that flushes in-memory learned health and telemetry
state to the StateStore.

Runs on a single daemon thread, modeled after HealthRefresher.
Injectable and inert until start() is called. SQLite writes happen only
on this thread (periodically) or on explicit flush() calls -- never on
the chat request path.
"""

import logging
import threading
import time
from collections import deque
from typing import Optional

from app.services.metrics import relay_metrics

_logger = logging.getLogger("relay")


class StateFlusher:
    """
    Periodically writes the in-memory HealthStore and TelemetryStore
    state to a StateStore.

    flush() snapshots the stores under their own locks and replaces the
    persisted rows in one transaction per table, so the SQLite database
    never holds stale partial state.
    """

    def __init__(
        self,
        health_store,
        telemetry,
        state_store,
        interval_seconds: int = 60,
        retention_days: int = 0,
        quality_store=None,
        decision_engine=None,
    ) -> None:
        self._health_store = health_store
        self._telemetry = telemetry
        self._state_store = state_store
        self._quality_store = quality_store
        self._decision_engine = decision_engine
        self._interval_seconds = max(1, int(interval_seconds))
        self._retention_days = int(retention_days)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_flush_at: Optional[float] = None
        self._flush_count = 0
        self._flush_errors: deque = deque(maxlen=20)
        self._consecutive_flush_failures = 0

    def flush(self) -> None:
        """
        Write the current in-memory state to the StateStore. Safe to call
        from any thread.
        """
        if self._state_store is None:
            return

        try:
            learned = self._health_store.export_learned_state()
            telemetry = self._telemetry.export_state()

            self._state_store.save_learned_state(learned)
            self._state_store.save_telemetry(telemetry)

            if self._quality_store is not None:
                self._state_store.save_quality(
                    self._quality_store.export_state()
                )

            if self._decision_engine is not None:
                self._state_store.save_decision_stats(
                    self._decision_engine.stats()
                )

            if self._retention_days > 0:
                self._state_store.prune_retention(self._retention_days)
        except Exception as exc:
            self._consecutive_flush_failures += 1
            if self._consecutive_flush_failures >= 5:
                _logger.warning(
                    "state flush has failed %d consecutive times; last error: %s",
                    self._consecutive_flush_failures,
                    str(exc),
                )
            with self._lock:
                self._flush_errors.append(
                    {"at": time.time(), "message": str(exc)}
                )
            relay_metrics.persistence_flush_failures.inc()
            raise
        else:
            self._consecutive_flush_failures = 0
            with self._lock:
                self._last_flush_at = time.time()
                self._flush_count += 1

    def set_retention_days(self, retention_days: int) -> None:
        """
        Update the retention window applied by future prunes. Negative
        values are clamped to 0 (pruning disabled).
        """
        self._retention_days = max(0, int(retention_days))

    def flush_stats(self) -> dict:
        """
        Diagnostic information about the write-behind flush loop.
        """
        with self._lock:
            return {
                "last_flush_at": self._last_flush_at,
                "flush_count": self._flush_count,
                "flush_errors": list(self._flush_errors),
            }

    def start(self) -> None:
        """
        Begin the periodic flush loop. Safe to call multiple times.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return

            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="state-flusher",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """
        Signal the loop to stop and wait for the current pass to finish.
        """
        self._stop.set()

        with self._lock:
            thread = self._thread

        if thread is not None:
            thread.join(timeout=self._interval_seconds + 5)

    @property
    def is_running(self) -> bool:
        """
        Whether the background loop is currently alive.
        """
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self.flush()
            except Exception:
                _logger.exception("state flush failed")
