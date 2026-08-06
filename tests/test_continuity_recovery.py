"""
P9d recovery failure-injection and state-machine tests.

Covers the mandated scenarios from the P9d plan (clarification 7):

* crash before commit (request received, model never started);
* crash after partial stream (model started, only some output completed,
  turn committed with a resume point);
* failed provider switch (turn committed with outcome ``failed``);
* repeated resume attempts (replay cap);
* corrupted summary/state (summary ahead of turns, seq gaps/duplicates);
* prune while active (in-flight conversations are never pruned).

Plus the 7-state machine (valid + invalid transitions), resume-token
contracts (one-way hash, no raw token in the store), reconcile reporting,
and the coordinator wiring (token issuance per turn, hash attached on
commit, resume envelope seeding with ``exclude_up_to_seq``).
"""

import hashlib
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.models.continuity import RecoveryState
from app.services.continuity_headers import (
    ContinuityHeaderError,
    derive_resume_token_hash,
    validate_resume_token,
)
from app.services.continuity_flusher import ContinuityFlusher
from app.services.continuity_recovery import ContinuityRecovery
from app.services.conversation_store import ConversationStore
from app.services.handoff import HandoffCoordinator
from app.services.metrics import relay_metrics


class FakeFlusher:
    """Records enqueued operations for inspection (no durable writes)."""

    def __init__(self):
        self.enqueue_calls = []

    def enqueue(self, operation, **kwargs):
        self.enqueue_calls.append((operation, kwargs))


def _store(tmp_path):
    store = ConversationStore(str(tmp_path / "continuity.db"))
    return store


def _recovery(store, max_resume_replays=3):
    return ContinuityRecovery(store, max_resume_replays=max_resume_replays)


def _commit_turn(
    store,
    *,
    key_id="k",
    seq=1,
    outcome="ok",
    resume_token_hash=None,
    cid=None,
):
    cid = cid or "c" * 32
    if store.get(cid, key_id) is None:
        store.create(
            key_id=key_id,
            client_bucket="cli",
            project_key="pk",
            conversation_id=cid,
        )
    store.append_turn(
        conversation_id=cid,
        key_id=key_id,
        seq=seq,
        outcome=outcome,
        provider="p",
        model="m1",
        resume_token_hash=resume_token_hash,
    )
    return cid


@pytest.fixture(autouse=True)
def reset_metrics():
    relay_metrics.reset()
    yield
    relay_metrics.reset()


# ------------------------- resume token contract -------------------------


class TestResumeTokenContract:
    def test_validate_resume_token_bounds(self):
        assert validate_resume_token("a" * 128) == "a" * 128
        assert validate_resume_token("a" * 129) is None
        assert validate_resume_token(" ") is None
        assert validate_resume_token("") is None
        assert validate_resume_token("has\tcontrol") is None
        assert validate_resume_token(123) is None

    def test_derive_hash_is_one_way_and_stable(self):
        token = "0123456789abcdef0123456789abcdef"
        digest = derive_resume_token_hash(token)
        assert digest == hashlib.sha256(token.encode("utf-8")).hexdigest()
        assert digest != token
        assert derive_resume_token_hash(token) == digest
        assert derive_resume_token_hash("") is None

    def test_issue_returns_raw_but_store_only_has_hash(self):
        store = ConversationStore(":memory:")
        recovery = _recovery(store)
        raw = recovery.issue_resume_token("c" * 32, "k")
        assert raw and len(raw) == 32
        assert raw != recovery.pending_token_hash("c" * 32)
        # The raw token is never retained; only the hash is queryable.
        pending = recovery.pending_token_hash("c" * 32)
        assert pending == hashlib.sha256(raw.encode("utf-8")).hexdigest()
        store.close()

    def test_new_issuance_replaces_old_hash(self):
        store = ConversationStore(":memory:")
        recovery = _recovery(store)
        old_raw = recovery.issue_resume_token("c" * 32, "k")
        old_hash = recovery.pending_token_hash("c" * 32)
        recovery.issue_resume_token("c" * 32, "k")
        assert recovery.pending_token_hash("c" * 32) != old_hash
        assert derive_resume_token_hash(old_raw) != recovery.pending_token_hash(
            "c" * 32
        )
        store.close()


# ------------------------- state machine -------------------------


class TestRecoveryStateMachine:
    def test_all_seven_states_defined(self):
        assert [s.value for s in RecoveryState] == [
            "active",
            "interrupted",
            "recoverable",
            "recovery_in_progress",
            "recovered",
            "failed_recovery",
            "archived",
        ]

    def test_valid_resume_path(self):
        store = ConversationStore(":memory:")
        recovery = _recovery(store)
        cid = "c" * 32
        assert recovery.state(cid) == RecoveryState.ACTIVE.value

        recovery.on_turn_started(cid)
        assert recovery.state(cid) == RecoveryState.INTERRUPTED.value

        # Simulate a resume: valid token transitions to RECOVERY_IN_PROGRESS.
        recovery.transition(cid, "resume_valid")
        assert recovery.state(cid) == RecoveryState.RECOVERY_IN_PROGRESS.value

        recovery.on_turn_committed(cid)
        assert recovery.state(cid) == RecoveryState.RECOVERED.value

        recovery.on_turn_started(cid)
        assert recovery.state(cid) == RecoveryState.ACTIVE.value
        store.close()

    def test_invalid_transitions_are_rejected(self):
        store = ConversationStore(":memory:")
        recovery = _recovery(store)
        cid = "c" * 32

        # ACTIVE -> RECOVERY_IN_PROGRESS is invalid (a resume can only
        # succeed through INTERRUPTED or RECOVERABLE); state must not change.
        assert recovery.state(cid) == RecoveryState.ACTIVE.value
        assert recovery.transition(cid, "resume_valid") == (
            RecoveryState.ACTIVE.value
        )

        # Reach RECOVERY_IN_PROGRESS through the valid path, then verify
        # that RECOVERY_IN_PROGRESS cannot be interrupted (turn_start).
        recovery.transition(cid, "turn_start")
        recovery.transition(cid, "resume_valid")
        assert recovery.state(cid) == RecoveryState.RECOVERY_IN_PROGRESS.value
        assert recovery.transition(cid, "turn_start") == (
            RecoveryState.RECOVERY_IN_PROGRESS.value
        )

        # ARCHIVED is terminal.
        recovery.transition(cid, "archive")
        assert recovery.transition(cid, "turn_start") == (
            RecoveryState.ARCHIVED.value
        )
        store.close()


# ------------------------- failure injection: crash before commit -------------------------


class TestCrashBeforeCommit:
    def test_no_resume_point_is_never_a_blank_continue(self, tmp_path):
        store = _store(tmp_path)
        recovery = _recovery(store)
        cid = "c" * 32
        # A turn started in a previous process but never committed: the
        # durable store has the conversation but no turns at all.
        store.create(
            key_id="k", client_bucket="cli", project_key="pk",
            conversation_id=cid,
        )

        decision = recovery.validate_resume(cid, "k", "deadbeef" * 4)
        assert decision["valid"] is False
        assert decision["reason"] == "no_resume_point"
        assert recovery.state(cid) == RecoveryState.FAILED_RECOVERY.value
        assert relay_metrics.continuity_resume_denials.value() > 0
        store.close()

    def test_interrupted_turn_committed_without_token_denies(self, tmp_path):
        store = _store(tmp_path)
        recovery = _recovery(store)
        cid = _commit_turn(store, resume_token_hash=None)
        # The last committed turn carries no resume token -> no safe point.
        decision = recovery.validate_resume(cid, "k", "cafebabe" * 4)
        assert decision["valid"] is False
        assert decision["reason"] == "no_resume_token"
        store.close()


# ------------------------- failure injection: partial stream, then resume -------------------------


class TestPartialStreamThenResume:
    def test_resume_from_last_committed_turn_excludes_acknowledged(self, tmp_path):
        store = _store(tmp_path)
        recovery = _recovery(store)
        cid = _commit_turn(
            store,
            seq=1,
            resume_token_hash=derive_resume_token_hash("tok1"),
        )
        # Second committed turn carries the current resume token.
        _commit_turn(
            store,
            seq=2,
            resume_token_hash=derive_resume_token_hash("tok2"),
            cid=cid,
        )

        decision = recovery.validate_resume(cid, "k", "tok2")
        assert decision["valid"] is True
        assert decision["last_seq"] == 2
        # In the fresh-process flow a healthy conversation is marked
        # RECOVERABLE by the startup reconcile pass first; a valid resume
        # then moves it to RECOVERY_IN_PROGRESS. (A live process resume
        # follows ACTIVE -> INTERRUPTED via on_turn_started instead.)
        assert recovery.reconcile()["recoverable"] == 1
        assert recovery.state(cid) == RecoveryState.RECOVERABLE.value
        decision = recovery.validate_resume(cid, "k", "tok2")
        assert decision["valid"] is True
        assert recovery.state(cid) == RecoveryState.RECOVERY_IN_PROGRESS.value

        envelope = recovery.resume_envelope(cid, "k")
        assert envelope["last_seq"] == 2
        assert envelope["exclude_up_to_seq"] == 2
        assert envelope["last_turn"]["resume_token_hash"] == (
            derive_resume_token_hash("tok2")
        )
        store.close()

    def test_stale_token_denied_after_new_commit(self, tmp_path):
        store = _store(tmp_path)
        recovery = _recovery(store)
        cid = _commit_turn(
            store, seq=1,
            resume_token_hash=derive_resume_token_hash("old-token"),
        )
        # Next commit replaces the durable hash: old token is now dead.
        _commit_turn(
            store, seq=2, cid=cid,
            resume_token_hash=derive_resume_token_hash("new-token"),
        )
        decision = recovery.validate_resume(cid, "k", "old-token")
        assert decision["valid"] is False
        assert decision["reason"] == "token_mismatch"
        store.close()


# ------------------------- failure injection: failed provider switch -------------------------


class TestFailedProviderSwitch:
    def test_resume_denied_when_last_turn_failed(self, tmp_path):
        store = _store(tmp_path)
        recovery = _recovery(store)
        cid = _commit_turn(
            store,
            seq=1,
            outcome="failed",
            resume_token_hash=derive_resume_token_hash("tok"),
        )
        decision = recovery.validate_resume(cid, "k", "tok")
        assert decision["valid"] is False
        assert decision["reason"] == "last_turn_not_ok"
        assert recovery.state(cid) == RecoveryState.FAILED_RECOVERY.value
        store.close()

    def test_denied_resume_never_reaches_recovered(self, tmp_path):
        store = _store(tmp_path)
        recovery = _recovery(store)
        cid = _commit_turn(
            store,
            seq=1,
            outcome="denied",
            resume_token_hash=derive_resume_token_hash("tok"),
        )
        recovery.validate_resume(cid, "k", "tok")
        assert recovery.state(cid) == RecoveryState.FAILED_RECOVERY.value
        store.close()


# ------------------------- failure injection: repeated resume attempts -------------------------


class TestRepeatedResumeAttempts:
    def test_replay_cap_exhausts_after_max_attempts(self, tmp_path):
        store = _store(tmp_path)
        recovery = _recovery(store, max_resume_replays=3)
        cid = _commit_turn(
            store, seq=1,
            resume_token_hash=derive_resume_token_hash("tok"),
        )

        for _ in range(3):
            decision = recovery.validate_resume(cid, "k", "tok")
            assert decision["valid"] is True

        decision = recovery.validate_resume(cid, "k", "tok")
        assert decision["valid"] is False
        assert decision["reason"] == "replay_limit"
        assert relay_metrics.continuity_resumes.value() == 3
        store.close()

    def test_wrong_token_denies_without_exhausting_correct_token(self, tmp_path):
        store = _store(tmp_path)
        recovery = _recovery(store, max_resume_replays=1)
        cid = _commit_turn(
            store, seq=1,
            resume_token_hash=derive_resume_token_hash("good"),
        )

        # Replay history is keyed (conversation_id, token_hash): wrong-token
        # hammering is denied per-token and never consumes the correct
        # token's budget.
        for _ in range(2):
            decision = recovery.validate_resume(cid, "k", "wrong")
            assert decision["valid"] is False
            assert decision["reason"] == "token_mismatch"
        decision = recovery.validate_resume(cid, "k", "good")
        assert decision["valid"] is True
        store.close()

    def test_new_commit_resets_replay_history(self, tmp_path):
        store = _store(tmp_path)
        recovery = _recovery(store, max_resume_replays=1)
        cid = _commit_turn(
            store, seq=1,
            resume_token_hash=derive_resume_token_hash("tok"),
        )
        assert recovery.validate_resume(cid, "k", "tok")["valid"] is True
        # The exhausted replay history is dropped when a fresh token is
        # issued (new turn), and the old token is dead on commit anyway.
        raw = recovery.issue_resume_token(cid, "k")
        recovery.on_turn_committed(cid)
        _commit_turn(
            store, seq=2, cid=cid,
            resume_token_hash=derive_resume_token_hash(raw),
        )
        assert recovery.validate_resume(cid, "k", "tok")["valid"] is False
        assert recovery.validate_resume(cid, "k", raw)["valid"] is True
        store.close()


# ------------------------- failure injection: corrupted summary/state -------------------------


class TestCorruptedState:
    def _summary(self, store, cid, up_to_seq, content="ok"):
        store.record_summary(
            conversation_id=cid,
            key_id="k",
            up_to_seq=up_to_seq,
            version=1,
            method="test",
            content=content,
        )

    def test_summary_ahead_of_turns_is_anomaly(self, tmp_path):
        store = _store(tmp_path)
        recovery = _recovery(store)
        cid = _commit_turn(
            store, seq=1,
            resume_token_hash=derive_resume_token_hash("tok"),
        )
        self._summary(store, cid, up_to_seq=5)  # beyond committed seq 1

        report = recovery.reconcile()
        assert report["requires_review"] == 1
        kinds = {a["kind"] for a in report["anomalies"]}
        assert "summary_ahead_of_turns" in kinds
        assert recovery.state(cid) == RecoveryState.FAILED_RECOVERY.value
        store.close()

    def test_seq_gap_is_anomaly_and_flagged_review(self, tmp_path):
        store = _store(tmp_path)
        recovery = _recovery(store)
        cid = "c" * 32
        store.create(key_id="k", client_bucket="cli", project_key="pk",
                     conversation_id=cid)
        store.append_turn(conversation_id=cid, key_id="k", seq=1, outcome="ok")
        store.append_turn(conversation_id=cid, key_id="k", seq=3, outcome="ok")

        report = recovery.reconcile()
        assert report["requires_review"] == 1
        assert "seq_gap" in {a["kind"] for a in report["anomalies"]}
        assert recovery.state(cid) == RecoveryState.FAILED_RECOVERY.value
        store.close()

    def test_duplicate_seq_is_anomaly(self, tmp_path):
        store = _store(tmp_path)
        recovery = _recovery(store)
        cid = "c" * 32
        store.create(key_id="k", client_bucket="cli", project_key="pk",
                     conversation_id=cid)
        store.append_turn(conversation_id=cid, key_id="k", seq=1, outcome="ok")

        # A true duplicate cannot be inserted (UNIQUE constraint on
        # (conversation_id, seq)); the deterministic detector is exercised
        # directly on the corrupted sequence.
        anomalies = ContinuityRecovery._detect_anomalies(
            cid, seqs=[1, 1], summary=None
        )
        assert "duplicate_seq" in anomalies
        assert recovery.reconcile()["healthy"] == 1
        store.close()

    def test_healthy_recoverable_conversation(self, tmp_path):
        store = _store(tmp_path)
        recovery = _recovery(store)
        cid = _commit_turn(
            store, seq=1,
            resume_token_hash=derive_resume_token_hash("tok"),
        )
        report = recovery.reconcile()
        assert report["healthy"] == 1
        assert report["recoverable"] == 1
        assert report["requires_review"] == 0
        assert recovery.state(cid) == RecoveryState.RECOVERABLE.value
        store.close()


# ------------------------- failure injection: prune while active -------------------------


class TestPruneWhileActive:
    def test_in_flight_conversation_never_pruned(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        store.create(
            key_id="k", client_bucket="cli", project_key="pk",
            conversation_id=cid,
        )
        # Age the conversation past the window.
        store._require_open().execute(
            "UPDATE conversations SET updated_at = ?, last_turn_ts = ?"
            " WHERE id = ?",
            (1.0, 1.0, cid),
        )

        store.mark_in_flight(cid)
        assert store.prune_retention(days=1) == 0
        assert store.get(cid, "k") is not None

        store.clear_in_flight(cid)
        assert store.prune_retention(days=1) == 1
        assert store.get(cid, "k") is None
        store.close()

    def test_prune_preview_matches_actual_prune(self, tmp_path):
        store = _store(tmp_path)
        for i in range(3):
            cid = ("c" * 31) + str(i)
            store.create(
                key_id="k", client_bucket="cli", project_key="pk",
                conversation_id=cid,
            )
            store._require_open().execute(
                "UPDATE conversations SET updated_at = ?, last_turn_ts = ?"
                " WHERE id = ?",
                (1.0, 1.0, cid),
            )
        store.mark_in_flight(("c" * 31) + "0")

        preview = store.prune_preview(days=1)
        assert ("c" * 31) + "0" not in preview
        assert len(preview) == 2
        assert store.prune_retention(days=1) == 2
        store.close()


# ------------------------- coordinator wiring -------------------------


class TestCoordinatorRecoveryWiring:
    def test_every_turn_gets_a_fresh_token(self):
        flusher = FakeFlusher()
        recovery = ContinuityRecovery(ConversationStore(":memory:"))
        coord = HandoffCoordinator(flusher=flusher, recovery=recovery)

        t1 = coord.start(key_id="k", client_bucket="cli", project_key="pk")
        t2 = coord.start(key_id="k", client_bucket="cli", project_key="pk")
        assert t1.resume_token and t2.resume_token
        assert t1.resume_token != t2.resume_token

    def test_commit_attaches_resume_token_hash(self):
        flusher = FakeFlusher()
        recovery = ContinuityRecovery(ConversationStore(":memory:"))
        coord = HandoffCoordinator(flusher=flusher, recovery=recovery)

        turn = coord.start(key_id="k", client_bucket="cli", project_key="pk")
        assert turn.resume_token
        turn.finish(provider="p", model="m1")

        appends = [
            kwargs for op, kwargs in flusher.enqueue_calls
            if op == "turn.append"
        ]
        assert len(appends) == 1
        assert appends[0]["resume_token_hash"] == (
            derive_resume_token_hash(turn.resume_token)
        )

    def test_no_recovery_means_no_token_metadata(self):
        flusher = FakeFlusher()
        coord = HandoffCoordinator(flusher=flusher)
        turn = coord.start(key_id="k", client_bucket="cli", project_key="pk")
        assert turn.resume_token is None
        meta = turn.metadata()
        assert "resume_token" not in meta
        turn.finish(provider="p", model="m1")
        appends = [
            kwargs for op, kwargs in flusher.enqueue_calls
            if op == "turn.append"
        ]
        assert appends[0]["resume_token_hash"] is None

    def test_resumed_turn_seeds_envelope_and_excludes_acknowledged(self, tmp_path):
        store = _store(tmp_path)
        cid = _commit_turn(
            store, seq=1,
            resume_token_hash=derive_resume_token_hash("tok"),
        )
        store.record_summary(
            conversation_id=cid, key_id="k", up_to_seq=1, version=1,
            method="test", content="prior work done",
        )

        recovery = _recovery(store)
        envelope = recovery.resume_envelope(cid, "k")
        assert envelope is not None

        flusher = FakeFlusher()
        coord = HandoffCoordinator(flusher=flusher, recovery=recovery)
        turn = coord.start(
            key_id="k", client_bucket="cli", project_key="pk",
            conversation_id=cid, resume=envelope,
        )
        assert turn.resumed is True
        assert turn.exclude_up_to_seq == 1
        assert turn.resume_token  # a fresh token for the resumed turn

        rendered = turn.inject_message("continue")
        assert "prior work done" in rendered
        assert turn.metadata()["resumed"] is True
        assert turn.metadata()["exclude_up_to_seq"] == 1

        turn.finish(provider="p", model="m1")
        # The resumed turn commits at seq 2 with the NEW token hash; the
        # old token is durable-invalidated by the replacement.
        appends = [
            kwargs for op, kwargs in flusher.enqueue_calls
            if op == "turn.append"
        ]
        assert appends[0]["seq"] == 2
        assert appends[0]["resume_token_hash"] == (
            derive_resume_token_hash(turn.resume_token)
        )


# ------------------------- reconcile reporting -------------------------


class TestReconcile:
    def test_reconcile_reports_and_counts(self, tmp_path):
        store = _store(tmp_path)
        recovery = _recovery(store)
        _commit_turn(store, seq=1,
                     resume_token_hash=derive_resume_token_hash("tok"))

        report = recovery.reconcile()
        assert report["scanned"] >= 1
        assert report["healthy"] >= 1
        assert report["requires_review"] == 0
        assert relay_metrics.continuity_reconciliations.value() >= 1

    def test_reconcile_action_is_allowlisted(self):
        from app.services.event_log import EVENT_ACTIONS

        assert "continuity.reconcile" in EVENT_ACTIONS

    def test_archived_conversations_skip_anomaly_scan(self, tmp_path):
        store = _store(tmp_path)
        recovery = _recovery(store)
        cid = _commit_turn(store, seq=1, resume_token_hash="x")
        store.archive(cid, "k")
        report = recovery.reconcile()
        assert report["requires_review"] == 0
        assert recovery.state(cid) == RecoveryState.ARCHIVED.value
        store.close()
