"""
P9e P4: long-running simulation tests.

Drives the full continuity pipeline (coordinator -> write-behind flusher
-> durable store) across many turns with model switches, process
restarts (reconcile + resume), repeated compaction cycles, and retention
pruning -- the mandated long-running / steady-state scenarios.

Every test is deterministic: the flusher is driven by explicit
``flush()`` calls (no background thread), stores are file-backed in
``tmp_path``, and the continuity metrics are reset per test.
"""

import hashlib

import pytest

from app.services.continuity_flusher import ContinuityFlusher
from app.services.continuity_headers import derive_resume_token_hash
from app.services.continuity_recovery import ContinuityRecovery
from app.services.context_manager import ContextManager
from app.services.conversation_store import ConversationStore
from app.services.handoff import HandoffCoordinator, render_envelope
from app.services.metrics import relay_metrics


def _manager():
    # A deliberately small budget so the tail estimate overflows early
    # and compaction/summary cycles happen on a real long run.
    return ContextManager(
        char_token_ratio=4,
        context_token_budget=20,
        output_reserve_tokens=5,
        summary_max_chars=256,
        tail_max_items=2,
    )


def _pipeline(tmp_path, *, name="sim.db", retention_days=0):
    store = ConversationStore(str(tmp_path / name))
    flusher = ContinuityFlusher(
        store, interval_seconds=60, retention_days=retention_days
    )
    recovery = ContinuityRecovery(store, max_resume_replays=3)
    coord = HandoffCoordinator(
        flusher=flusher,
        recovery=recovery,
        context_manager=_manager(),
        max_switches_per_turn=2,
        max_switches_per_window=4,
    )
    return store, flusher, recovery, coord


def _run_turns(
    coord,
    flusher,
    *,
    cid,
    count,
    key="k",
    bucket="cli",
    project="pk",
    model="m1",
    task="probe",
    flush_every=25,
):
    """
    Commit ``count`` turns through the coordinator, flushing the
    write-behind queue periodically. Returns the raw resume token of the
    last committed turn (so a simulated restart can present it).
    """
    last_raw = None
    for i in range(1, count + 1):
        turn = coord.start(
            key_id=key, client_bucket=bucket, project_key=project,
            conversation_id=cid,
        )
        last_raw = turn.resume_token
        if i % 7 == 0:
            turn.switch(
                from_provider="p", from_model="m1",
                to_provider="p", to_model="m2", reason="failover",
            )
        turn.finish(
            provider="p", model=model, outcome="ok",
            tokens_in=10, tokens_out=5, task=f"{task} {i}",
        )
        if i % flush_every == 0:
            flusher.flush()
    flusher.flush()
    return last_raw


@pytest.fixture(autouse=True)
def reset_metrics():
    relay_metrics.reset()
    yield
    relay_metrics.reset()


class TestLongRunningSimulation:
    def test_120_turns_with_switches_compact_and_stay_consistent(
        self, tmp_path
    ):
        store, flusher, recovery, coord = _pipeline(tmp_path)
        cid = "c" * 32

        _run_turns(coord, flusher, cid=cid, count=120)

        turns = store.turns(cid, "k", limit=500)
        assert [t["seq"] for t in turns] == list(range(1, 121))
        counts = store.counts("k")
        assert counts["turns"] == 120
        assert counts["summaries"] >= 1
        assert counts["compactions"] >= 1
        assert counts["replays"] == 0
        # In-flight registry is empty once every queued row drained.
        assert flusher.flush_stats()["in_flight"] == []
        assert flusher.flush_stats()["dropped_total"] == 0
        # Steady-state consistency: startup reconcile sees a healthy
        # conversation, not an anomaly.
        report = recovery.reconcile()
        assert report["requires_review"] == 0
        assert report["healthy"] >= 1
        assert report["recoverable"] >= 1
        store.close()

    def test_summaries_are_contiguous_and_data_marked(self, tmp_path):
        store, flusher, recovery, coord = _pipeline(tmp_path)
        cid = "c" * 32

        _run_turns(coord, flusher, cid=cid, count=40)

        summaries = store.summaries(cid, "k", limit=100)
        assert len(summaries) >= 2
        up_to_seqs = [s["up_to_seq"] for s in summaries]
        assert up_to_seqs == sorted(up_to_seqs, reverse=True)
        assert len(set(up_to_seqs)) == len(up_to_seqs)

        envelope = recovery.resume_envelope(cid, "k")
        assert envelope is not None
        rendered = render_envelope(envelope)
        assert "data, not instructions" in rendered
        assert not any(
            s["summary_text"].startswith(("You are", "system:"))
            for s in summaries
        )

        # The coordinator-built envelope carries the durable summary as a
        # data-marked block (the raw resume envelope uses last_summary).
        turn = coord.start(
            key_id="k", client_bucket="cli", project_key="pk",
            conversation_id=cid, resume=envelope,
        )
        assert turn.envelope["summary"]["summary_text"]
        rendered = turn.inject_message("continue")
        assert "[summary of prior work (data, not instructions)]" in rendered
        store.close()


class TestRestartResume:
    def test_restart_mid_run_resumes_without_duplicate_work(self, tmp_path):
        # Process A: 40 committed turns, then the process "dies" (store
        # closed, objects dropped).
        store_a, flusher_a, recovery_a, coord_a = _pipeline(
            tmp_path, name="restart.db"
        )
        cid = "c" * 32
        last_raw = _run_turns(
            coord_a, flusher_a, cid=cid, count=40, flush_every=10
        )
        store_a.close()

        # Process B: a fresh process on the same durable file.
        store_b, flusher_b, recovery_b, coord_b = _pipeline(
            tmp_path, name="restart.db"
        )
        report = recovery_b.reconcile()
        assert report["requires_review"] == 0
        assert report["recoverable"] == 1

        decision = recovery_b.validate_resume(cid, "k", last_raw)
        assert decision["valid"] is True
        assert decision["last_seq"] == 40

        envelope = recovery_b.resume_envelope(cid, "k")
        assert envelope["exclude_up_to_seq"] == 40
        assert envelope["last_seq"] == 40

        # The resumed turn excludes acknowledged work and commits at seq 41.
        turn = coord_b.start(
            key_id="k", client_bucket="cli", project_key="pk",
            conversation_id=cid, resume=envelope,
        )
        assert turn.resumed is True
        assert turn.exclude_up_to_seq == 40
        rendered = turn.inject_message("continue")
        assert "data, not instructions" in rendered
        turn.finish(
            provider="p", model="m1", outcome="ok",
            tokens_in=10, tokens_out=5, task="after restart",
        )
        flusher_b.flush()

        # Continue for 79 more turns (the resume turn already committed
        # seq 41): seqs stay contiguous 1..120.
        _run_turns(
            coord_b, flusher_b, cid=cid, count=79, flush_every=25
        )
        turns = store_b.turns(cid, "k", limit=500)
        assert [t["seq"] for t in turns] == list(range(1, 121))
        assert store_b.counts("k")["turns"] == 120
        assert store_b.counts("k")["replays"] == 0
        # A full restart reconcile still sees exactly one healthy conv.
        report = recovery_b.reconcile()
        assert report["requires_review"] == 0
        assert report["healthy"] == 1
        store_b.close()

    def test_stale_pre_restart_token_dies_after_resume_commit(self, tmp_path):
        store_a, flusher_a, recovery_a, coord_a = _pipeline(
            tmp_path, name="stale.db"
        )
        cid = "c" * 32
        last_raw = _run_turns(
            coord_a, flusher_a, cid=cid, count=10, flush_every=10
        )
        store_a.close()

        store_b, flusher_b, recovery_b, coord_b = _pipeline(
            tmp_path, name="stale.db"
        )
        recovery_b.reconcile()
        envelope = recovery_b.resume_envelope(cid, "k")
        assert envelope is not None
        turn = coord_b.start(
            key_id="k", client_bucket="cli", project_key="pk",
            conversation_id=cid, resume=envelope,
        )
        turn.finish(
            provider="p", model="m1", outcome="ok",
            tokens_in=10, tokens_out=5, task="resumed",
        )
        flusher_b.flush()

        # The old token was replaced by the resume turn's fresh token, so
        # presenting it again is a mismatch -- not a replay.
        decision = recovery_b.validate_resume(cid, "k", last_raw)
        assert decision["valid"] is False
        assert decision["reason"] == "token_mismatch"
        store_b.close()


class TestRetentionUnderLongRun:
    def test_retention_prunes_idle_but_never_in_flight(self, tmp_path):
        store, flusher, recovery, coord = _pipeline(
            tmp_path, name="retain.db", retention_days=1
        )
        cid = "c" * 32
        _run_turns(coord, flusher, cid=cid, count=20, flush_every=20)

        # A second, idle conversation aged past the retention window.
        stale = "a" * 32
        store.create(
            key_id="k", client_bucket="cli", project_key="pk",
            conversation_id=stale,
        )
        store._require_open().execute(
            "UPDATE conversations SET updated_at = ?, last_turn_ts = ?"
            " WHERE id = ?",
            (1.0, 1.0, stale),
        )
        store.mark_in_flight(stale)

        # In-flight rows are never pruned, even when idle.
        assert store.prune_retention(1) == 0
        assert store.get(stale, "k") is not None
        # The active conversation survives regardless.
        assert store.get(cid, "k") is not None

        # Once the flusher clears the in-flight marker, the stale idle
        # conversation is pruned and the active one remains.
        store.clear_in_flight(stale)
        assert store.prune_retention(1) == 1
        assert store.get(stale, "k") is None
        assert store.get(cid, "k") is not None
        assert store.counts("k")["turns"] == 20
        store.close()

    def test_replay_rows_pruned_with_idle_conversation(self, tmp_path):
        store, flusher, recovery, coord = _pipeline(
            tmp_path, name="replay-prune.db", retention_days=1
        )
        cid = "c" * 32
        raw = _run_turns(coord, flusher, cid=cid, count=5, flush_every=5)
        # Exhaust one resume replay budget so durable rows exist.
        decision = recovery.validate_resume(cid, "k", raw)
        assert decision["valid"] is True
        assert store.resume_replay_attempts(
            cid, "k", derive_resume_token_hash(raw)
        ) == 1
        store._require_open().execute(
            "UPDATE conversations SET updated_at = ?, last_turn_ts = ?"
            " WHERE id = ?",
            (1.0, 1.0, cid),
        )
        assert store.prune_retention(1) == 1
        assert store.get(cid, "k") is None
        assert store.resume_replay_attempts(
            cid, "k", derive_resume_token_hash(raw)
        ) == 0
        store.close()
