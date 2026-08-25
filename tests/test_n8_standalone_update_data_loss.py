"""
N-8 regression: standalone malformed turn.update must not erase durable state.

When a durable turn already exists with valid accounting, a standalone
malformed turn.update must not:
  * corrupt in-memory committed-turn state;
  * overwrite durable accounting with NULL;
  * alter the durable outcome;
  * drain silently without error.

Two fix layers are tested:
  1. HandoffCoordinator.update() rejects malformed accounting before
     any state mutation (in-memory contamination prevention).
  2. ConversationStore.update_turn() preserves existing durable
     accounting when None is passed (F-5: no NULL overwrite).
"""

import pytest

from app.services.conversation_store import (
    ConversationStore,
    MalformedInputError,
    _validate_non_negative_int,
)
from app.services.continuity_flusher import ContinuityFlusher
from app.services.handoff import HandoffCoordinator
from app.services.context_manager import ContextManager


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _store(tmp_path):
    return ConversationStore(str(tmp_path / "platform.db"))


def _create_conversation(store, cid="c" * 32, key_id="k"):
    return store.create(
        conversation_id=cid,
        key_id=key_id,
        client_bucket="cline",
        project_key="ab" * 16,
    )


class _FakeFlusher:
    def __init__(self):
        self.enqueue_calls = []

    def enqueue(self, operation, **kwargs):
        self.enqueue_calls.append((operation, kwargs))
        return True


def _coordinator(flusher=None):
    return HandoffCoordinator(
        flusher=flusher if flusher is not None else _FakeFlusher(),
        context_manager=ContextManager(
            char_token_ratio=4,
            context_token_budget=2048,
            output_reserve_tokens=128,
        ),
    )


def _operations(flusher, operation):
    return [kwargs for op, kwargs in flusher.enqueue_calls if op == operation]


def _make_durable_turn(store, cid, seq=1, outcome="denied", tokens_in=10,
                       tokens_out=20, latency_ms=30, key_id="k"):
    """Create a durable turn with valid accounting."""
    store.append_turn(
        conversation_id=cid,
        key_id=key_id,
        seq=seq,
        outcome=outcome,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# A. Existing durable turn + malformed accounting (all types)
# ---------------------------------------------------------------------------


class TestN8ExistingDurableTurnMalformedAccounting:
    """A standalone malformed update must not erase valid durable state."""

    @pytest.mark.parametrize(
        "bad_value",
        [-1, -100, -999999],
        ids=["neg-one", "neg-hundred", "neg-large"],
    )
    def test_negative_int_tokens_preserved(self, tmp_path, bad_value):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        _make_durable_turn(store, cid)

        flusher = ContinuityFlusher(store, interval_seconds=60, retention_days=0)
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=bad_value,
        )
        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "denied"
        assert turns[0]["tokens_in"] == 10
        assert turns[0]["tokens_out"] == 20
        assert turns[0]["latency_ms"] == 30
        assert flusher.queue_size == 0
        store.close()

    @pytest.mark.parametrize(
        "bad_value",
        [0.5, 3.14, -0.1],
        ids=["half", "pi", "neg-small"],
    )
    def test_float_tokens_preserved(self, tmp_path, bad_value):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        _make_durable_turn(store, cid)

        flusher = ContinuityFlusher(store, interval_seconds=60, retention_days=0)
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_out=bad_value,
        )
        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["tokens_out"] == 20
        assert flusher.queue_size == 0
        store.close()

    @pytest.mark.parametrize(
        "bad_value",
        [True, False],
        ids=["bool-true", "bool-false"],
    )
    def test_bool_tokens_preserved(self, tmp_path, bad_value):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        _make_durable_turn(store, cid)

        flusher = ContinuityFlusher(store, interval_seconds=60, retention_days=0)
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            latency_ms=bad_value,
        )
        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["latency_ms"] == 30
        assert flusher.queue_size == 0
        store.close()

    @pytest.mark.parametrize(
        "bad_value",
        ["100", "abc", "", "none"],
        ids=["numeric-string", "alpha", "empty", "none-string"],
    )
    def test_string_tokens_preserved(self, tmp_path, bad_value):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        _make_durable_turn(store, cid)

        flusher = ContinuityFlusher(store, interval_seconds=60, retention_days=0)
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=bad_value,
        )
        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["tokens_in"] == 10
        assert turns[0]["tokens_out"] == 20
        assert turns[0]["latency_ms"] == 30
        assert flusher.queue_size == 0
        store.close()

    def test_all_malformed_fields_preserved(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        _make_durable_turn(store, cid)

        flusher = ContinuityFlusher(store, interval_seconds=60, retention_days=0)
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=-1,
            tokens_out="bad",
            latency_ms=False,
        )
        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "denied"
        assert turns[0]["tokens_in"] == 10
        assert turns[0]["tokens_out"] == 20
        assert turns[0]["latency_ms"] == 30
        assert flusher.queue_size == 0
        store.close()


# ---------------------------------------------------------------------------
# B. Existing durable turn + malformed accounting + valid non-accounting update
# ---------------------------------------------------------------------------


class TestN8MalformedAccountingWithValidOutcome:
    """Malformed accounting must not prevent the outcome from being updated
    IF the accounting is rejected and the update is re-submitted with valid
    accounting.  But a single update with malformed accounting should be
    entirely rejected — both accounting AND outcome change."""

    def test_malformed_accounting_rejects_entire_update(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        _make_durable_turn(store, cid)

        flusher = ContinuityFlusher(store, interval_seconds=60, retention_days=0)
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=-1,
        )
        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "denied"
        assert turns[0]["tokens_in"] == 10
        store.close()

    def test_valid_accounting_updates_durable_state(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        _make_durable_turn(store, cid)

        flusher = ContinuityFlusher(store, interval_seconds=60, retention_days=0)
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
        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "ok"
        assert turns[0]["tokens_in"] == 100
        assert turns[0]["tokens_out"] == 200
        assert turns[0]["latency_ms"] == 500
        assert flusher.queue_size == 0
        store.close()


# ---------------------------------------------------------------------------
# C. Existing durable turn + valid accounting update
# ---------------------------------------------------------------------------


class TestN8ValidAccountingUpdate:
    """Valid accounting must still update correctly."""

    def test_valid_update_all_fields(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        _make_durable_turn(store, cid)

        flusher = ContinuityFlusher(store, interval_seconds=60, retention_days=0)
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
        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "ok"
        assert turns[0]["tokens_in"] == 100
        assert turns[0]["tokens_out"] == 200
        assert turns[0]["latency_ms"] == 500
        store.close()

    def test_valid_zero_tokens(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        _make_durable_turn(store, cid)

        flusher = ContinuityFlusher(store, interval_seconds=60, retention_days=0)
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=0,
            tokens_out=0,
            latency_ms=0,
        )
        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "ok"
        assert turns[0]["tokens_in"] == 0
        assert turns[0]["tokens_out"] == 0
        assert turns[0]["latency_ms"] == 0
        store.close()


# ---------------------------------------------------------------------------
# D. Existing durable turn + None accounting (F-5)
# ---------------------------------------------------------------------------


class TestN8NoneAccountingPreservesExisting:
    """F-5: None accounting must not overwrite existing durable values with NULL."""

    def test_none_tokens_preserves_existing(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        _make_durable_turn(store, cid, tokens_in=10, tokens_out=20, latency_ms=30)

        flusher = ContinuityFlusher(store, interval_seconds=60, retention_days=0)
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
        )
        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "ok"
        assert turns[0]["tokens_in"] == 10
        assert turns[0]["tokens_out"] == 20
        assert turns[0]["latency_ms"] == 30
        store.close()

    def test_none_tokens_out_preserves_existing(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        _make_durable_turn(store, cid, tokens_in=10, tokens_out=20, latency_ms=30)

        flusher = ContinuityFlusher(store, interval_seconds=60, retention_days=0)
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=100,
        )
        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "ok"
        assert turns[0]["tokens_in"] == 100
        assert turns[0]["tokens_out"] == 20
        assert turns[0]["latency_ms"] == 30
        store.close()

    def test_partial_none_preserves_existing(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        _make_durable_turn(store, cid, tokens_in=10, tokens_out=20, latency_ms=30)

        flusher = ContinuityFlusher(store, interval_seconds=60, retention_days=0)
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=100,
            latency_ms=500,
        )
        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "ok"
        assert turns[0]["tokens_in"] == 100
        assert turns[0]["tokens_out"] == 20
        assert turns[0]["latency_ms"] == 500
        store.close()


# ---------------------------------------------------------------------------
# E. Provisional append + malformed update (N-7 regression)
# ---------------------------------------------------------------------------


class TestN7Regression:
    """N-7: provisional append + malformed update must not lose the turn."""

    def test_malformed_update_coalesces_with_append(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        flusher = ContinuityFlusher(store, interval_seconds=60, retention_days=0)

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
            tokens_in=-1,
            tokens_out=100,
        )
        assert flusher.queue_size == 1

        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "ok"
        assert turns[0]["tokens_in"] is None
        assert turns[0]["tokens_out"] == 100
        assert turns[0]["provider"] == "openai"
        assert flusher.queue_size == 0
        store.close()


# ---------------------------------------------------------------------------
# F. Provisional append + valid update
# ---------------------------------------------------------------------------


class TestN8ValidCoalescing:
    """Normal append+update coalescing must still work."""

    def test_valid_append_update_coalesces(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        flusher = ContinuityFlusher(store, interval_seconds=60, retention_days=0)

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
        assert flusher.queue_size == 1

        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "ok"
        assert turns[0]["tokens_in"] == 100
        assert turns[0]["tokens_out"] == 200
        assert turns[0]["latency_ms"] == 500
        assert turns[0]["provider"] == "openai"
        store.close()


# ---------------------------------------------------------------------------
# G. Multiple conversations
# ---------------------------------------------------------------------------


class TestN8MultipleConversations:
    """Malformed data for one conversation must not affect another."""

    def test_malformed_one_conversation_does_not_affect_other(self, tmp_path):
        store = _store(tmp_path)
        cid_a = "a" * 32
        cid_b = "b" * 32
        _create_conversation(store, cid_a, key_id="ka")
        _create_conversation(store, cid_b, key_id="kb")
        _make_durable_turn(store, cid_a, key_id="ka", tokens_in=10, tokens_out=20, latency_ms=30)
        _make_durable_turn(store, cid_b, key_id="kb", tokens_in=50, tokens_out=60, latency_ms=70)

        flusher = ContinuityFlusher(store, interval_seconds=60, retention_days=0)

        flusher.enqueue(
            "turn.update",
            conversation_id=cid_a,
            key_id="ka",
            seq=1,
            outcome="ok",
            tokens_in=-1,
        )
        flusher.enqueue(
            "turn.update",
            conversation_id=cid_b,
            key_id="kb",
            seq=1,
            outcome="ok",
            tokens_in=500,
            tokens_out=600,
            latency_ms=700,
        )

        flusher.flush()

        turns_a = store.turns(cid_a, "ka")
        assert len(turns_a) == 1
        assert turns_a[0]["tokens_in"] == 10
        assert turns_a[0]["outcome"] == "denied"

        turns_b = store.turns(cid_b, "kb")
        assert len(turns_b) == 1
        assert turns_b[0]["tokens_in"] == 500
        assert turns_b[0]["outcome"] == "ok"
        store.close()


# ---------------------------------------------------------------------------
# H. HandoffCoordinator in-memory state
# ---------------------------------------------------------------------------


class TestN8HandoffCoordinatorInMemory:
    """Malformed accounting must not contaminate committed-turn state."""

    def test_malformed_update_rejected_by_coordinator(self):
        flusher = _FakeFlusher()
        coord = _coordinator(flusher)

        turn = coord.start(
            key_id="k",
            client_bucket="cline",
            project_key="proj",
        )

        coord.commit(
            turn,
            provider="openai",
            model="gpt-4",
            outcome="denied",
        )

        state = coord._states[("k", turn.conversation_id)]
        assert len(state.committed_turns) == 1
        record = state.committed_turns[0]
        original_outcome = record["outcome"]
        original_tokens_in = record["tokens_in"]

        result = coord.update(
            turn,
            outcome="ok",
            tokens_in=-1,
            tokens_out="bad",
            latency_ms=False,
        )

        assert result == {}
        assert record["outcome"] == original_outcome
        assert record["tokens_in"] == original_tokens_in
        assert turn._provisional is True

    def test_valid_update_mutates_coordinator_state(self):
        flusher = _FakeFlusher()
        coord = _coordinator(flusher)

        turn = coord.start(
            key_id="k",
            client_bucket="cline",
            project_key="proj",
        )

        coord.commit(
            turn,
            provider="openai",
            model="gpt-4",
            outcome="denied",
        )

        result = coord.update(
            turn,
            outcome="ok",
            tokens_in=100,
            tokens_out=200,
            latency_ms=500,
        )

        assert result != {}
        state = coord._states[("k", turn.conversation_id)]
        record = state.committed_turns[0]
        assert record["outcome"] == "ok"
        assert record["tokens_in"] == 100
        assert record["tokens_out"] == 200
        assert record["latency_ms"] == 500

    def test_abort_with_malformed_state_not_enqueued(self):
        flusher = _FakeFlusher()
        coord = _coordinator(flusher)

        turn = coord.start(
            key_id="k",
            client_bucket="cline",
            project_key="proj",
        )

        coord.commit(
            turn,
            provider="openai",
            model="gpt-4",
            outcome="denied",
        )

        state = coord._states[("k", turn.conversation_id)]
        assert len(state.committed_turns) == 1
        record = state.committed_turns[0]

        result = coord.update(turn, outcome="ok", tokens_in=-1)
        assert result == {}
        assert record["outcome"] == "denied"


# ---------------------------------------------------------------------------
# I. Queue continuation
# ---------------------------------------------------------------------------


class TestN8QueueContinuation:
    """Malformed operation followed by valid operations must all persist."""

    def test_malformed_then_valid_all_persist(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        _make_durable_turn(store, cid)

        flusher = ContinuityFlusher(store, interval_seconds=60, retention_days=0)

        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=-1,
        )
        flusher.enqueue(
            "turn.append",
            conversation_id=cid,
            key_id="k",
            seq=2,
            outcome="ok",
            tokens_in=50,
            tokens_out=100,
        )
        flusher.enqueue(
            "turn.append",
            conversation_id=cid,
            key_id="k",
            seq=3,
            outcome="ok",
            tokens_in=150,
            tokens_out=200,
        )

        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 3
        t1 = [t for t in turns if t["seq"] == 1][0]
        assert t1["outcome"] == "denied"
        assert t1["tokens_in"] == 10
        t2 = [t for t in turns if t["seq"] == 2][0]
        assert t2["tokens_in"] == 50
        t3 = [t for t in turns if t["seq"] == 3][0]
        assert t3["tokens_in"] == 150
        assert flusher.queue_size == 0
        store.close()


# ---------------------------------------------------------------------------
# J. Infrastructure failure regression
# ---------------------------------------------------------------------------


class TestN8InfrastructureRegression:
    """Genuine transient failures must still be retained/retried."""

    def test_os_error_still_retains(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        _make_durable_turn(store, cid)

        flusher = ContinuityFlusher(store, interval_seconds=60, retention_days=0)
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=100,
            tokens_out=200,
        )

        original = store.update_turn

        def flaky(**kwargs):
            raise OSError("disk error")

        monkeypatch.setattr(store, "update_turn", flaky)
        flusher.flush()

        assert flusher.queue_size == 1
        stats = flusher.flush_stats()
        assert len(stats["flush_errors"]) == 1

        monkeypatch.setattr(store, "update_turn", original)
        flusher.flush()
        assert flusher.queue_size == 0
        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["tokens_in"] == 100
        store.close()


# ---------------------------------------------------------------------------
# K. ConversationStore.update_turn() directly (F-5 semantics)
# ---------------------------------------------------------------------------


class TestN8UpdateTurnPreservesExisting:
    """F-5: ConversationStore.update_turn must preserve existing accounting
    when None is passed."""

    def test_update_turn_none_preserves_existing(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        _make_durable_turn(store, cid, tokens_in=10, tokens_out=20, latency_ms=30)

        result = store.update_turn(
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
        )
        assert result != {}

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "ok"
        assert turns[0]["tokens_in"] == 10
        assert turns[0]["tokens_out"] == 20
        assert turns[0]["latency_ms"] == 30
        store.close()

    def test_update_turn_valid_overwrites_existing(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        _make_durable_turn(store, cid, tokens_in=10, tokens_out=20, latency_ms=30)

        result = store.update_turn(
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=100,
            tokens_out=200,
            latency_ms=500,
        )
        assert result != {}

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "ok"
        assert turns[0]["tokens_in"] == 100
        assert turns[0]["tokens_out"] == 200
        assert turns[0]["latency_ms"] == 500
        store.close()

    def test_update_turn_partial_none_preserves_existing(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        _make_durable_turn(store, cid, tokens_in=10, tokens_out=20, latency_ms=30)

        result = store.update_turn(
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=100,
        )
        assert result != {}

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["outcome"] == "ok"
        assert turns[0]["tokens_in"] == 100
        assert turns[0]["tokens_out"] == 20
        assert turns[0]["latency_ms"] == 30
        store.close()

    def test_update_turn_malformed_raises(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        _make_durable_turn(store, cid)

        with pytest.raises(MalformedInputError):
            store.update_turn(
                conversation_id=cid,
                key_id="k",
                seq=1,
                outcome="ok",
                tokens_in=-1,
            )

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["tokens_in"] == 10
        store.close()


# ---------------------------------------------------------------------------
# End-to-end: exact Codex N-8 scenario
# ---------------------------------------------------------------------------


class TestN8CodexExactScenario:
    """Exact Codex N-8 reproduction:

    Durable turn: outcome=denied, tokens=(10, 20, 30)
    Standalone turn.update: outcome=ok, tokens_in=-1, tokens_out="bad",
    latency_ms=False
    → outcome must remain 'denied', tokens must remain (10, 20, 30)
    """

    def test_codex_exact_scenario_durably_preserved(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)
        _make_durable_turn(store, cid, tokens_in=10, tokens_out=20, latency_ms=30)

        flusher = ContinuityFlusher(store, interval_seconds=60, retention_days=0)
        flusher.enqueue(
            "turn.update",
            conversation_id=cid,
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=-1,
            tokens_out="bad",
            latency_ms=False,
        )
        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1

        assert turns[0]["outcome"] == "denied"
        assert turns[0]["tokens_in"] == 10
        assert turns[0]["tokens_out"] == 20
        assert turns[0]["latency_ms"] == 30

        assert flusher.queue_size == 0
        stats = flusher.flush_stats()
        assert stats["flush_errors"] == []
        assert cid not in store.in_flight
        store.close()

    def test_codex_exact_scenario_in_memory_preserved(self):
        flusher = _FakeFlusher()
        coord = _coordinator(flusher)

        turn = coord.start(
            key_id="k",
            client_bucket="cline",
            project_key="proj",
        )

        coord.commit(
            turn,
            provider="openai",
            model="gpt-4",
            outcome="denied",
            tokens_in=10,
            tokens_out=20,
            latency_ms=30,
        )

        state = coord._states[("k", turn.conversation_id)]
        record = state.committed_turns[0]
        assert record["outcome"] == "denied"
        assert record["tokens_in"] == 10
        assert record["tokens_out"] == 20
        assert record["latency_ms"] == 30

        result = coord.update(
            turn,
            outcome="ok",
            tokens_in=-1,
            tokens_out="bad",
            latency_ms=False,
        )
        assert result == {}

        assert record["outcome"] == "denied"
        assert record["tokens_in"] == 10
        assert record["tokens_out"] == 20
        assert record["latency_ms"] == 30
        assert turn._provisional is True
