"""
P9d recovery, retention, and operator-visibility service.

``ContinuityRecovery`` owns the P9d recovery state machine
(``RecoveryState``), resume-token issuance/validation (one-way SHA-256
hashes only), the durable replay counter (schema v8 ``resume_replays``),
and the startup reconciliation pass. It is additive on top of the P9c
coordinator: ``HandoffCoordinator`` asks it for a fresh resume token per
turn and reports ``turn_started`` / ``turn_committed`` events; the relay
facade asks it to validate a presented token and to hydrate the durable
resume envelope.

Boundary notes:

* No new rows are ever written by this service except the best-effort
  ``continuity.reconcile`` audit event and the v8 ``resume_replays``
  attempt counter (written synchronously from ``validate_resume``, the
  single sanctioned resume path). Tokens are stored as hashes only,
  attached to the next committed turn via the existing
  ``conversation_turns.resume_token`` column (P9a schema v7, no
  migration). Durable single-use comes from replacing the hash on the
  next commit: validation always compares against the *latest* committed
  turn, so a consumed token dies once a new turn commits.
* Resume validation and envelope hydration perform bounded, key-scoped
  single-row reads on the request path (the resume decision must be made
  before the model runs). This is the one deliberate exception to the
  "SQLite never on chat paths" rule; it is read-only and bounded.
* Recovery state is derived and process-local: it is rebuilt on startup by
  ``reconcile()``. Nothing here claims that unfinished work completed.
* The replay counter is durable (P9e): replay attempts survive a process
  restart because they are tracked in ``resume_replays``, keyed by
  ``(conversation_id, token_hash)``, and cleared only when a new token is
  issued or a turn commits.

Recovery-confidence gating (clarification 1): a resume is only valid when
the last committed turn exists, carries a resume-token hash, and has
outcome ``ok``. When the last safe point cannot be determined the
conversation is marked ``FAILED_RECOVERY`` (requires recovery review);
continuity never blindly continues and never pretends unfinished work was
completed.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from typing import Optional

from app.models.continuity import RecoveryState
from app.services.metrics import relay_metrics

_logger = logging.getLogger("relay")

# Bounded scan for the startup reconciliation pass so an operator task can
# never walk the whole table without limit (mirrors the store's read
# ceilings).
_SCAN_LIMIT = 5000

# Bounded number of anomaly entries surfaced in a reconcile report.
_REPORT_ANOMALY_CAP = 100


class ContinuityRecovery:
    """
    Recovery state machine, resume tokens, replay cap, and reconcile.

    Injectable and inert until wired to a ``ConversationStore``. Never
    raises on the chat path: every public method degrades to a safe
    denial / no-op.
    """

    def __init__(
        self,
        store=None,
        *,
        max_resume_replays: Optional[int] = None,
        scan_limit: int = _SCAN_LIMIT,
    ) -> None:
        from app.core.config import settings

        self._store = store
        self._max_resume_replays = max(
            0,
            int(
                max_resume_replays
                if max_resume_replays is not None
                else settings.max_resume_replays
            ),
        )
        self._scan_limit = max(1, int(scan_limit))
        self._lock = threading.Lock()
        # conversation_id -> RecoveryState (derived, process-local).
        self._states: dict = {}
        # conversation_id -> latest issued-but-uncommitted token hash.
        self._pending_tokens: dict = {}

    # ------------------------- state machine -------------------------

    # Event -> next state per current state. Anything absent is invalid and
    # leaves the state unchanged (documented in models.continuity).
    _TRANSITIONS = {
        RecoveryState.ACTIVE: {
            "turn_start": RecoveryState.INTERRUPTED,
            "turn_committed": RecoveryState.ACTIVE,
            # A denied resume on an active conversation means the last safe
            # point could not be determined: requires recovery review.
            "resume_denied": RecoveryState.FAILED_RECOVERY,
            "archive": RecoveryState.ARCHIVED,
        },
        RecoveryState.INTERRUPTED: {
            "turn_committed": RecoveryState.ACTIVE,
            "resume_valid": RecoveryState.RECOVERY_IN_PROGRESS,
            "resume_denied": RecoveryState.FAILED_RECOVERY,
            "archive": RecoveryState.ARCHIVED,
        },
        RecoveryState.RECOVERABLE: {
            "resume_valid": RecoveryState.RECOVERY_IN_PROGRESS,
            "resume_denied": RecoveryState.FAILED_RECOVERY,
            "turn_start": RecoveryState.ACTIVE,
            "turn_committed": RecoveryState.ACTIVE,
            "archive": RecoveryState.ARCHIVED,
        },
        RecoveryState.RECOVERY_IN_PROGRESS: {
            "turn_committed": RecoveryState.RECOVERED,
            "archive": RecoveryState.ARCHIVED,
        },
        RecoveryState.RECOVERED: {
            "turn_start": RecoveryState.ACTIVE,
            "turn_committed": RecoveryState.ACTIVE,
            "archive": RecoveryState.ARCHIVED,
        },
        RecoveryState.FAILED_RECOVERY: {
            "resume_valid": RecoveryState.RECOVERY_IN_PROGRESS,
            "turn_start": RecoveryState.ACTIVE,
            "turn_committed": RecoveryState.ACTIVE,
            "archive": RecoveryState.ARCHIVED,
        },
        RecoveryState.ARCHIVED: {},
    }

    def state(self, conversation_id: str) -> str:
        """Current recovery state for a conversation (default ACTIVE)."""
        if not conversation_id:
            return RecoveryState.ACTIVE.value
        with self._lock:
            return self._states.get(
                conversation_id, RecoveryState.ACTIVE
            ).value

    def transition(
        self, conversation_id: str, event: str
    ) -> str:
        """
        Apply one state-machine transition. Invalid transitions are
        rejected (state unchanged) and never raise. Returns the state
        (as a string) after the attempt.
        """
        if not conversation_id:
            return RecoveryState.ACTIVE.value
        with self._lock:
            current = self._states.get(
                conversation_id, RecoveryState.ACTIVE
            )
            next_state = self._TRANSITIONS.get(current, {}).get(event)
            if next_state is None:
                return current.value
            self._states[conversation_id] = next_state
            return next_state.value

    def on_archive(self, conversation_id: str) -> None:
        """Record an archive event (diagnostics only)."""
        if conversation_id:
            self.transition(conversation_id, "archive")

    # ------------------------- resume tokens -------------------------

    def issue_resume_token(
        self, conversation_id: str, key_id: str
    ) -> Optional[str]:
        """
        Issue a fresh one-time resume token (uuid4 hex) and record its
        one-way SHA-256 hash as pending for the conversation's next
        commit. Returns the raw token exactly once to the caller; only the
        hash is ever retained. None when no conversation id is given.
        """
        if not conversation_id:
            return None
        raw = uuid.uuid4().hex
        token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        with self._lock:
            self._pending_tokens[conversation_id] = token_hash
        # A new issuance ends the previous token's lifecycle: reset its
        # durable replay budget (best-effort, never raises).
        self._clear_replay_best_effort(conversation_id, key_id)
        return raw

    def pending_token_hash(self, conversation_id: str) -> Optional[str]:
        """Hash of the latest issued-but-uncommitted token (for commit)."""
        if not conversation_id:
            return None
        with self._lock:
            return self._pending_tokens.get(conversation_id)

    def _clear_replay_best_effort(
        self, conversation_id: str, key_id: Optional[str] = None
    ) -> None:
        if self._store is None or not conversation_id:
            return
        try:
            self._store.clear_resume_replay(conversation_id, key_id)
        except Exception:  # noqa: BLE001 - recovery never raises
            _logger.debug(
                "durable replay clear failed for %s", conversation_id
            )

    # ------------------------- resume validation -------------------------

    def validate_resume(
        self, conversation_id: str, key_id: str, raw_token: Optional[str]
    ) -> dict:
        """
        Validate a presented resume token for one conversation.

        Decision dict: ``{"attempted", "valid", "reason", "state",
        "last_seq"}``. A valid resume transitions the conversation to
        RECOVERY_IN_PROGRESS. Denials increment the resume-denials metric.
        Never raises; a store read failure degrades to a denial.
        """
        from app.services.continuity_headers import (
            derive_resume_token_hash,
        )

        if not conversation_id:
            return self._deny(
                conversation_id, "no_conversation", attempted=True
            )

        # The durable last turn is read up front so every decision carries
        # the authoritative ``last_seq``. A denied resume must still let a
        # later turn continue the conversation at ``last_seq + 1`` -- a
        # blank ``next_seq=1`` on an existing conversation would collide
        # with the ``UNIQUE (conversation_id, seq)`` constraint on the
        # first append and block the whole write-behind flusher (R3 live
        # validation fix).
        try:
            last = (
                self._store.last_turn(conversation_id, key_id)
                if self._store
                else None
            )
        except Exception:  # noqa: BLE001 - recovery never breaks chat
            last = None
        last_seq = last["seq"] if last else None

        if not raw_token:
            return self._deny(
                conversation_id,
                "no_token",
                attempted=False,
                last_seq=last_seq,
            )

        token_hash = derive_resume_token_hash(raw_token)
        if token_hash is None:
            return self._deny(
                conversation_id,
                "malformed_token",
                attempted=True,
                last_seq=last_seq,
            )

        # Confidence gating: no last safe point -> requires recovery
        # review, never a blind continue.
        if last is None:
            self.transition(conversation_id, "resume_denied")
            return self._deny(
                conversation_id,
                "no_resume_point",
                attempted=True,
                last_seq=last_seq,
            )
        if not last.get("resume_token_hash"):
            self.transition(conversation_id, "resume_denied")
            return self._deny(
                conversation_id,
                "no_resume_token",
                attempted=True,
                last_seq=last_seq,
            )
        if last.get("outcome") != "ok":
            self.transition(conversation_id, "resume_denied")
            return self._deny(
                conversation_id,
                "last_turn_not_ok",
                attempted=True,
                last_seq=last_seq,
            )

        if last["resume_token_hash"] != token_hash:
            return self._deny(
                conversation_id,
                "token_mismatch",
                attempted=True,
                last_seq=last_seq,
            )

        # Durable replay counter (P9e): the attempt is recorded in
        # resume_replays before it is honored, so the cap survives a
        # process restart. Fail-closed when the counter cannot be
        # persisted: an untracked token is never honored.
        if self._store is None:
            return self._deny(
                conversation_id,
                "store_unavailable",
                attempted=True,
                last_seq=last_seq,
            )
        try:
            attempts = self._store.record_resume_replay_attempt(
                conversation_id, key_id, token_hash
            )
        except Exception:  # noqa: BLE001 - recovery never breaks chat
            return self._deny(
                conversation_id,
                "replay_store_unavailable",
                attempted=True,
                last_seq=last_seq,
            )

        if attempts > self._max_resume_replays:
            return self._deny(
                conversation_id,
                "replay_limit",
                attempted=True,
                last_seq=last_seq,
            )

        self.transition(conversation_id, "resume_valid")
        relay_metrics.continuity_resumes.inc()
        return {
            "attempted": True,
            "valid": True,
            "reason": "",
            "state": self.state(conversation_id),
            "last_seq": last["seq"],
        }

    def _deny(
        self,
        conversation_id: str,
        reason: str,
        *,
        attempted: bool,
        last_seq: Optional[int] = None,
    ) -> dict:
        # Only an actual resume attempt counts as a denial; an un-attempted
        # validation (e.g. no token presented on a normal turn) must not
        # inflate the denial metric.
        if attempted:
            relay_metrics.continuity_resume_denials.inc()
        return {
            "attempted": attempted,
            "valid": False,
            "reason": reason,
            "state": self.state(conversation_id),
            "last_seq": last_seq,
        }

    # ------------------------- resume envelope -------------------------

    def durable_last_seq(
        self, conversation_id: str, key_id: str
    ) -> Optional[int]:
        """
        Best-effort durable max seq for a conversation: the seq of the last
        committed turn, or None when the conversation has no durable turns
        or the store is unavailable. Used to seed a fresh coordinator state
        at ``last_seq + 1`` so a post-restart conversation never restarts at
        seq 1 and collides with ``UNIQUE (conversation_id, seq)``. Never
        raises.
        """
        if not conversation_id or self._store is None:
            return None
        try:
            last = self._store.last_turn(conversation_id, key_id)
        except Exception:  # noqa: BLE001 - recovery never breaks chat
            return None
        return last["seq"] if last else None

    def resume_envelope(
        self, conversation_id: str, key_id: str
    ) -> Optional[dict]:
        """
        Hydrate the durable resume envelope for a validated resume:
        the last committed turn, the latest summary (highest ``up_to_seq``),
        and ``exclude_up_to_seq`` so already-acknowledged turns are never
        repeated in the next handoff envelope (duplicate-work prevention).
        None when the store has no resume point or is unavailable.
        """
        if not conversation_id or self._store is None:
            return None
        try:
            last = self._store.last_turn(conversation_id, key_id)
            if last is None:
                return None
            summary = self._store.last_summary(conversation_id, key_id)
            return {
                "conversation_id": conversation_id,
                "last_seq": last["seq"],
                "last_turn": dict(last),
                "last_summary": dict(summary) if summary else None,
                "exclude_up_to_seq": last["seq"],
            }
        except Exception:  # noqa: BLE001 - recovery never breaks chat
            return None

    # ------------------------- coordinator events -------------------------

    def on_turn_started(self, conversation_id: str) -> None:
        """A new turn began (in-memory in-flight marker, S4d)."""
        self.transition(conversation_id, "turn_start")

    def on_turn_committed(
        self, conversation_id: str, key_id: Optional[str] = None
    ) -> None:
        """
        A turn committed. The pending token (if any) is now durable on the
        latest turn, so an old token becomes invalid (single-use). Clears
        the pending token and the durable replay history for the
        conversation (best-effort).
        """
        self.transition(conversation_id, "turn_committed")
        if not conversation_id:
            return
        with self._lock:
            self._pending_tokens.pop(conversation_id, None)
        self._clear_replay_best_effort(conversation_id, key_id)

    # ------------------------- reconcile -------------------------

    def reconcile(self) -> dict:
        """
        Startup reconciliation pass: scan active conversations, detect seq
        gaps / duplicates and summary-ahead-of-turns anomalies, and report
        them without repairing anything. Conversations whose last safe
        point is undeterminable are marked FAILED_RECOVERY (recovery
        review). Emits one additive ``continuity.reconcile`` audit event.
        Never raises; a store outage produces an error report.
        """
        started = time.time()
        report = {
            "scanned": 0,
            "healthy": 0,
            "recoverable": 0,
            "requires_review": 0,
            "anomalies": [],
            "ts": started,
        }

        try:
            conversations = (
                self._store.list(limit=self._scan_limit) if self._store else []
            )
        except Exception as exc:  # noqa: BLE001 - reconcile never breaks
            _logger.warning("continuity reconcile unavailable: %s", exc)
            return {**report, "error": str(exc)}

        for conversation in conversations:
            cid = conversation["id"]
            report["scanned"] += 1

            if conversation.get("status") == "archived":
                self.transition(cid, "archive")
                continue

            try:
                seqs = (
                    self._store.turn_seqs(cid, conversation["key_id"])
                    if self._store
                    else []
                )
                summary = (
                    self._store.last_summary(cid, conversation["key_id"])
                    if self._store
                    else None
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "continuity reconcile read failed for %s: %s", cid, exc
                )
                report["requires_review"] += 1
                self._append_anomaly(
                    report, cid, "read_failed", str(exc)
                )
                self.transition(cid, "resume_denied")
                continue

            anomalies = self._detect_anomalies(cid, seqs, summary)
            if anomalies:
                report["requires_review"] += 1
                for anomaly in anomalies:
                    self._append_anomaly(report, cid, anomaly, "")
                self.transition(cid, "resume_denied")
                continue

            report["healthy"] += 1
            if seqs:
                try:
                    last = self._store.last_turn(cid, conversation["key_id"])
                except Exception:  # noqa: BLE001
                    last = None
                if last and last.get("resume_token_hash"):
                    self._states[cid] = RecoveryState.RECOVERABLE
                    report["recoverable"] += 1
                    continue
            self._states[cid] = RecoveryState.ACTIVE

        relay_metrics.continuity_reconciliations.inc()
        self._emit_reconcile_audit(report)
        return report

    @staticmethod
    def _detect_anomalies(cid: str, seqs: list, summary: Optional[dict]) -> list:
        """Deterministic anomaly detection for one conversation."""
        anomalies: list = []
        if not seqs:
            return anomalies

        duplicates = len(seqs) != len(set(seqs))
        if duplicates:
            anomalies.append("duplicate_seq")
        if seqs[0] != 1:
            anomalies.append("first_seq_not_one")
        expected = list(range(1, seqs[-1] + 1))
        if sorted(seqs) != expected:
            anomalies.append("seq_gap")

        if summary and summary["up_to_seq"] > seqs[-1]:
            anomalies.append("summary_ahead_of_turns")

        return anomalies

    @staticmethod
    def _append_anomaly(report: dict, cid: str, kind: str, detail: str) -> None:
        if len(report["anomalies"]) < _REPORT_ANOMALY_CAP:
            report["anomalies"].append(
                {"conversation_id": cid, "kind": kind, "detail": detail}
            )

    def _emit_reconcile_audit(self, report: dict) -> None:
        from app.services import event_log as event_log_module

        try:
            event_log_module.event_log().emit(
                "continuity.reconcile",
                actor="system",
                target="conversations",
                outcome="ok",
                detail={
                    "scanned": report["scanned"],
                    "healthy": report["healthy"],
                    "recoverable": report["recoverable"],
                    "requires_review": report["requires_review"],
                    "anomaly_count": len(report["anomalies"]),
                    "ts": report["ts"],
                },
            )
        except Exception:  # noqa: BLE001 - audit must never break
            _logger.debug("continuity.reconcile audit row failed")


__all__ = ["ContinuityRecovery"]
