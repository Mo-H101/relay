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
R3: a ``turn.append`` whose ``(conversation_id, seq)`` row already exists
is likewise idempotent -- it is already durable -- so one stale-seq turn
can never stall the write-behind queue for the whole project.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import deque
from typing import Optional

from app.services.conversation_store import MalformedInputError
from app.services.conversation_store import _validate_non_negative_int
from app.services.metrics import relay_metrics

_logger = logging.getLogger("relay")

# Bounded in-memory buffer. A full buffer never evicts an older durable row:
# coalescible metadata is folded into its latest queued operation, while an
# operation that cannot be admitted is rejected and counted explicitly.
_MAX_QUEUE = 10000

# Continuity operations enqueued by the coordinator, mapped to the
# ConversationStore method that persists them.
_OP_METHODS = {
    "conversation.create": "create",
    "turn.append": "append_turn",
    "turn.update": "update_turn",
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
        self._rejected_count = 0
        self._last_rejection_log_at = 0.0
        self._pruned_total = 0
        # Pending (queued-but-unflushed) rows per conversation, so the
        # store's in-flight registry stays accurate and background prunes
        # skip conversations that still have rows in this buffer.
        self._pending_per_conversation: dict = {}
        # N-11: the highest ``seq`` of any ``turn.append``/``turn.update``
        # still queued (not yet flushed) for a ``(conversation_id, key_id)``.
        # This is the authoritative watermark of *accepted-but-not-yet-durable*
        # turns, independent of the coordinator's bounded in-memory LRU. A
        # conversation state that is evicted from the LRU while these rows are
        # pending must never have its sequence number reused: the recreated
        # state seeds its next seq from this watermark so the flusher never
        # misclassifies a genuinely new turn as an "already-durable" duplicate.
        self._pending_seq: dict = {}

    def enqueue(self, operation: str, **kwargs) -> bool:
        """
        Buffer one durable continuity operation for the write-behind
        thread. Never touches SQLite and never raises. A full buffer may
        coalesce a safe metadata update; otherwise it rejects the new row
        without evicting an older operation or registering a false pending
        count.

        N-8: a ``turn.update`` with malformed accounting (negative, float,
        bool, or string tokens) is rejected when it does not coalesce with
        a matching queue entry.  When it *does* coalesce, the N-7 stripping
        applies and only the malformed fields are discarded.
        """
        if operation not in _OP_METHODS:
            return False

        conversation_id = kwargs.get("conversation_id")

        # N-8: pre-validate accounting before coalescing.  This flag
        # records whether any non-None accounting field is malformed so
        # the standalone (non-coalescing) path can reject the update.
        _has_malformed_accounting = False
        if operation == "turn.update":
            for field in self._ACCOUNTING_FIELDS:
                value = kwargs.get(field)
                if value is not None:
                    try:
                        _validate_non_negative_int(value, field)
                    except MalformedInputError:
                        _has_malformed_accounting = True
                        break

        with self._lock:
            if self._coalesce_locked(operation, kwargs):
                self._queued_count += 1
                relay_metrics.continuity_rows_queued.set(len(self._queue))
                self._track_pending_seq_locked(operation, kwargs)
                # Coalescing merges into an existing queued row already
                # counted in ``_pending_per_conversation``; it adds no new
                # drainable row, so it must NOT increment the per-conversation
                # count (that would leave the count unbalanced and the
                # conversation stuck in-flight after draining).
                return True

            # N-8: reject standalone malformed turn.update.  When
            # coalescing did not find a match, the update would be
            # persisted as-is — including a changed outcome and NULL
            # accounting that erases durable state.  Drop it entirely.
            if _has_malformed_accounting:
                self._dropped_count += 1
                relay_metrics.continuity_rows_queued.set(len(self._queue))
                return False

            if len(self._queue) >= _MAX_QUEUE:
                self._rejected_count += 1
                relay_metrics.continuity_rows_queued.set(len(self._queue))
                now = time.time()
                if now - self._last_rejection_log_at >= 1.0:
                    _logger.warning(
                        "continuity queue full; rejecting operation %s",
                        operation,
                    )
                    self._last_rejection_log_at = now
                return False
            self._queue.append((operation, dict(kwargs)))
            self._queued_count += 1
            relay_metrics.continuity_rows_queued.set(len(self._queue))
            self._track_pending_seq_locked(operation, kwargs)
            # N-11-fix: bump the pending-count increment atomically with the
            # queue insertion (same lock) so a concurrent drain can never
            # observe the conversation as fully drained — and drop its seq
            # watermark — while this just-queued row is still in the buffer.
            # Splitting the increment into a later, separate critical section
            # left a window where a drain popped the conversation's last prior
            # row and cleared _pending_seq (N-11 unflushed-seq protection)
            # before the new row's count was added, silently allowing a
            # duplicate seq to collide and an accepted turn to be dropped.
            if conversation_id:
                self._pending_per_conversation[conversation_id] = (
                    self._pending_per_conversation.get(conversation_id, 0) + 1
                )

        # In-flight marker is a pruning gate and updates an in-memory set in
        # the store; it need not be atomic with the count. Held outside the
        # flusher lock so we never hold a flusher lock across a store call.
        if conversation_id and self._store is not None:
            self._store.mark_in_flight(conversation_id)

        return True

    _ACCOUNTING_FIELDS = ("tokens_in", "tokens_out", "latency_ms")

    def _track_pending_seq_locked(self, operation: str, kwargs: dict) -> None:
        """Record the highest unflushed turn seq for a conversation.

        N-11: only ``turn.append``/``turn.update`` rows carry sequence
        numbers that constrain later sequencing. The watermark is
        monotonic (never lowered) so a conversation whose state was evicted
        from the coordinator LRU while these rows were still queued can
        never reuse a sequence number that is accepted-but-not-yet-durable.
        The caller holds ``self._lock``.
        """
        if operation not in ("turn.append", "turn.update"):
            return
        cid = kwargs.get("conversation_id")
        seq = kwargs.get("seq")
        if not cid or seq is None:
            return
        ident = (str(cid), str(kwargs.get("key_id") or ""))
        try:
            seq_i = int(seq)
        except (TypeError, ValueError):
            return
        self._pending_seq[ident] = max(self._pending_seq.get(ident, 0), seq_i)

    def pending_max_seq(self, conversation_id: str, key_id=None) -> Optional[int]:
        """The highest turn seq still queued (unflushed) for a conversation.

        Returns None when the flusher has no pending turn rows for the
        conversation. Used by ``HandoffCoordinator.start`` to seed a fresh
        in-memory state above both durable SQLite seq *and* accepted-but-
        unflushed seq, so LRU eviction can never cause a sequence collision
        that silently drops an accepted turn (N-11).
        """
        if not conversation_id:
            return None
        ident = (str(conversation_id), str(key_id or ""))
        with self._lock:
            return self._pending_seq.get(ident)

    def _coalesce_locked(self, operation: str, kwargs: dict) -> bool:
        """Fold a superseded non-turn operation into the queue.

        Turn appends are essential. A queued finalization can safely merge
        into its matching append, and repeated project/summary/compaction
        metadata keeps only the newest state. No turn append is discarded.
        The caller holds ``self._lock``.
        """
        cid = kwargs.get("conversation_id")
        key_id = kwargs.get("key_id")

        if operation == "turn.update":
            # N-7: validate accounting before merging.  Malformed fields
            # (e.g. negative/float/bool tokens from an untrusted provider)
            # are set to None so the merge loop's ``if value is not None``
            # check preserves the existing row's original (valid) values.
            # This prevents a malformed update from contaminating a valid
            # provisional append and causing the entire coalesced row to be
            # dropped by the drain loop.
            for field in self._ACCOUNTING_FIELDS:
                value = kwargs.get(field)
                if value is not None:
                    try:
                        _validate_non_negative_int(value, field)
                    except MalformedInputError:
                        kwargs[field] = None

            seq = kwargs.get("seq")
            for index in range(len(self._queue) - 1, -1, -1):
                existing_op, existing = self._queue[index]
                if (
                    existing.get("conversation_id") == cid
                    and existing.get("key_id") == key_id
                    and existing.get("seq") == seq
                    and existing_op in {"turn.append", "turn.update"}
                ):
                    merged = dict(existing)
                    for name, value in kwargs.items():
                        if value is not None:
                            merged[name] = value
                    self._queue[index] = (existing_op, merged)
                    return True

        if operation in {
            "project_state.update",
            "summary.record",
            "compaction.record",
        }:
            # project_state rows are keyed by (key_id, project_key) in the
            # store and carry no conversation id; folding two different
            # projects into one queue slot silently drops the older
            # project's update, so its identity must include project_key.
            identity = (operation, cid, key_id, kwargs.get("project_key"))
            for index in range(len(self._queue) - 1, -1, -1):
                existing_op, existing = self._queue[index]
                if (
                    (
                        existing_op,
                        existing.get("conversation_id"),
                        existing.get("key_id"),
                        existing.get("project_key"),
                    )
                    == identity
                ):
                    self._queue[index] = (operation, dict(kwargs))
                    return True

        return False

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
        clean = self._drain_queue()
        pruned = 0

        try:
            if self._store is not None and self._retention_days > 0:
                pruned = self._store.prune_retention(self._retention_days)
        except Exception as exc:
            self._record_flush_error(exc)
            clean = False
        else:
            with self._lock:
                self._last_flush_at = time.time()
                self._flush_count += 1

        # The consecutive-failure streak must only reset when this pass
        # drained cleanly. The old reset lived in the prune ``else`` and
        # ran even when ``_drain_queue`` had retained a poison row, so the
        # >=5 warning never fired while failures kept incrementing (R3
        # live-validation fix).
        if clean:
            self._consecutive_flush_failures = 0

        relay_metrics.continuity_flushes.inc()
        relay_metrics.continuity_pruned.inc(pruned)
        with self._lock:
            self._pruned_total += pruned
        return pruned

    def _drain_queue(self) -> bool:
        """
        Apply every buffered row to the ConversationStore on this thread.
        Returns True when the pass applied every row without a retained
        failure.

        Idempotent-skips: a ``conversation.create`` that collides with an
        existing row (resumed across processes) and a ``turn.append`` whose
        ``(conversation_id, seq)`` row already exists (already-durable,
        e.g. a stale-seq turn from a restarted coordinator) are treated as
        already applied and drained -- one duplicate row must never stall
        the whole queue. Any other failure is counted, the row is retained
        at the head of the queue, and the drain stops so a transient store
        outage never loses un-flushed turns (P9e, audit §3.4).
        """
        drained = 0
        clean = True

        while True:
            with self._lock:
                if not self._queue:
                    break
                operation, kwargs = self._queue.popleft()

            method_name = _OP_METHODS.get(operation)

            if method_name is None:
                continue
            if self._store is None:
                self._retain_row(operation, kwargs)
                clean = False
                break

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
                if operation == "turn.append":
                    # Duplicate (conversation_id, seq): the turn is already
                    # durable -- a coordinator restarted at seq 1 collides
                    # with existing rows. Skip it and keep draining so a
                    # single poison row can never block durability for the
                    # entire project (R3 live-validation fix).
                    _logger.warning(
                        "continuity flush: skipping already-durable turn "
                        "append (conversation_id=%s seq=%s): %s",
                        kwargs.get("conversation_id"),
                        kwargs.get("seq"),
                        exc,
                    )
                    drained += 1
                    self._on_op_drained(kwargs.get("conversation_id"))
                    continue
                self._record_flush_error(exc)
                self._retain_row(operation, kwargs)
                clean = False
                break
            except MalformedInputError as exc:
                # N-4: malformed input (e.g. negative/float/bool tokens from
                # an untrusted provider) can never succeed on retry.  Drop
                # the row so one bad operation cannot poison the write-behind
                # queue and block later valid operations from being persisted.
                # ConversationStore raises ValueError exclusively for
                # validation failures -- never for infrastructure errors,
                # which retain their existing retry/error behavior above.
                _logger.warning(
                    "continuity flush: dropping malformed operation "
                    "%s (conversation_id=%s): %s",
                    operation,
                    kwargs.get("conversation_id"),
                    exc,
                )
                self._on_op_drained(kwargs.get("conversation_id"))
                continue
            except Exception as exc:
                self._record_flush_error(exc)
                self._retain_row(operation, kwargs)
                clean = False
                break

        with self._lock:
            self._drained_count += drained
            relay_metrics.continuity_rows_queued.set(len(self._queue))

        return clean

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
                # N-11: all rows for the conversation have drained, so
                # durable SQLite now reflects every accepted turn and the
                # in-memory pending-seq watermark is no longer needed.
                self._pending_seq = {
                    k: v
                    for k, v in self._pending_seq.items()
                    if k[0] != conversation_id
                }
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
                "rejected_total": self._rejected_count,
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
