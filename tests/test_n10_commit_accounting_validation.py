"""
N-10 regression: malformed provider accounting bypasses commit() validation.

When a provider response carries malformed usage accounting (negative
ints, floats, bools, strings), HandoffCoordinator.commit() must sanitize
the values before they enter:

  1. In-memory committed_turns  (affects envelope building, compaction,
     model chain, diagnostics)
  2. The durable turn.append queue  (affects SQLite persistence)

Previously, commit() passed raw provider accounting directly into both
surfaces without calling _validate_non_negative_int().  The durable path
was guarded by ConversationStore.append_turn() + the flusher's
MalformedInputError catch, but the in-memory path was not — polluted
committed_turns corrupted envelope tail serialization, compaction
decisions, and summary accounting for the lifetime of the process.
"""

import json

import pytest

from app.services.conversation_store import (
    ConversationStore,
    MalformedInputError,
    _validate_non_negative_int,
)
from app.services.continuity_flusher import ContinuityFlusher
from app.services.context_manager import ContextManager
from app.services.handoff import HandoffCoordinator, TurnContext


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


def _turn_context(coordinator, cid="c" * 32, key_id="k"):
    """Create a fresh TurnContext through the coordinator's start()."""
    return coordinator.start(
        key_id=key_id,
        conversation_id=cid,
        client_bucket="cline",
        project_key="ab" * 16,
    )


def _get_state(coordinator, cid="c" * 32, key_id="k"):
    """Retrieve the in-memory conversation state."""
    return coordinator._states[(key_id, cid)]


# ---------------------------------------------------------------------------
# A. Reproduction: malformed accounting enters committed_turns without
#    validation (the N-10 bug)
# ---------------------------------------------------------------------------


class TestN10MalformedAccountingBypass:
    """Malformed provider accounting in commit() must not pollute
    in-memory state or the durable queue."""

    @pytest.mark.parametrize(
        "bad_value",
        [-1, -100, -999999],
        ids=["neg-one", "neg-hundred", "neg-large"],
    )
    def test_negative_int_tokens_sanitized_in_committed_turns(
        self, tmp_path, bad_value
    ):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)

        flusher = _FakeFlusher()
        coord = _coordinator(flusher)
        tc = _turn_context(coord, cid)

        result = tc.finish(
            provider="openai",
            model="gpt-4",
            tokens_in=bad_value,
            tokens_out=100,
            latency_ms=50,
        )
        assert result  # commit succeeded

        # The committed turn must have sanitized tokens_in (None, not -100)
        state = _get_state(coord, cid)
        last = state.committed_turns[-1]
        assert last["tokens_in"] is None  # sanitized
        assert last["tokens_out"] == 100  # valid, preserved
        assert last["latency_ms"] == 50  # valid, preserved

        # The durable queue must have sanitized tokens_in
        appends = _operations(flusher, "turn.append")
        assert len(appends) == 1
        assert appends[0]["tokens_in"] is None
        assert appends[0]["tokens_out"] == 100

    @pytest.mark.parametrize(
        "bad_value",
        [0.5, 3.14, -0.1],
        ids=["half", "pi", "neg-small"],
    )
    def test_float_tokens_sanitized_in_committed_turns(
        self, tmp_path, bad_value
    ):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)

        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )
        coord = _coordinator(flusher)
        tc = _turn_context(coord, cid)

        result = tc.finish(
            provider="openai",
            model="gpt-4",
            tokens_in=bad_value,
            tokens_out=100,
        )
        assert result

        state = _get_state(coord, cid)
        last = state.committed_turns[-1]
        assert last["tokens_in"] is None
        assert last["tokens_out"] == 100
        store.close()

    @pytest.mark.parametrize(
        "bad_value",
        [True, False],
        ids=["bool-true", "bool-false"],
    )
    def test_bool_tokens_sanitized_in_committed_turns(self, tmp_path, bad_value):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)

        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )
        coord = _coordinator(flusher)
        tc = _turn_context(coord, cid)

        result = tc.finish(
            provider="openai",
            model="gpt-4",
            tokens_in=bad_value,
            tokens_out=100,
        )
        assert result

        state = _get_state(coord, cid)
        last = state.committed_turns[-1]
        assert last["tokens_in"] is None
        store.close()

    def test_string_tokens_sanitized_in_committed_turns(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)

        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )
        coord = _coordinator(flusher)
        tc = _turn_context(coord, cid)

        result = tc.finish(
            provider="openai",
            model="gpt-4",
            tokens_in="not-a-number",
            tokens_out=100,
        )
        assert result

        state = _get_state(coord, cid)
        last = state.committed_turns[-1]
        assert last["tokens_in"] is None
        assert last["tokens_out"] == 100
        store.close()

    def test_malformed_latency_ms_sanitized(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)

        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )
        coord = _coordinator(flusher)
        tc = _turn_context(coord, cid)

        result = tc.finish(
            provider="openai",
            model="gpt-4",
            tokens_in=100,
            tokens_out=200,
            latency_ms=-50,
        )
        assert result

        state = _get_state(coord, cid)
        last = state.committed_turns[-1]
        assert last["tokens_in"] == 100
        assert last["tokens_out"] == 200
        assert last["latency_ms"] is None  # sanitized
        store.close()


# ---------------------------------------------------------------------------
# B. Valid accounting must pass through unchanged
# ---------------------------------------------------------------------------


class TestN10ValidAccountingUnchanged:
    """Valid accounting must not be affected by the sanitization."""

    def test_valid_int_accounting_preserved(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)

        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )
        coord = _coordinator(flusher)
        tc = _turn_context(coord, cid)

        result = tc.finish(
            provider="openai",
            model="gpt-4",
            tokens_in=500,
            tokens_out=150,
            latency_ms=42,
        )
        assert result

        state = _get_state(coord, cid)
        last = state.committed_turns[-1]
        assert last["tokens_in"] == 500
        assert last["tokens_out"] == 150
        assert last["latency_ms"] == 42
        store.close()

    def test_zero_accounting_preserved(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)

        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )
        coord = _coordinator(flusher)
        tc = _turn_context(coord, cid)

        result = tc.finish(
            provider="openai",
            model="gpt-4",
            tokens_in=0,
            tokens_out=0,
            latency_ms=0,
        )
        assert result

        state = _get_state(coord, cid)
        last = state.committed_turns[-1]
        assert last["tokens_in"] == 0
        assert last["tokens_out"] == 0
        assert last["latency_ms"] == 0
        store.close()

    def test_none_accounting_preserved(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)

        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )
        coord = _coordinator(flusher)
        tc = _turn_context(coord, cid)

        result = tc.finish(
            provider="openai",
            model="gpt-4",
        )
        assert result

        state = _get_state(coord, cid)
        last = state.committed_turns[-1]
        assert last["tokens_in"] is None
        assert last["tokens_out"] is None
        assert last["latency_ms"] is None
        store.close()


# ---------------------------------------------------------------------------
# C. Mixed valid + malformed accounting
# ---------------------------------------------------------------------------


class TestN10MixedAccounting:
    """When some fields are valid and some are malformed, valid fields
    must be preserved while malformed ones are sanitized."""

    def test_malformed_tokens_in_valid_rest(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)

        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )
        coord = _coordinator(flusher)
        tc = _turn_context(coord, cid)

        result = tc.finish(
            provider="anthropic",
            model="claude-3",
            tokens_in=-1,
            tokens_out=200,
            latency_ms=75,
        )
        assert result

        state = _get_state(coord, cid)
        last = state.committed_turns[-1]
        assert last["tokens_in"] is None
        assert last["tokens_out"] == 200
        assert last["latency_ms"] == 75
        store.close()

    def test_all_malformed_sanitized(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)

        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )
        coord = _coordinator(flusher)
        tc = _turn_context(coord, cid)

        result = tc.finish(
            provider="openai",
            model="gpt-4",
            tokens_in=-1,
            tokens_out="bad",
            latency_ms=3.14,
        )
        assert result

        state = _get_state(coord, cid)
        last = state.committed_turns[-1]
        assert last["tokens_in"] is None
        assert last["tokens_out"] is None
        assert last["latency_ms"] is None
        store.close()


# ---------------------------------------------------------------------------
# D. Durable persistence after sanitization
# ---------------------------------------------------------------------------


class TestN10DurablePersistence:
    """After sanitization, the turn must persist to SQLite with the
    sanitized values."""

    def test_sanitized_turn_persists_to_store(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)

        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )
        coord = _coordinator(flusher)
        tc = _turn_context(coord, cid)

        tc.finish(
            provider="openai",
            model="gpt-4",
            tokens_in=-99,
            tokens_out=100,
            latency_ms=50,
        )
        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["tokens_in"] is None
        assert turns[0]["tokens_out"] == 100
        assert turns[0]["latency_ms"] == 50
        assert turns[0]["outcome"] == "ok"
        store.close()

    def test_valid_turn_persists_normally(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)

        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )
        coord = _coordinator(flusher)
        tc = _turn_context(coord, cid)

        tc.finish(
            provider="openai",
            model="gpt-4",
            tokens_in=500,
            tokens_out=150,
            latency_ms=42,
        )
        flusher.flush()

        turns = store.turns(cid, "k")
        assert len(turns) == 1
        assert turns[0]["tokens_in"] == 500
        assert turns[0]["tokens_out"] == 150
        assert turns[0]["latency_ms"] == 42
        store.close()


# ---------------------------------------------------------------------------
# E. Multiple turns: malformed on one must not corrupt others
# ---------------------------------------------------------------------------


class TestN10MultiTurn:
    """A malformed accounting value on one turn must not affect other
    turns in the same conversation."""

    def test_malformed_then_valid_turns(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)

        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )
        coord = _coordinator(flusher)

        # Turn 1: malformed
        tc1 = _turn_context(coord, cid)
        tc1.finish(
            provider="openai",
            model="gpt-4",
            tokens_in=-100,
            tokens_out=-200,
        )

        # Turn 2: valid
        tc2 = _turn_context(coord, cid)
        tc2.finish(
            provider="openai",
            model="gpt-4",
            tokens_in=500,
            tokens_out=150,
            latency_ms=42,
        )

        state = _get_state(coord, cid)
        assert len(state.committed_turns) == 2
        # Turn 1: sanitized
        assert state.committed_turns[0]["tokens_in"] is None
        assert state.committed_turns[0]["tokens_out"] is None
        # Turn 2: valid, not affected
        assert state.committed_turns[1]["tokens_in"] == 500
        assert state.committed_turns[1]["tokens_out"] == 150
        assert state.committed_turns[1]["latency_ms"] == 42
        store.close()

    def test_valid_then_malformed_then_valid(self, tmp_path):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)

        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )
        coord = _coordinator(flusher)

        # Turn 1: valid
        tc1 = _turn_context(coord, cid)
        tc1.finish(
            provider="openai", model="gpt-4",
            tokens_in=100, tokens_out=200, latency_ms=10,
        )

        # Turn 2: malformed
        tc2 = _turn_context(coord, cid)
        tc2.finish(
            provider="openai", model="gpt-4",
            tokens_in="garbage", tokens_out=True, latency_ms=-5,
        )

        # Turn 3: valid
        tc3 = _turn_context(coord, cid)
        tc3.finish(
            provider="openai", model="gpt-4",
            tokens_in=300, tokens_out=400, latency_ms=30,
        )

        state = _get_state(coord, cid)
        assert len(state.committed_turns) == 3
        assert state.committed_turns[0]["tokens_in"] == 100
        assert state.committed_turns[1]["tokens_in"] is None
        assert state.committed_turns[2]["tokens_in"] == 300
        store.close()


# ---------------------------------------------------------------------------
# F. Envelope building with sanitized accounting
# ---------------------------------------------------------------------------


class TestN10EnvelopeIntegrity:
    """Sanitized accounting must produce a valid envelope tail that
    serializes correctly and produces correct cost estimates."""

    def test_envelope_tail_serializes_with_sanitized_accounting(
        self, tmp_path
    ):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)

        flusher = ContinuityFlusher(
            store, interval_seconds=60, retention_days=0
        )
        coord = _coordinator(flusher)

        # Commit a turn with mixed valid/malformed accounting
        tc = _turn_context(coord, cid)
        tc.finish(
            provider="openai",
            model="gpt-4",
            tokens_in=-100,
            tokens_out=500,
            latency_ms=42,
        )

        # Build an envelope — should not crash
        state = _get_state(coord, cid)
        envelope = coord._build_envelope(state)
        assert envelope is not None

        # The tail should be valid JSON with sanitized values
        tail_text = envelope.get("tail", "[]")
        tail = json.loads(tail_text)
        assert isinstance(tail, list)
        assert len(tail) == 1
        # tokens_in should be None (omitted from serialization since
        # serialize_tail skips None values)
        assert "tokens_in" not in tail[0]
        assert tail[0]["tokens_out"] == 500
        store.close()

    def test_compaction_cost_uses_sanitized_values(self, tmp_path):
        """Malformed tokens must not corrupt the compaction cost estimate."""
        from app.services.context_manager import _turn_cost

        # A turn with sanitized accounting (tokens_in=None after fix)
        turn_sanitized = {
            "tokens_in": None,
            "tokens_out": 100,
            "latency_ms": 10,
        }
        cost = _turn_cost(turn_sanitized)
        # tokens_in=None → int(None or 0) → 0, tokens_out=100 → 100
        # max(1, 0+100) = 100
        assert cost == 100

        # A turn with valid accounting
        turn_valid = {
            "tokens_in": 50,
            "tokens_out": 100,
            "latency_ms": 10,
        }
        cost_valid = _turn_cost(turn_valid)
        assert cost_valid == 150


# ---------------------------------------------------------------------------
# G. Queue coalescing interaction
# ---------------------------------------------------------------------------


class TestN10QueueCoalescing:
    """Malformed accounting in a turn.append must not break the queue
    or coalescing behavior."""

    def test_malformed_append_enqueues_with_sanitized_values(
        self, tmp_path
    ):
        store = _store(tmp_path)
        cid = "c" * 32
        _create_conversation(store, cid)

        flusher = _FakeFlusher()
        coord = _coordinator(flusher)
        tc = _turn_context(coord, cid)

        tc.finish(
            provider="openai",
            model="gpt-4",
            tokens_in="not-a-number",
            tokens_out=-5,
            latency_ms=True,
        )

        appends = _operations(flusher, "turn.append")
        assert len(appends) == 1
        assert appends[0]["tokens_in"] is None
        assert appends[0]["tokens_out"] is None
        assert appends[0]["latency_ms"] is None
        # Other fields preserved
        assert appends[0]["provider"] == "openai"
        assert appends[0]["model"] == "gpt-4"
        assert appends[0]["outcome"] == "ok"
