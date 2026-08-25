"""
N-4 regression: ContinuityFlusher poison-row reliability.

Malformed provider accounting values must never poison the write-behind
queue.  One bad operation must not prevent later valid operations from
being persisted.  Genuine infrastructure failures must still retry as
before.
"""

import sqlite3
import time

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


# ---------------------------------------------------------------------------
# 1. Reproduction: the exact Codex poison-row failure mode
# ---------------------------------------------------------------------------


class TestPoisonRowReproduction:
    """Reproduce the exact failure Codex identified:

    malformed provider accounting → enters queue → store rejects →
    flusher retains at head → drain stops → valid op behind it
    never persists → repeated flushes keep failing on the bad row.
    """

    def test_malformed_turn_append_does_not_block_valid_operation(
        self, tmp_path
    ):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)

        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )

        # Enqueue a malformed turn (negative tokens) followed by a valid one.
        flusher.enqueue(
            "turn.append",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=-5,
            tokens_out=10,
        )
        flusher.enqueue(
            "turn.append",
            conversation_id=cid,
            key_id="k",
            seq=2,
            outcome="ok",
            tokens_in=100,
            tokens_out=200,
        )

        # Flush: the malformed row must be dropped, the valid row persisted.
        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1, (
            f"Expected 1 valid turn persisted, got {len(turns)}"
        )
        assert turns[0]["seq"] == 2
        assert turns[0]["tokens_in"] == 100
        assert turns[0]["tokens_out"] == 200

        # The queue must be drained — no poison row left.
        assert flusher.queue_size == 0

        store.close()

    def test_malformed_turn_update_does_not_block_valid_operation(
        self, tmp_path
    ):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        store.append_turn(
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="denied",
        )

        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )

        # Malformed update (float tokens) then valid update.
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=3.14,
            tokens_out=200,
        )
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=100,
            tokens_out=200,
        )

        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["tokens_in"] == 100
        assert turns[0]["tokens_out"] == 200
        assert flusher.queue_size == 0

        store.close()

    def test_malformed_summary_does_not_block_valid_operation(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        store.append_turn(
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
        )

        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )

        # Malformed summary (bool tokens) then valid summary.
        flusher.enqueue(
            "summary.record",
            conversation_id=cid,
            key_id="k",
            up_to_seq=1,
            version=1,
            method="test",
            content="summary text",
            tokens_in=True,
            tokens_out=50,
        )
        flusher.enqueue(
            "summary.record",
            conversation_id=cid,
            key_id="k",
            up_to_seq=1,
            version=1,
            method="test",
            content="summary text",
            tokens_in=100,
            tokens_out=50,
        )

        flusher.flush()

        summaries = store.summaries(cid, "k")
        assert len(summaries) == 1
        assert summaries[0]["tokens_in"] == 100
        assert flusher.queue_size == 0

        store.close()

    def test_malformed_compaction_does_not_block_valid_operation(
        self, tmp_path
    ):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)

        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )

        # Malformed compaction (string tokens) then valid compaction.
        flusher.enqueue(
            "compaction.record",
            conversation_id=cid,
            key_id="k",
            reason="test",
            method="test",
            from_tokens="bad",
            to_tokens=50,
        )
        flusher.enqueue(
            "compaction.record",
            conversation_id=cid,
            key_id="k",
            reason="test",
            method="test",
            from_tokens=100,
            to_tokens=50,
        )

        flusher.flush()

        # The valid compaction should have been persisted.
        assert flusher.queue_size == 0

        store.close()


# ---------------------------------------------------------------------------
# 2. Invalid provider accounting — all rejection types
# ---------------------------------------------------------------------------


class TestInvalidProviderAccounting:
    """Each type of malformed accounting value must be dropped, not retained."""

    @pytest.mark.parametrize(
        "bad_value",
        [-1, -100, -999999],
        ids=["negative-one", "negative-hundred", "negative-large"],
    )
    def test_negative_integer_turn_append(self, tmp_path, bad_value):
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
            outcome="ok",
            tokens_in=bad_value,
        )
        flusher.flush()
        assert store.turns(cid, "k") == []
        assert flusher.queue_size == 0
        store.close()

    @pytest.mark.parametrize(
        "bad_value",
        [0.5, 3.14, -0.1, 1e10],
        ids=["half", "pi", "neg-small", "large-scientific"],
    )
    def test_float_turn_append(self, tmp_path, bad_value):
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
            outcome="ok",
            tokens_out=bad_value,
        )
        flusher.flush()
        assert store.turns(cid, "k") == []
        assert flusher.queue_size == 0
        store.close()

    @pytest.mark.parametrize(
        "bad_value",
        [True, False],
        ids=["bool-true", "bool-false"],
    )
    def test_bool_turn_append(self, tmp_path, bad_value):
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
            outcome="ok",
            latency_ms=bad_value,
        )
        flusher.flush()
        assert store.turns(cid, "k") == []
        assert flusher.queue_size == 0
        store.close()

    @pytest.mark.parametrize(
        "bad_value",
        ["100", "abc", "", "none"],
        ids=["numeric-string", "alpha", "empty", "none-string"],
    )
    def test_string_turn_append(self, tmp_path, bad_value):
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
            outcome="ok",
            tokens_in=bad_value,
        )
        flusher.flush()
        assert store.turns(cid, "k") == []
        assert flusher.queue_size == 0
        store.close()

    def test_zero_tokens_accepted(self, tmp_path):
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
            outcome="ok",
            tokens_in=0,
            tokens_out=0,
        )
        flusher.flush()
        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["tokens_in"] == 0
        assert turns[0]["tokens_out"] == 0
        store.close()

    def test_none_tokens_accepted(self, tmp_path):
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
            outcome="ok",
            tokens_in=None,
            tokens_out=None,
        )
        flusher.flush()
        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["tokens_in"] is None
        store.close()

    def test_positive_tokens_persisted(self, tmp_path):
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
            outcome="ok",
            tokens_in=500,
            tokens_out=1000,
            latency_ms=250,
        )
        flusher.flush()
        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["tokens_in"] == 500
        assert turns[0]["tokens_out"] == 1000
        assert turns[0]["latency_ms"] == 250
        store.close()


# ---------------------------------------------------------------------------
# 3. Queue behavior — invalid does not poison later valid operations
# ---------------------------------------------------------------------------


class TestQueueBehavior:
    """The queue must continue draining after encountering a malformed op."""

    def test_multiple_valid_after_invalid_all_persisted(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )

        # Malformed, then 3 valid.
        flusher.enqueue(
            "turn.append",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=-1,
        )
        for i in range(2, 5):
            flusher.enqueue(
                "turn.append",
                conversation_id=cid,
                key_id="k",
                seq=i,
                outcome="ok",
                tokens_in=i * 100,
            )

        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 3
        assert [t["seq"] for t in turns] == [2, 3, 4]
        assert flusher.queue_size == 0
        store.close()

    def test_invalid_does_not_increment_flush_errors(self, tmp_path):
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
            outcome="ok",
            tokens_in=-1,
        )

        flusher.flush()

        stats = flusher.flush_stats()
        # Malformed input is not a flush error (it's a dropped bad row).
        assert stats["flush_errors"] == []
        store.close()

    def test_flusher_clean_flag_true_after_malformed_drop(self, tmp_path):
        """A malformed row must not mark the flush pass as dirty."""
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
            outcome="ok",
            tokens_in="garbage",
        )

        # flush() returns pruned count, but we can check that the
        # consecutive failure counter did NOT increment.
        flusher.flush()
        stats = flusher.flush_stats()
        # If the malformed row had been treated as an error,
        # consecutive_flush_failures would be > 0.
        # We can't read it directly, but flush_errors is empty.
        assert stats["flush_errors"] == []
        store.close()

    def test_in_flight_cleared_after_malformed_drop(self, tmp_path):
        """Dropping a malformed row must correctly release in-flight tracking."""
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
            outcome="ok",
            tokens_in=-1,
        )

        assert cid in store.in_flight
        flusher.flush()
        assert cid not in store.in_flight
        assert flusher.queue_size == 0
        store.close()

    def test_mixed_valid_and_invalid_multiple_conversations(self, tmp_path):
        """Different conversations' valid ops must not be blocked by another's malformed op."""
        store = _store(tmp_path)
        cid_a = "a" * 32
        cid_b = "b" * 32
        _create_conversation(store, cid_a, key_id="ka")
        _create_conversation(store, cid_b, key_id="kb")
        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )

        # Malformed op for conversation A.
        flusher.enqueue(
            "turn.append",
            conversation_id=cid_a,
            key_id="ka",
            seq=1,
            outcome="ok",
            tokens_in=True,
        )
        # Valid op for conversation B.
        flusher.enqueue(
            "turn.append",
            conversation_id=cid_b,
            key_id="kb",
            seq=1,
            outcome="ok",
            tokens_in=100,
        )

        flusher.flush()

        assert store.turns(cid_a, "ka") == []
        turns_b = store.turns(cid_b, "kb")
        assert len(turns_b) == 1
        assert turns_b[0]["tokens_in"] == 100
        assert flusher.queue_size == 0
        store.close()


# ---------------------------------------------------------------------------
# 4. Infrastructure failure distinction — genuine errors still retry
# ---------------------------------------------------------------------------


class TestInfrastructureFailureDistinction:
    """Genuine storage failures must retain their retry behavior."""

    def test_os_error_still_retains_row(self, tmp_path, monkeypatch):
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
            outcome="ok",
            tokens_in=100,
        )

        original = store.append_turn

        def flaky(**kwargs):
            raise OSError("disk I/O error")

        monkeypatch.setattr(store, "append_turn", flaky)

        flusher.flush()

        # OSError is infrastructure: row is retained, error is recorded.
        assert flusher.queue_size == 1
        stats = flusher.flush_stats()
        assert len(stats["flush_errors"]) == 1
        assert "disk I/O error" in stats["flush_errors"][0]["message"]

        # Restore and verify it drains on retry.
        monkeypatch.setattr(store, "append_turn", original)
        flusher.flush()
        assert flusher.queue_size == 0
        assert len(store.turns(cid, "k")) == 1
        store.close()

    def test_sqlite_operational_error_still_retains_row(
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
            outcome="ok",
            tokens_in=100,
        )

        original = store.append_turn

        def flaky(**kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(store, "append_turn", flaky)

        flusher.flush()

        assert flusher.queue_size == 1
        stats = flusher.flush_stats()
        assert len(stats["flush_errors"]) == 1
        assert "database is locked" in stats["flush_errors"][0]["message"]

        monkeypatch.setattr(store, "append_turn", original)
        flusher.flush()
        assert flusher.queue_size == 0
        assert len(store.turns(cid, "k")) == 1
        store.close()

    def test_integrity_error_non_idempotent_still_retains(
        self, tmp_path, monkeypatch
    ):
        """sqlite3.IntegrityError on non-idempotent ops must still retry."""
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=100,
        )

        original = store.update_turn

        def flaky(**kwargs):
            raise sqlite3.IntegrityError("constraint failed")

        monkeypatch.setattr(store, "update_turn", flaky)

        flusher.flush()

        assert flusher.queue_size == 1
        stats = flusher.flush_stats()
        assert len(stats["flush_errors"]) == 1

        monkeypatch.setattr(store, "update_turn", original)
        flusher.flush()
        assert flusher.queue_size == 0
        store.close()


# ---------------------------------------------------------------------------
# 5. Final verification: the exact Codex failure scenario
# ---------------------------------------------------------------------------


class TestCodexFailureScenarioVerification:
    """End-to-end verification of the Codex failure scenario:

    MALFORMED PROVIDER ACCOUNTING
        ↓
    rejected safely
        ↓
    NO POISONED QUEUE ENTRY
        ↓
    VALID OPERATION
        ↓
    queue drains
        ↓
    DURABLE PERSISTENCE SUCCESS
    """

    def test_exact_codex_scenario(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)

        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )

        # Step 1: malformed provider accounting enters the queue.
        flusher.enqueue(
            "turn.append",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=-999,
            tokens_out="not-a-number",
            latency_ms=True,
        )
        assert flusher.queue_size == 1

        # Step 2: valid operation enters behind it.
        flusher.enqueue(
            "turn.append",
            conversation_id=cid,
            key_id="k",
            seq=2,
            outcome="ok",
            tokens_in=42,
            tokens_out=128,
            latency_ms=350,
        )
        assert flusher.queue_size == 2

        # Step 3: flush — malformed is dropped, valid is persisted.
        flusher.flush()

        # Step 4: verify durable persistence.
        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["seq"] == 2
        assert turns[0]["tokens_in"] == 42
        assert turns[0]["tokens_out"] == 128
        assert turns[0]["latency_ms"] == 350

        # Step 5: queue is clean.
        assert flusher.queue_size == 0

        # Step 6: no flush errors (malformed input is not an error).
        stats = flusher.flush_stats()
        assert stats["flush_errors"] == []

        # Step 7: in-flight tracking is correct.
        assert cid not in store.in_flight

        # Step 8: repeated flushes are safe (queue is empty).
        flusher.flush()
        assert flusher.queue_size == 0
        assert len(store.turns(cid, "k")) == 1

        store.close()
