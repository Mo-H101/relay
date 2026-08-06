"""
Background writer for the P9 project-continuity layer.

``ContinuityFlusher`` mirrors ``StateFlusher``: a single daemon thread
periodically drains queued continuity rows into the ``ConversationStore``
and applies retention pruning, with a final flush on shutdown and
consecutive-failure tracking. SQLite writes happen only on this thread
(or on explicit ``flush()`` calls) -- never on the chat request path.

P9c populates the write-behind buffer: the ``HandoffCoordinator`` enqueues
durable operations (``conversation.create``, ``turn.append``,
``summary.record``, ``compaction.record``, ``project_state.update``)
through ``enqueue()`` and the flusher drains them on its thread, applying
each to the matching ``ConversationStore`` method. A create that collides
with an existing row (a conversation resumed across processes) is treated
as idempotent so later turns still append to the existing conversation.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import deque
from typing import Optional

from app.services.metrics import relay_metrics

_logger = logging.getLogger("relay")

# Bounded in-memory buffer; a full buffer drops the oldest queued row so
# a flood of writes can never grow the heap without limit.
_MAX_QUEUE = 10000

# Continuity operations enqueued by the coordinator, mapped to the
# ConversationStore method that persists them.
_OP_METHODS = {
    "conversation.create": "create",
    "turn.append": "append_turn",
    "summary.record": "record_summary",
    "compaction.record": "record_compaction",
    "project_state.update": "update_project_state",
}


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
        self._queue: deque = deque()
        self._queued_count = 0
        self._drained_count = 0
        self._dropped_count = 0
        self._pruned_total = 0
        # Pending (queued-but-unflushed) rows per conversation, so the
        # store's in-flight registry stays accurate and background prunes
        # skip conversations that still have rows in this buffer.
        self._pending_per_conversation: dict = {}

    def enqueue(self, operation: str, **kwargs) -> bool:
        """
        Buffer one durable continuity operation for the write-behind
        thread. Never touches SQLite and never raises; a full buffer drops
        the oldest queued row (counted as dropped).
        """
        if operation not in _OP_METHODS:
            return False

        conversation_id = kwargs.get("conversation_id")

        with self._lock:
            if len(self._queue) >= _MAX_QUEUE:
                self._queue.popleft()
                self._dropped_count += 1
            self._queue.append((operation, dict(kwargs)))
            self._queued_count += 1
            relay_metrics.continuity_rows_queued.set(len(self._queue))

        if conversation_id and self._store is not None:
            self._store.mark_in_flight(conversation_id)
            with self._lock:
                self._pending_per_conversation[conversation_id] = (
                    self._pending_per_conversation.get(conversation_id, 0) + 1
                )

        return True

    @property
    def queue_size(self) -> int:
        """Number of rows waiting in the write-behind buffer."""
        with self._lock:
            return len(self._queue)

    def flush(self) -> int:
        """
        Drain queued continuity rows, then apply retention pruning.

        Returns the number of rows pruned this pass (0 when pruning is
        disabled or the store is unavailable). Never raises: a store
        outage is counted and degraded instead, and drained rows are
        counted per operation.
        """
        self._drain_queue()
        pruned = 0

        try:
            if self._store is not None and self._retention_days > 0:
                pruned = self._store.prune_retention(self._retention_days)
        except Exception as exc:
            self._record_flush_error(exc)
        else:
            self._consecutive_flush_failures = 0
            with self._lock:
                self._last_flush_at = time.time()
                self._flush_count += 1

        relay_metrics.continuity_flushes.inc()
        relay_metrics.continuity_pruned.inc(pruned)
        with self._lock:
            self._pruned_total += pruned
        return pruned

    def _drain_queue(self) -> int:
        """
        Apply every buffered row to the ConversationStore on this thread.
        A ``conversation.create`` that collides with an existing row is
        idempotent (the conversation predates this process); any other
        failure is counted, the row is retained at the head of the queue,
        and the drain stops so a transient store outage never loses
        un-flushed turns (P9e, audit §3.4).
        """
        drained = 0

        while True:
            with self._lock:
                if not self._queue:
                    break
                operation, kwargs = self._queue.popleft()

            method_name = _OP_METHODS.get(operation)

            if method_name is None or self._store is None:
                continue

            try:
                getattr(self._store, method_name)(**kwargs)
                drained += 1
                self._on_op_drained(kwargs.get("conversation_id"))
            except sqlite3.IntegrityError as exc:
                if operation == "conversation.create":
                    # Resume across processes: the conversation row already
                    # exists; keep it and let later turns append to it.
                    drained += 1
                    self._on_op_drained(kwargs.get("conversation_id"))
                    continue
                self._record_flush_error(exc)
                self._retain_row(operation, kwargs)
                break
            except Exception as exc:
                self._record_flush_error(exc)
                self._retain_row(operation, kwargs)
                break

        with self._lock:
            self._drained_count += drained
            relay_metrics.continuity_rows_queued.set(len(self._queue))

        return drained

    def _retain_row(self, operation: str, kwargs: dict) -> None:
        """
        Re-queue a row that failed to flush at the head of the buffer so a
        later pass can retry it. The conversation stays in-flight (its
        pending count is untouched) so a background prune never removes it.
        """
        with self._lock:
            self._queue.appendleft((operation, dict(kwargs)))
            relay_metrics.continuity_rows_queued.set(len(self._queue))

    def _on_op_drained(self, conversation_id: Optional[str]) -> None:
        """
        Release one pending row for a conversation; when the conversation
        has no rows left in the buffer, clear its in-flight marker so a
        background prune may consider it again.
        """
        if not conversation_id or self._store is None:
            return

        with self._lock:
            pending = self._pending_per_conversation.get(conversation_id, 0) - 1
            if pending <= 0:
                self._pending_per_conversation.pop(conversation_id, None)
                clear = True
            else:
                self._pending_per_conversation[conversation_id] = pending
                clear = False

        if clear:
            self._store.clear_in_flight(conversation_id)

    def _record_flush_error(self, exc: Exception) -> None:
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
                "queued": len(self._queue),
                "queued_total": self._queued_count,
                "drained_total": self._drained_count,
                "dropped_total": self._dropped_count,
                "pruned_total": self._pruned_total,
                "in_flight": list(self._store.in_flight)
                if self._store is not None
                else [],
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
