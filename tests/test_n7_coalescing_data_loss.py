"""
N-7 regression: coalesced malformed accounting must not drop valid turns.

When a valid provisional turn.append is followed by a turn.update with
malformed accounting, coalescing merges the update into the append.  The
malformed accounting then causes the entire coalesced row to be dropped,
losing the valid provisional turn.
"""

import pytest

from app.services.conversation_store import ConversationStore
from app.services.continuity_flusher import ContinuityFlusher


def _store(tmp_path):
    return ConversationStore(str(tmp_path / "platform.db"))


def _create_conversation(store, cid="c" * 32, key_id="k"):
    return store.create(
        conversation_id=cid,
        key_id=key_id,
        client_bucket="cline",
        project_key="ab" * 16,
    )


class TestN7CoalescingDataLoss:
    """The exact Codex failure scenario."""

    def test_malformed_update_does_not_drop_valid_append(self, tmp_path):
        """Valid provisional append + malformed update → valid turn persists."""
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)

        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )

        # Step 1: valid provisional turn.append
        flusher.enqueue(
            "turn.append",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="denied",
            provider="openai",
            model="gpt-4",
        )
        assert flusher.queue_size == 1

        # Step 2: malformed turn.update (negative tokens)
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=-1,
            tokens_out=100,
        )
        # After coalescing, the queue should still have 1 entry
        # (the update was merged into the append).
        assert flusher.queue_size == 1

        # Step 3: flush
        flusher.flush()

        # Step 4: the valid turn must survive
        turns = store.turns(cid, "k")
        assert len(turns) == 1, (
            f"Valid provisional turn was lost! turns={turns}"
        )
        assert turns[0]["seq"] == 1

        # Step 5: malformed accounting must not be persisted
        assert turns[0]["tokens_in"] is None
        # tokens_out=100 is valid and should be preserved
        assert turns[0]["tokens_out"] == 100

        # Step 6: valid non-accounting fields must be preserved
        assert turns[0]["outcome"] == "ok"
        assert turns[0]["provider"] == "openai"
        assert turns[0]["model"] == "gpt-4"

        # Step 7: queue is clean
        assert flusher.queue_size == 0
        store.close()

    def test_malformed_update_does_not_drop_append_float_tokens(
        self, tmp_path
    ):
        """Valid append + float-token update → append survives."""
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )

        flusher.enqueue(
            "turn.append",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="denied",
        )
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=3.14,
        )

        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "ok"
        assert turns[0]["tokens_in"] is None
        assert flusher.queue_size == 0
        store.close()

    def test_malformed_update_does_not_drop_append_bool_tokens(
        self, tmp_path
    ):
        """Valid append + bool-token update → append survives."""
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )

        flusher.enqueue(
            "turn.append",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="denied",
        )
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=True,
            tokens_out=False,
        )

        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "ok"
        assert turns[0]["tokens_in"] is None
        assert turns[0]["tokens_out"] is None
        assert flusher.queue_size == 0
        store.close()

    def test_malformed_update_does_not_drop_append_string_tokens(
        self, tmp_path
    ):
        """Valid append + string-token update → append survives."""
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )

        flusher.enqueue(
            "turn.append",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="denied",
        )
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in="bad",
        )

        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "ok"
        assert turns[0]["tokens_in"] is None
        assert flusher.queue_size == 0
        store.close()


class TestN7ValidCoalescing:
    """Normal (valid) append+update coalescing must still work."""

    def test_valid_append_update_coalesces_correctly(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )

        flusher.enqueue(
            "turn.append",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="denied",
            provider="openai",
            model="gpt-4",
        )
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=100,
            tokens_out=200,
            latency_ms=500,
        )

        # Coalesced: one entry
        assert flusher.queue_size == 1

        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "ok"
        assert turns[0]["tokens_in"] == 100
        assert turns[0]["tokens_out"] == 200
        assert turns[0]["latency_ms"] == 500
        assert turns[0]["provider"] == "openai"
        assert turns[0]["model"] == "gpt-4"
        assert flusher.queue_size == 0
        store.close()

    def test_valid_zero_tokens_coalesce(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )

        flusher.enqueue(
            "turn.append",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="denied",
        )
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=0,
            tokens_out=0,
        )

        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "ok"
        assert turns[0]["tokens_in"] == 0
        assert turns[0]["tokens_out"] == 0
        store.close()

    def test_valid_none_tokens_coalesce(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )

        flusher.enqueue(
            "turn.append",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="denied",
        )
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=None,
            tokens_out=None,
        )

        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "ok"
        assert turns[0]["tokens_in"] is None
        store.close()


class TestN7QueueContinuation:
    """After a malformed coalesced update, later valid ops must persist."""

    def test_later_valid_operations_persist_after_malformed_coalesce(
        self, tmp_path
    ):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )

        # Turn 1: append + malformed update
        flusher.enqueue(
            "turn.append",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="denied",
        )
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=-1,
        )

        # Turn 2: valid append + valid update
        flusher.enqueue(
            "turn.append",
            conversation_id=cid,
            key_id="k",
            seq=2,
            outcome="denied",
        )
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=2,
            outcome="ok",
            tokens_in=50,
            tokens_out=100,
        )

        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 2
        # Turn 1: outcome updated, accounting stripped
        t1 = [t for t in turns if t["seq"] == 1][0]
        assert t1["outcome"] == "ok"
        assert t1["tokens_in"] is None
        # Turn 2: fully valid
        t2 = [t for t in turns if t["seq"] == 2][0]
        assert t2["outcome"] == "ok"
        assert t2["tokens_in"] == 50
        assert t2["tokens_out"] == 100
        assert flusher.queue_size == 0
        store.close()

    def test_other_conversations_unaffected(self, tmp_path):
        store = _store(tmp_path)
        cid_a = "a" * 32
        cid_b = "b" * 32
        _create_conversation(store, cid_a, key_id="ka")
        _create_conversation(store, cid_b, key_id="kb")
        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )

        # Conversation A: malformed append + update
        flusher.enqueue(
            "turn.append",
            conversation_id=cid_a,
            key_id="ka",
            seq=1,
            outcome="denied",
        )
        flusher.enqueue(
            "turn.update",
            conversation_id=cid_a,
            key_id="ka",
            seq=1,
            outcome="ok",
            tokens_in=-1,
        )

        # Conversation B: valid append + update
        flusher.enqueue(
            "turn.append",
            conversation_id=cid_b,
            key_id="kb",
            seq=1,
            outcome="denied",
        )
        flusher.enqueue(
            "turn.update",
            conversation_id=cid_b,
            key_id="kb",
            seq=1,
            outcome="ok",
            tokens_in=100,
            tokens_out=200,
        )

        flusher.flush()

        turns_a = store.turns(cid_a, "ka")
        assert len(turns_a) == 1
        assert turns_a[0]["outcome"] == "ok"
        assert turns_a[0]["tokens_in"] is None

        turns_b = store.turns(cid_b, "kb")
        assert len(turns_b) == 1
        assert turns_b[0]["tokens_in"] == 100
        assert flusher.queue_size == 0
        store.close()

    def test_in_flight_tracking_correct(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )

        flusher.enqueue(
            "turn.append",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="denied",
        )
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=True,
        )

        assert cid in store.in_flight

        flusher.flush()

        assert cid not in store.in_flight
        assert flusher.queue_size == 0
        store.close()


class TestN7InfrastructureStillRetries:
    """Genuine infrastructure failures must still retain/retry."""

    def test_os_error_still_retains_coalesced_row(
        self, tmp_path, monkeypatch
    ):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )

        flusher.enqueue(
            "turn.append",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="denied",
        )
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=100,
        )

        original = store.append_turn

        def flaky(**kwargs):
            raise OSError("disk error")

        monkeypatch.setattr(store, "append_turn", flaky)
        flusher.flush()

        # Infrastructure error: row retained.
        assert flusher.queue_size == 1
        stats = flusher.flush_stats()
        assert len(stats["flush_errors"]) == 1

        # Recovery.
        monkeypatch.setattr(store, "append_turn", original)
        flusher.flush()
        assert flusher.queue_size == 0
        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "ok"
        assert turns[0]["tokens_in"] == 100
        store.close()


class TestN7ExactCodexScenario:
    """End-to-end verification of the exact Codex failure scenario."""

    def test_codex_exact_scenario(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )

        # finish(outcome="denied") → valid provisional turn
        flusher.enqueue(
            "turn.append",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="denied",
            provider="openai",
            model="gpt-4",
        )

        # update(tokens_in=-1, ...) → malformed accounting
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=-1,
            tokens_out=100,
        )

        # flush()
        flusher.flush()

        # VALID PROVISIONAL TURN SURVIVES
        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["seq"] == 1

        # MALFORMED ACCOUNTING NOT PERSISTED
        assert turns[0]["tokens_in"] is None
        # tokens_out=100 is valid and preserved
        assert turns[0]["tokens_out"] == 100

        # CORRECT VALID FIELDS PRESERVED
        assert turns[0]["outcome"] == "ok"
        assert turns[0]["provider"] == "openai"
        assert turns[0]["model"] == "gpt-4"

        # QUEUE CONTINUES DRAINING
        assert flusher.queue_size == 0

        # NO INFINITE RETRY
        stats = flusher.flush_stats()
        assert stats["flush_errors"] == []

        # NO SILENT LOSS
        assert cid not in store.in_flight

        store.close()
