"""
Phase 14 tests: provisional turn lifecycle + finalization.

Covers the full provisional→finalized lifecycle, crash-safety invariants,
token accounting, recovery interaction, provider usage extraction,
overflow retry interaction, and cancellation semantics.
"""

import asyncio
import sqlite3
import time

import pytest

from app.core.config import settings
from app.services.context_manager import ContextManager
from app.services.conversation_store import ConversationStore
from app.services.continuity_flusher import ContinuityFlusher
from app.services.continuity_recovery import ContinuityRecovery
from app.services.handoff import HandoffCoordinator, TurnContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeFlusher:
    """Records enqueued operations for inspection."""

    def __init__(self):
        self.enqueue_calls = []

    def enqueue(self, operation, **kwargs):
        self.enqueue_calls.append((operation, dict(kwargs)))


def _coordinator(flusher=None, recovery=None, **kwargs):
    return HandoffCoordinator(
        flusher=flusher if flusher is not None else FakeFlusher(),
        context_manager=ContextManager(
            char_token_ratio=4,
            context_token_budget=2048,
            output_reserve_tokens=128,
        ),
        recovery=recovery,
        **kwargs,
    )


def _operations(flusher, operation):
    return [kwargs for op, kwargs in flusher.enqueue_calls if op == operation]


def _store(tmp_path, name="platform.db"):
    return ConversationStore(str(tmp_path / name))


def _wired_store_flusher(tmp_path):
    """A real ConversationStore + ContinuityFlusher wired together."""
    store = _store(tmp_path)
    flusher = ContinuityFlusher(store, interval_seconds=9999)
    flusher.start()
    return store, flusher


def _make_provider(name="openai", model="gpt-4o"):
    from app.providers.base import Provider
    return Provider(
        name=name,
        base_url=f"https://{name}.invalid",
        api_key="test-key",
        enabled=True,
        priority=1,
        models=[model],
    )


class _FakeStreamingClient:
    """Configurable streaming client for tests."""

    def __init__(self):
        self.stream_calls = []
        self._chunks = []

    def set_chunks(self, chunks):
        self._chunks = list(chunks)

    async def achat_stream_messages(self, provider, payload):
        self.stream_calls.append((provider.name, payload))
        for chunk in self._chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    async def achat_messages(self, provider, payload):
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 15},
        }


class _FakeStreamingClientWithDisconnect:
    """Client that yields one chunk then raises ConnectionError."""

    def __init__(self):
        self.stream_calls = []

    async def achat_stream_messages(self, provider, payload):
        self.stream_calls.append((provider.name, payload))
        yield {"choices": [{"delta": {"content": "Hello"}}]}
        raise ConnectionError("client disconnected")


@pytest.fixture
def fake_registry(monkeypatch):
    """Point every ClientRegistry at FakeClients, no real network."""
    from app.services import client_registry
    holder = {}

    def fake_get(self, provider_name):
        return holder[provider_name]

    monkeypatch.setattr(
        client_registry.ClientRegistry, "get", fake_get
    )
    return holder


# ---------------------------------------------------------------------------
# A. Store layer: update_turn
# ---------------------------------------------------------------------------


class TestConversationStoreUpdateTurn:
    def test_update_turn_basic(self, tmp_path):
        store = _store(tmp_path)
        conv = store.create(key_id="k", client_bucket="cline", project_key="p" * 32)
        store.append_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="denied", provider="openai", model="gpt-4o",
        )
        updated = store.update_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="ok", tokens_in=100, tokens_out=50, latency_ms=1234,
        )
        last = store.last_turn(conv["id"], "k")
        store.close()

        assert updated["outcome"] == "ok"
        assert updated["tokens_in"] == 100
        assert updated["tokens_out"] == 50
        assert updated["latency_ms"] == 1234
        assert last["outcome"] == "ok"
        assert last["tokens_in"] == 100
        assert last["tokens_out"] == 50

    def test_update_turn_nonexistent_returns_empty(self, tmp_path):
        store = _store(tmp_path)
        conv = store.create(key_id="k", client_bucket="cline", project_key="p" * 32)
        result = store.update_turn(
            conversation_id=conv["id"], key_id="k", seq=999,
            outcome="ok",
        )
        store.close()
        assert result == {}

    def test_update_turn_wrong_key_returns_empty(self, tmp_path):
        store = _store(tmp_path)
        conv = store.create(key_id="k", client_bucket="cline", project_key="p" * 32)
        store.append_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="denied",
        )
        result = store.update_turn(
            conversation_id=conv["id"], key_id="wrong-key", seq=1,
            outcome="ok",
        )
        store.close()
        assert result == {}

    def test_update_turn_invalid_outcome_raises(self, tmp_path):
        store = _store(tmp_path)
        conv = store.create(key_id="k", client_bucket="cline", project_key="p" * 32)
        store.append_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="denied",
        )
        with pytest.raises(ValueError, match="invalid turn outcome"):
            store.update_turn(
                conversation_id=conv["id"], key_id="k", seq=1,
                outcome="bogus",
            )
        store.close()

    def test_update_turn_none_tokens_preserved(self, tmp_path):
        store = _store(tmp_path)
        conv = store.create(key_id="k", client_bucket="cline", project_key="p" * 32)
        store.append_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="denied", provider="openai", model="gpt-4o",
        )
        store.update_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="failed",
        )
        last = store.last_turn(conv["id"], "k")
        store.close()

        assert last["outcome"] == "failed"
        assert last["tokens_in"] is None
        assert last["tokens_out"] is None

    def test_update_turn_idempotent(self, tmp_path):
        store = _store(tmp_path)
        conv = store.create(key_id="k", client_bucket="cline", project_key="p" * 32)
        store.append_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="denied",
        )
        store.update_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="ok", tokens_in=10, tokens_out=5,
        )
        store.update_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="ok", tokens_in=10, tokens_out=5,
        )
        last = store.last_turn(conv["id"], "k")
        store.close()
        assert last["outcome"] == "ok"
        assert last["tokens_in"] == 10


# ---------------------------------------------------------------------------
# B. HandoffCoordinator: update() in-memory + flusher
# ---------------------------------------------------------------------------


class TestHandoffCoordinatorUpdate:
    def test_update_modifies_committed_turn(self):
        flusher = FakeFlusher()
        coord = _coordinator(flusher=flusher)
        turn = coord.start(
            key_id="k", client_bucket="cline", project_key="p" * 32,
        )

        # Provisional commit
        coord.commit(
            turn, provider="openai", model="gpt-4o", outcome="denied",
        )
        state = coord._states[("k", turn.conversation_id)]
        assert state.committed_turns[-1]["outcome"] == "denied"

        # Finalize
        coord.update(
            turn, outcome="ok", tokens_in=100, tokens_out=50, latency_ms=500,
        )

        # In-memory record should now be outcome="ok"
        assert state.committed_turns[-1]["outcome"] == "ok"
        assert state.committed_turns[-1]["tokens_in"] == 100
        assert state.committed_turns[-1]["tokens_out"] == 50
        assert state.committed_turns[-1]["latency_ms"] == 500

        # Flusher should have turn.update
        updates = _operations(flusher, "turn.update")
        assert len(updates) == 1
        assert updates[0]["outcome"] == "ok"
        assert updates[0]["tokens_in"] == 100

    def test_update_to_failed(self):
        flusher = FakeFlusher()
        coord = _coordinator(flusher=flusher)
        turn = coord.start(
            key_id="k", client_bucket="cline", project_key="p" * 32,
        )
        coord.commit(turn, provider="openai", model="gpt-4o", outcome="denied")
        coord.update(turn, outcome="failed")

        state = coord._states[("k", turn.conversation_id)]
        assert state.committed_turns[-1]["outcome"] == "failed"

        updates = _operations(flusher, "turn.update")
        assert len(updates) == 1
        assert updates[0]["outcome"] == "failed"

    def test_update_without_prior_commit_returns_empty(self):
        flusher = FakeFlusher()
        coord = _coordinator(flusher=flusher)
        turn = coord.start(
            key_id="k", client_bucket="cline", project_key="p" * 32,
        )
        result = coord.update(turn, outcome="ok")
        assert result == {}

    def test_update_does_not_add_second_turn(self):
        flusher = FakeFlusher()
        coord = _coordinator(flusher=flusher)
        turn = coord.start(
            key_id="k", client_bucket="cline", project_key="p" * 32,
        )
        coord.commit(turn, provider="openai", model="gpt-4o", outcome="denied")
        coord.update(turn, outcome="ok", tokens_in=10, tokens_out=5)

        state = coord._states[("k", turn.conversation_id)]
        # Should be exactly one turn, not two
        assert len(state.committed_turns) == 1
        assert state.committed_turns[0]["outcome"] == "ok"
        assert state.next_seq == 2  # seq was assigned at commit time


# ---------------------------------------------------------------------------
# C. TurnContext.update() API
# ---------------------------------------------------------------------------


class TestTurnContextUpdate:
    def test_update_delegates_to_coordinator(self):
        flusher = FakeFlusher()
        coord = _coordinator(flusher=flusher)
        turn = coord.start(
            key_id="k", client_bucket="cline", project_key="p" * 32,
        )
        coord.commit(turn, provider="openai", model="gpt-4o", outcome="denied")

        result = turn.update(outcome="ok", tokens_in=20, tokens_out=10)

        assert result["outcome"] == "ok"
        state = coord._states[("k", turn.conversation_id)]
        assert state.committed_turns[-1]["outcome"] == "ok"

    def test_update_without_coordinator_returns_empty(self):
        turn = TurnContext(
            conversation_id="c", key_id="k",
            client_bucket="cline", project_key="p" * 32,
        )
        result = turn.update(outcome="ok")
        assert result == {}

    def test_update_exception_safe(self):
        flusher = FakeFlusher()
        coord = _coordinator(flusher=flusher)
        turn = coord.start(
            key_id="k", client_bucket="cline", project_key="p" * 32,
        )
        # Patch update to raise
        original = coord.update

        def broken_update(*a, **kw):
            raise RuntimeError("boom")

        coord.update = broken_update
        result = turn.update(outcome="ok")
        assert result == {}
        coord.update = original


# ---------------------------------------------------------------------------
# D. Streaming lifecycle: provisional → finalization
# ---------------------------------------------------------------------------


class TestStreamingProvisionalLifecycle:
    @pytest.mark.asyncio
    async def test_provisional_commit_after_first_chunk(self, fake_registry):
        """After first chunk, turn is committed with outcome='denied'."""
        from app.services.async_chat_service import AsyncChatService

        provider = _make_provider()
        client = _FakeStreamingClient()
        client.set_chunks([
            {"choices": [{"delta": {"content": "Hello"}}], "finish_reason": None},
            {"choices": [{"delta": {"content": " world"}}], "finish_reason": None},
            {"choices": [{"delta": {}}], "finish_reason": "stop"},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        ])
        fake_registry[provider.identity()] = client

        flusher = FakeFlusher()
        coord = _coordinator(flusher=flusher)
        turn = coord.start(
            key_id="k", client_bucket="cline", project_key="p" * 32,
        )

        svc = AsyncChatService()
        result = await svc.achat_across_stream_messages(
            [(provider, "gpt-4o")],
            {"messages": [{"role": "user", "content": "hi"}], "stream": True},
            turn=turn,
        )

        assert result["success"] is True

        # The turn should be committed with outcome="denied" (provisional)
        state = coord._states[("k", turn.conversation_id)]
        assert len(state.committed_turns) == 1
        assert state.committed_turns[0]["outcome"] == "denied"
        assert state.committed_turns[0]["tokens_in"] is None

        # turn.append should be enqueued (provisional commit)
        appends = _operations(flusher, "turn.append")
        assert len(appends) == 1
        assert appends[0]["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_stream_finalize_after_consumption(self, fake_registry):
        """After consuming stream through API-layer wrapping, turn finalized."""
        from app.services.async_chat_service import AsyncChatService

        provider = _make_provider()
        client = _FakeStreamingClient()
        client.set_chunks([
            {"choices": [{"delta": {"content": "Hi"}}], "finish_reason": None},
            {"choices": [], "usage": {"prompt_tokens": 42, "completion_tokens": 7}},
            {"choices": [{"delta": {}}], "finish_reason": "stop"},
        ])
        fake_registry[provider.identity()] = client

        flusher = FakeFlusher()
        coord = _coordinator(flusher=flusher)
        turn = coord.start(
            key_id="k", client_bucket="cline", project_key="p" * 32,
        )

        svc = AsyncChatService()
        result = await svc.achat_across_stream_messages(
            [(provider, "gpt-4o")],
            {"messages": [{"role": "user", "content": "hi"}], "stream": True},
            turn=turn,
        )

        # Simulate the API layer's stream_generator wrapping: consume
        # chunks, extract usage, and call turn.update() in finally block.
        usage_in = None
        usage_out = None
        success = False
        try:
            async for chunk in result["stream_gen"]:
                chunk_usage = chunk.get("usage") if isinstance(chunk, dict) else None
                if isinstance(chunk_usage, dict):
                    if chunk_usage.get("prompt_tokens") is not None:
                        usage_in = chunk_usage["prompt_tokens"]
                    if chunk_usage.get("completion_tokens") is not None:
                        usage_out = chunk_usage["completion_tokens"]
            success = True
        finally:
            turn.update(
                outcome="ok" if success else "failed",
                tokens_in=usage_in,
                tokens_out=usage_out,
            )

        # After stream consumption, turn should be finalized
        state = coord._states[("k", turn.conversation_id)]
        assert state.committed_turns[0]["outcome"] == "ok"
        assert state.committed_turns[0]["tokens_in"] == 42
        assert state.committed_turns[0]["tokens_out"] == 7

        # turn.update should be enqueued
        updates = _operations(flusher, "turn.update")
        assert len(updates) == 1
        assert updates[0]["outcome"] == "ok"
        assert updates[0]["tokens_in"] == 42
        assert updates[0]["tokens_out"] == 7

    @pytest.mark.asyncio
    async def test_stream_failure_finalize_as_failed(self, fake_registry):
        """Provider error mid-stream → turn finalized as 'failed'."""
        from app.services.async_chat_service import AsyncChatService

        provider = _make_provider()
        client = _FakeStreamingClient()
        client.set_chunks([
            {"choices": [{"delta": {"content": "Hi"}}], "finish_reason": None},
            RuntimeError("provider exploded"),
        ])
        fake_registry[provider.identity()] = client

        flusher = FakeFlusher()
        coord = _coordinator(flusher=flusher)
        turn = coord.start(
            key_id="k", client_bucket="cline", project_key="p" * 32,
        )

        svc = AsyncChatService()
        result = await svc.achat_across_stream_messages(
            [(provider, "gpt-4o")],
            {"messages": [{"role": "user", "content": "hi"}], "stream": True},
            turn=turn,
        )

        # Simulate API-layer wrapping: consume and call turn.update in finally
        success = False
        try:
            async for _ in result["stream_gen"]:
                pass
            success = True
        except Exception:
            pass
        finally:
            turn.update(outcome="ok" if success else "failed")

        # After stream consumption (with error), turn finalized as failed
        state = coord._states[("k", turn.conversation_id)]
        assert state.committed_turns[0]["outcome"] == "failed"

        updates = _operations(flusher, "turn.update")
        assert len(updates) == 1
        assert updates[0]["outcome"] == "failed"


# ---------------------------------------------------------------------------
# E. Recovery interaction: provisional turns not resume points
# ---------------------------------------------------------------------------


class TestRecoveryInteraction:
    def test_provisional_turn_denied_resume(self, tmp_path):
        """A provisional (outcome='denied') turn must not be a resume point."""
        from app.services.continuity_headers import derive_resume_token_hash

        store = _store(tmp_path)
        conv = store.create(key_id="k", client_bucket="cline", project_key="p" * 32)
        store.append_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="denied", provider="openai", model="gpt-4o",
            resume_token_hash=derive_resume_token_hash("tok"),
        )

        recovery = ContinuityRecovery(store=store)
        result = recovery.validate_resume(conv["id"], "k", "tok")
        store.close()

        # outcome="denied" → resume denied
        assert result["valid"] is False
        assert result["reason"] == "last_turn_not_ok"

    def test_finalized_turn_allows_resume(self, tmp_path):
        """After finalization to outcome='ok', the turn is a resume point."""
        from app.services.continuity_headers import derive_resume_token_hash

        store = _store(tmp_path)
        conv = store.create(key_id="k", client_bucket="cline", project_key="p" * 32)
        store.append_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="denied", provider="openai", model="gpt-4o",
            resume_token_hash=derive_resume_token_hash("tok"),
        )

        # Finalize the turn
        store.update_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="ok", tokens_in=10, tokens_out=5,
        )

        recovery = ContinuityRecovery(store=store)
        result = recovery.validate_resume(conv["id"], "k", "tok")
        store.close()

        # After finalization, resume should succeed
        assert result["valid"] is True

    def test_fresh_start_after_provisional_crash(self, tmp_path):
        """After crash with provisional turn, fresh start works correctly."""
        store = _store(tmp_path)
        conv = store.create(key_id="k", client_bucket="cline", project_key="p" * 32)
        store.append_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="denied", provider="openai", model="gpt-4o",
        )

        recovery = ContinuityRecovery(store=store)

        # durable_last_seq should include the provisional turn
        seq = recovery.durable_last_seq(conv["id"], "k")
        assert seq == 1

        # last_provider_model should include the provisional turn's model
        pm = recovery.last_provider_model(conv["id"], "k")
        assert pm is not None
        assert pm["model"] == "gpt-4o"
        store.close()


# ---------------------------------------------------------------------------
# F. Non-streaming token accounting
# ---------------------------------------------------------------------------


class TestNonStreamingTokenAccounting:
    @pytest.mark.asyncio
    async def test_tokens_from_response_dict(self, fake_registry):
        """Non-streaming path extracts usage from response dict."""
        from app.services.async_chat_service import AsyncChatService

        provider = _make_provider()
        fake_registry[provider.identity()] = _FakeStreamingClient()

        flusher = FakeFlusher()
        coord = _coordinator(flusher=flusher)
        turn = coord.start(
            key_id="k", client_bucket="cline", project_key="p" * 32,
        )

        svc = AsyncChatService()
        result = await svc.achat_across_messages(
            [(provider, "gpt-4o")],
            {"messages": [{"role": "user", "content": "hi"}]},
            turn=turn,
        )

        assert result["success"] is True
        state = coord._states[("k", turn.conversation_id)]
        assert state.committed_turns[0]["tokens_in"] == 20
        assert state.committed_turns[0]["tokens_out"] == 15


# ---------------------------------------------------------------------------
# G. Overflow interaction
# ---------------------------------------------------------------------------


class TestOverflowInteraction:
    def test_pre_first_chunk_overflow_retries_normally(self):
        """Overflow before first chunk → retry, no provisional turn."""
        from app.services.context_manager import ContextOverflowSignal

        flusher = FakeFlusher()
        coord = _coordinator(flusher=flusher)
        turn = coord.start(
            key_id="k", client_bucket="cline", project_key="p" * 32,
        )

        # Simulate overflow error
        exc = ContextOverflowSignal("context length exceeded")
        assert turn.context_manager.should_retry_compacted(exc) is True

        # No turn committed yet
        state = coord._states[("k", turn.conversation_id)]
        assert len(state.committed_turns) == 0

    def test_post_first_chunk_provisional_exists(self):
        """After first chunk, provisional turn exists in committed_turns."""
        flusher = FakeFlusher()
        coord = _coordinator(flusher=flusher)
        turn = coord.start(
            key_id="k", client_bucket="cline", project_key="p" * 32,
        )

        # Provisional commit
        coord.commit(turn, provider="openai", model="gpt-4o", outcome="denied")

        state = coord._states[("k", turn.conversation_id)]
        assert len(state.committed_turns) == 1

        # The overflow retry decision is made in the chat service loop,
        # not in the coordinator. The invariant is: overflow retry happens
        # BEFORE first_chunk is yielded (inside the try/except for first_chunk).
        # After first_chunk succeeds, the overflow_retried flag prevents
        # another retry. This is preserved by the existing code structure.


# ---------------------------------------------------------------------------
# H. Client disconnect / cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    def test_long_conversation_keeps_bounded_tail_and_summary(self):
        flusher = FakeFlusher()
        coord = _coordinator(flusher=flusher, max_in_memory_turns=3)
        cid = "t" * 32

        for seq in range(1, 11):
            turn = coord.start(
                key_id="k", client_bucket="cline", project_key="p" * 32,
                conversation_id=cid,
            )
            turn.finish(
                provider="openai", model="gpt-4o", task=f"task-{seq}"
            )

        state = coord._states[("k", cid)]
        assert len(state.committed_turns) <= 3
        assert state.committed_turns[-1]["seq"] == 10
        assert state.rolling_summary is not None
        assert len(state.rolling_summary["summary_text"]) <= 4096
        assert any(
            operation == "summary.record"
            for operation, _kwargs in flusher.enqueue_calls
        )

    def test_aborting_uncommitted_turn_clears_pending_resume_hash(self):
        recovery = ContinuityRecovery(
            None, max_pending_tokens=2
        )
        coord = _coordinator(recovery=recovery)
        turns = [
            coord.start(
                key_id="k", client_bucket="cline", project_key="p" * 32,
                conversation_id=("a" * 31) + str(index),
            )
            for index in range(3)
        ]

        assert recovery.pending_token_hash(
            turns[0].conversation_id, "k"
        ) is None
        assert recovery.pending_token_hash(
            turns[1].conversation_id, "k"
        ) is not None
        turns[1].abort()
        assert recovery.pending_token_hash(
            turns[1].conversation_id, "k"
        ) is None

    @pytest.mark.asyncio
    async def test_client_disconnect_finalizes_as_failed(self, fake_registry):
        """Client disconnect → exception → turn finalized as 'failed'."""
        from app.services.async_chat_service import AsyncChatService

        provider = _make_provider("openai", "gpt-4o")
        fake_registry[provider.identity()] = _FakeStreamingClientWithDisconnect()

        flusher = FakeFlusher()
        coord = _coordinator(flusher=flusher)
        turn = coord.start(
            key_id="k", client_bucket="cline", project_key="p" * 32,
        )

        svc = AsyncChatService()
        result = await svc.achat_across_stream_messages(
            [(provider, "gpt-4o")],
            {"messages": [{"role": "user", "content": "hi"}], "stream": True},
            turn=turn,
        )

        # Simulate API-layer wrapping: consume and call turn.update in finally
        success = False
        try:
            async for _ in result["stream_gen"]:
                pass
            success = True
        except Exception:
            pass
        finally:
            turn.update(outcome="ok" if success else "failed")

        # After stream consumption (with error), turn finalized as failed
        state = coord._states[("k", turn.conversation_id)]
        assert state.committed_turns[0]["outcome"] == "failed"

        updates = _operations(flusher, "turn.update")
        assert len(updates) == 1
        assert updates[0]["outcome"] == "failed"


# ---------------------------------------------------------------------------
# I. Flush flusher integration: turn.update drains correctly
# ---------------------------------------------------------------------------


class TestFlusherIntegration:
    def test_turn_update_drains_to_store(self, tmp_path):
        store, flusher = _wired_store_flusher(tmp_path)
        conv = store.create(key_id="k", client_bucket="cline", project_key="p" * 32)
        store.append_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="denied", provider="openai", model="gpt-4o",
        )

        # Simulate the flusher draining a turn.update
        flusher.enqueue(
            "turn.update",
            conversation_id=conv["id"],
            key_id="k",
            seq=1,
            outcome="ok",
            tokens_in=42,
            tokens_out=21,
            latency_ms=999,
        )
        flusher.flush()

        last = store.last_turn(conv["id"], "k")
        store.close()

        assert last["outcome"] == "ok"
        assert last["tokens_in"] == 42
        assert last["tokens_out"] == 21
        assert last["latency_ms"] == 999

    def test_turn_update_idempotent_for_missing_row(self, tmp_path):
        """turn.update for a non-existent row is a no-op (no crash)."""
        store, flusher = _wired_store_flusher(tmp_path)
        conv = store.create(key_id="k", client_bucket="cline", project_key="p" * 32)

        # Enqueue update for a seq that doesn't exist
        flusher.enqueue(
            "turn.update",
            conversation_id=conv["id"],
            key_id="k",
            seq=999,
            outcome="ok",
        )
        # Should not raise
        flusher.flush()
        store.close()


# ---------------------------------------------------------------------------
# J. Crash semantics: provisional turn not treated as success
# ---------------------------------------------------------------------------


class TestCrashSemantics:
    def test_provisional_turn_survives_restart_as_denied(self, tmp_path):
        """Simulate restart: provisional turn persisted as 'denied'."""
        store = _store(tmp_path)
        conv = store.create(key_id="k", client_bucket="cline", project_key="p" * 32)

        # Simulate a provisional turn written to SQLite
        store.append_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="denied", provider="openai", model="gpt-4o",
        )

        # "Restart" - create fresh recovery instance
        recovery = ContinuityRecovery(store=store)

        # Validate: last turn is "denied" → not a resume point
        last = store.last_turn(conv["id"], "k")
        assert last["outcome"] == "denied"

        # But seq continuity works
        seq = recovery.durable_last_seq(conv["id"], "k")
        assert seq == 1

        # Model lineage works
        pm = recovery.last_provider_model(conv["id"], "k")
        assert pm["model"] == "gpt-4o"
        store.close()

    def test_finalized_turn_survives_restart_as_ok(self, tmp_path):
        """After finalization, turn persists as 'ok' with real tokens."""
        store = _store(tmp_path)
        conv = store.create(key_id="k", client_bucket="cline", project_key="p" * 32)
        store.append_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="denied", provider="openai", model="gpt-4o",
        )
        store.update_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="ok", tokens_in=100, tokens_out=50,
        )

        last = store.last_turn(conv["id"], "k")
        assert last["outcome"] == "ok"
        assert last["tokens_in"] == 100
        assert last["tokens_out"] == 50
        store.close()

    def test_multiple_turns_provisional_then_finalized(self, tmp_path):
        """Two turns: one provisional, one finalized."""
        store = _store(tmp_path)
        conv = store.create(key_id="k", client_bucket="cline", project_key="p" * 32)

        # Turn 1: provisional → finalized
        store.append_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="denied", provider="openai", model="gpt-4o",
        )
        store.update_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="ok", tokens_in=10, tokens_out=5,
        )

        # Turn 2: provisional (new stream)
        store.append_turn(
            conversation_id=conv["id"], key_id="k", seq=2,
            outcome="denied", provider="anthropic", model="claude-3",
        )

        turns = store.turns(conv["id"], "k")
        store.close()

        assert len(turns) == 2
        assert turns[0]["outcome"] == "ok"
        assert turns[1]["outcome"] == "denied"


# ---------------------------------------------------------------------------
# K. Existing continuity integrity: commit → envelope → recovery
# ---------------------------------------------------------------------------


class TestExistingContinuityIntegrity:
    def test_provisional_turn_appears_in_envelope(self):
        """Provisional turn is in committed_turns → included in envelope."""
        flusher = FakeFlusher()
        coord = _coordinator(flusher=flusher)
        turn = coord.start(
            key_id="k", client_bucket="cline", project_key="p" * 32,
        )
        coord.commit(turn, provider="openai", model="gpt-4o", outcome="denied")

        # Envelope should include the turn's model in model_chain
        assert "gpt-4o" in turn.model_chain

        # Finalize
        coord.update(turn, outcome="ok", tokens_in=10, tokens_out=5)

        state = coord._states[("k", turn.conversation_id)]
        assert state.committed_turns[0]["outcome"] == "ok"
        assert state.committed_turns[0]["tokens_in"] == 10

    def test_model_chain_survives_provisional_to_finalized(self):
        """Model chain is not duplicated by provisional→finalized."""
        flusher = FakeFlusher()
        coord = _coordinator(flusher=flusher)
        turn = coord.start(
            key_id="k", client_bucket="cline", project_key="p" * 32,
        )
        coord.commit(turn, provider="openai", model="gpt-4o", outcome="denied")
        coord.update(turn, outcome="ok")

        state = coord._states[("k", turn.conversation_id)]
        # Model should appear once
        assert state.model_chain.count("gpt-4o") == 1

    def test_next_seq_unchanged_by_update(self):
        """update() doesn't advance next_seq (already assigned at commit)."""
        flusher = FakeFlusher()
        coord = _coordinator(flusher=flusher)
        turn = coord.start(
            key_id="k", client_bucket="cline", project_key="p" * 32,
        )
        seq_before = coord._states[
            ("k", turn.conversation_id)
        ].next_seq
        coord.commit(turn, provider="openai", model="gpt-4o", outcome="denied")
        seq_after_commit = coord._states[
            ("k", turn.conversation_id)
        ].next_seq
        coord.update(turn, outcome="ok")
        seq_after_update = coord._states[
            ("k", turn.conversation_id)
        ].next_seq

        assert seq_after_commit == seq_before + 1
        assert seq_after_update == seq_after_commit  # unchanged by update


# ---------------------------------------------------------------------------
# F-4: Token validation regression tests
# ---------------------------------------------------------------------------


class TestUpdateTurnTokenValidation:
    def test_negative_tokens_in_raises(self, tmp_path):
        store = _store(tmp_path)
        conv = store.create(key_id="k", client_bucket="cline", project_key="p" * 32)
        store.append_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="denied", provider="openai", model="gpt-4o",
        )
        with pytest.raises(ValueError, match="invalid tokens_in"):
            store.update_turn(
                conversation_id=conv["id"], key_id="k", seq=1,
                outcome="ok", tokens_in=-1, tokens_out=50,
            )
        store.close()

    def test_negative_tokens_out_raises(self, tmp_path):
        store = _store(tmp_path)
        conv = store.create(key_id="k", client_bucket="cline", project_key="p" * 32)
        store.append_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="denied", provider="openai", model="gpt-4o",
        )
        with pytest.raises(ValueError, match="invalid tokens_out"):
            store.update_turn(
                conversation_id=conv["id"], key_id="k", seq=1,
                outcome="ok", tokens_in=100, tokens_out=-5,
            )
        store.close()

    def test_negative_latency_ms_raises(self, tmp_path):
        store = _store(tmp_path)
        conv = store.create(key_id="k", client_bucket="cline", project_key="p" * 32)
        store.append_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="denied", provider="openai", model="gpt-4o",
        )
        with pytest.raises(ValueError, match="invalid latency_ms"):
            store.update_turn(
                conversation_id=conv["id"], key_id="k", seq=1,
                outcome="ok", latency_ms=-100,
            )
        store.close()

    def test_zero_tokens_accepted(self, tmp_path):
        store = _store(tmp_path)
        conv = store.create(key_id="k", client_bucket="cline", project_key="p" * 32)
        store.append_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="denied", provider="openai", model="gpt-4o",
        )
        updated = store.update_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="ok", tokens_in=0, tokens_out=0,
        )
        store.close()
        assert updated["tokens_in"] == 0
        assert updated["tokens_out"] == 0

    def test_none_tokens_still_accepted(self, tmp_path):
        store = _store(tmp_path)
        conv = store.create(key_id="k", client_bucket="cline", project_key="p" * 32)
        store.append_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="denied", provider="openai", model="gpt-4o",
        )
        updated = store.update_turn(
            conversation_id=conv["id"], key_id="k", seq=1,
            outcome="failed",
        )
        store.close()
        assert updated["tokens_in"] is None
        assert updated["tokens_out"] is None


# ---------------------------------------------------------------------------
# F-5: TurnContext.update() token preservation regression tests
# ---------------------------------------------------------------------------


class TestTurnContextUpdateTokenPreservation:
    def test_update_without_tokens_preserves_existing(self):
        """update(outcome='ok') should not erase previously committed tokens."""
        flusher = FakeFlusher()
        coord = _coordinator(flusher=flusher)
        turn = coord.start(
            key_id="k", client_bucket="cline", project_key="p" * 32,
        )
        coord.commit(
            turn, provider="openai", model="gpt-4o", outcome="denied",
            tokens_in=100, tokens_out=50, latency_ms=200,
        )
        # Update outcome without providing tokens
        coord.update(turn, outcome="ok")

        state = coord._states[("k", turn.conversation_id)]
        record = state.committed_turns[-1]
        assert record["outcome"] == "ok"
        assert record["tokens_in"] == 100
        assert record["tokens_out"] == 50
        assert record["latency_ms"] == 200

    def test_update_with_tokens_overwrites_existing(self):
        """update(tokens_in=X) should overwrite when explicitly provided."""
        flusher = FakeFlusher()
        coord = _coordinator(flusher=flusher)
        turn = coord.start(
            key_id="k", client_bucket="cline", project_key="p" * 32,
        )
        coord.commit(
            turn, provider="openai", model="gpt-4o", outcome="denied",
            tokens_in=100, tokens_out=50,
        )
        # Update with new token values
        coord.update(turn, outcome="ok", tokens_in=200, tokens_out=100)

        state = coord._states[("k", turn.conversation_id)]
        record = state.committed_turns[-1]
        assert record["tokens_in"] == 200
        assert record["tokens_out"] == 100

    def test_update_partial_token_preservation(self):
        """update(tokens_in=X) should preserve tokens_out when not provided."""
        flusher = FakeFlusher()
        coord = _coordinator(flusher=flusher)
        turn = coord.start(
            key_id="k", client_bucket="cline", project_key="p" * 32,
        )
        coord.commit(
            turn, provider="openai", model="gpt-4o", outcome="denied",
            tokens_in=100, tokens_out=50, latency_ms=300,
        )
        # Update only outcome and tokens_in
        coord.update(turn, outcome="ok", tokens_in=200)

        state = coord._states[("k", turn.conversation_id)]
        record = state.committed_turns[-1]
        assert record["outcome"] == "ok"
        assert record["tokens_in"] == 200
        assert record["tokens_out"] == 50  # preserved
        assert record["latency_ms"] == 300  # preserved
