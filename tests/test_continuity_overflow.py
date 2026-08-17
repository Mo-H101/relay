"""
Phase 10B tests: overflow-retry in the synchronous and asynchronous chat
execution paths.

Cases A–E:
  A. Provider returns context-overflow error → envelope rebuilt with
     aggressive compaction via ``rebuild_for_overflow`` → succeeds.
  B. Provider returns context-overflow error, retry also overflows →
     fail (exactly one overflow retry per candidate).
  C. Provider returns non-overflow error → no compaction/retry, normal
     retry behaviour.
  D. Provider returns context-overflow error but no turn → no
     compaction/retry, normal failover.
  E. Provider returns context-overflow error, rebuild fails →
     no retry, normal error path.

Additional:
  F. rebuild_for_overflow produces a different (more compact) envelope.
  G. Empty-envelope turn still works after rebuild.
"""

import asyncio
import pytest

from unittest.mock import patch, MagicMock, call

from app.providers.base import Provider
from app.services.context_manager import ContextManager, ContextOverflowSignal
from app.services.handoff import HandoffCoordinator, TurnContext
from app.services.metrics import relay_metrics
from tests.test_continuity_handoff import FakeFlusher

def _provider(name="p1", models=None):
    return Provider(
        name=name,
        base_url=f"https://{name}.invalid",
        api_key="test-key",
        enabled=True,
        priority=1,
        models=models or ["m1"],
    )


class _OverflowError(Exception):
    """ProviderError-like overflow that matches marker strings."""
    def __init__(self, msg="request too large: context_length exceeded"):
        super().__init__(msg)


class _NonOverflowError(Exception):
    """Non-overflow provider error."""
    def __init__(self, msg="rate limited"):
        super().__init__(msg)


class _FakeClient:
    """Queue-based fake; pops exceptions or return values in order."""

    def __init__(self):
        self._outcomes = []
        self.chat_calls = []
        self.chat_messages_calls = []

    def set_outcomes(self, outcomes):
        self._outcomes = list(outcomes)

    def chat(self, provider, model, message, **kwargs):
        self.chat_calls.append((provider.name, model, message))
        if not self._outcomes:
            raise RuntimeError("no outcome configured")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def achat(self, provider, model, message, **kwargs):
        self.chat_calls.append((provider.name, model, message))
        if not self._outcomes:
            raise RuntimeError("no outcome configured")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def chat_messages(self, provider, payload):
        self.chat_calls.append((provider.name, payload["model"]))
        self.chat_messages_calls.append(dict(payload))
        if not self._outcomes:
            raise RuntimeError("no outcome configured")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, dict):
            return outcome
        model = payload["model"]
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 1700000000,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": str(outcome)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    async def achat_messages(self, provider, payload):
        return self.chat_messages(provider, payload)


class _FakeRegistry:
    def __init__(self, client):
        self._client = client

    def get(self, identity):
        return self._client


def _make_svc(cls, client):
    svc = cls()
    svc.registry = _FakeRegistry(client)
    return svc


def _make_turn(coordinator=None):
    """Build a minimal TurnContext with a real ContextManager and a fake envelope."""
    ctx = TurnContext(
        conversation_id="conv-overflow-test",
        key_id="key-1",
        client_bucket="opencode",
        project_key="proj-x",
    )
    ctx.context_manager = ContextManager(
        char_token_ratio=4,
        context_token_budget=10000,
        output_reserve_tokens=200,
        summary_share=0.4,
        summary_max_chars=4096,
        tail_max_items=5,
    )
    ctx.envelope = {
        "conversation_id": "conv-overflow-test",
        "summary": None,
        "summary_version": 1,
        "turns": [],
        "model_chain": [],
        "turn_count": 0,
        "tokens_budget": 10000,
        "tokens_used": 5000,
    }
    if coordinator is not None:
        ctx._handoff = coordinator
    return ctx


def _make_turn_with_committed_turns(num_turns=4):
    """Build a TurnContext backed by a real coordinator with committed turns
    so that ``rebuild_for_overflow`` can look up and rebuild the envelope.
    """
    flusher = FakeFlusher()
    manager = ContextManager(
        char_token_ratio=4,
        context_token_budget=10000,
        output_reserve_tokens=200,
        summary_share=0.4,
        summary_max_chars=4096,
        tail_max_items=5,
    )
    coord = HandoffCoordinator(flusher=flusher, context_manager=manager)
    cid = "c" * 32
    t1 = coord.start(
        key_id="key-1",
        client_bucket="opencode",
        project_key="proj-x",
        conversation_id=cid,
    )
    for i in range(num_turns):
        coord.commit(t1, provider="p", model=f"m{i}", tokens_in=200, tokens_out=100)
    t2 = coord.start(
        key_id="key-1",
        client_bucket="opencode",
        project_key="proj-x",
        conversation_id=cid,
    )
    t2.context_manager = manager
    return t2


# ---------------------------------------------------------------------------
# Sync chat_across tests
# ---------------------------------------------------------------------------

class TestOverflowRetrySync:

    def test_overflow_retry_succeeds(self):
        """Case A: overflow → rebuild_for_overflow → success."""
        from app.services.chat_service import ChatService

        client = _FakeClient()
        client.set_outcomes([
            _OverflowError(),                          # attempt 1: overflow
            "compacted reply",                         # attempt 2: success
        ])
        svc = _make_svc(ChatService, client)
        turn = _make_turn()

        with patch.object(
            turn.context_manager, "should_retry_compacted", return_value=True
        ), patch.object(
            turn, "rebuild_for_overflow"
        ) as rebuild:
            result = svc.chat_across(
                [(_provider(), "m1")],
                "hello",
                max_retries=0,
                turn=turn,
            )

        assert result["success"] is True
        assert result["response"] == "compacted reply"
        assert len(result["attempts"]) == 2
        rebuild.assert_called_once()

    def test_overflow_retry_exhausted(self):
        """Case B: overflow → retry also overflows → fail, only one retry."""
        from app.services.chat_service import ChatService

        client = _FakeClient()
        client.set_outcomes([
            _OverflowError(),   # attempt 1: overflow
            _OverflowError(),   # attempt 2 (overflow retry): overflow again
        ])
        svc = _make_svc(ChatService, client)
        turn = _make_turn()

        with patch.object(
            turn.context_manager, "should_retry_compacted", return_value=True
        ):
            result = svc.chat_across(
                [(_provider(), "m1")],
                "hello",
                max_retries=0,
                turn=turn,
            )

        assert result["success"] is False
        assert len(result["attempts"]) == 2

    def test_non_overflow_error_no_retry(self):
        """Case C: non-overflow error → no compaction retry."""
        from app.services.chat_service import ChatService

        client = _FakeClient()
        client.set_outcomes([
            _NonOverflowError(),  # rate limited
        ])
        svc = _make_svc(ChatService, client)
        turn = _make_turn()

        with patch.object(
            turn, "rebuild_for_overflow"
        ) as rebuild:
            result = svc.chat_across(
                [(_provider(), "m1")],
                "hello",
                max_retries=0,
                turn=turn,
            )

        assert result["success"] is False
        rebuild.assert_not_called()
        assert len(result["attempts"]) == 1

    def test_overflow_no_turn_no_retry(self):
        """Case D: overflow error but no turn → normal failover."""
        from app.services.chat_service import ChatService

        client = _FakeClient()
        client.set_outcomes([
            _OverflowError(),
        ])
        svc = _make_svc(ChatService, client)

        result = svc.chat_across(
            [(_provider(), "m1")],
            "hello",
            max_retries=0,
        )

        assert result["success"] is False
        assert len(result["attempts"]) == 1


# ---------------------------------------------------------------------------
# Async achat_across tests
# ---------------------------------------------------------------------------

class TestOverflowRetryAsync:

    @pytest.mark.asyncio
    async def test_overflow_retry_succeeds(self):
        """Case A (async): overflow → rebuild_for_overflow → success."""
        from app.services.async_chat_service import AsyncChatService

        client = _FakeClient()
        client.set_outcomes([
            _OverflowError(),
            "compacted reply async",
        ])
        svc = _make_svc(AsyncChatService, client)
        turn = _make_turn()

        with patch.object(
            turn.context_manager, "should_retry_compacted", return_value=True
        ), patch.object(
            turn, "rebuild_for_overflow"
        ) as rebuild:
            result = await svc.achat_across(
                [(_provider(), "m1")],
                "hello",
                max_retries=0,
                turn=turn,
            )

        assert result["success"] is True
        assert result["response"] == "compacted reply async"
        assert len(result["attempts"]) == 2
        rebuild.assert_called_once()

    @pytest.mark.asyncio
    async def test_overflow_retry_exhausted(self):
        """Case B (async): overflow → retry → overflow → fail."""
        from app.services.async_chat_service import AsyncChatService

        client = _FakeClient()
        client.set_outcomes([
            _OverflowError(),
            _OverflowError(),
        ])
        svc = _make_svc(AsyncChatService, client)
        turn = _make_turn()

        with patch.object(
            turn.context_manager, "should_retry_compacted", return_value=True
        ):
            result = await svc.achat_across(
                [(_provider(), "m1")],
                "hello",
                max_retries=0,
                turn=turn,
            )

        assert result["success"] is False
        assert len(result["attempts"]) == 2

    @pytest.mark.asyncio
    async def test_non_overflow_no_retry(self):
        """Case C (async): non-overflow → no compaction retry."""
        from app.services.async_chat_service import AsyncChatService

        client = _FakeClient()
        client.set_outcomes([
            _NonOverflowError(),
        ])
        svc = _make_svc(AsyncChatService, client)
        turn = _make_turn()

        with patch.object(
            turn, "rebuild_for_overflow"
        ) as rebuild:
            result = await svc.achat_across(
                [(_provider(), "m1")],
                "hello",
                max_retries=0,
                turn=turn,
            )

        assert result["success"] is False
        rebuild.assert_not_called()
        assert len(result["attempts"]) == 1


# ---------------------------------------------------------------------------
# Messages-variant tests (inject_payload path)
# ---------------------------------------------------------------------------

class TestOverflowRetryMessages:

    def test_sync_messages_overflow_retry(self):
        """Case A (sync messages): overflow → rebuild_for_overflow with payload re-injection."""
        from app.services.chat_service import ChatService

        client = _FakeClient()
        client.set_outcomes([
            _OverflowError(),
            "compacted reply msgs",
        ])
        svc = _make_svc(ChatService, client)
        turn = _make_turn()
        payload = {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}

        with patch.object(
            turn.context_manager, "should_retry_compacted", return_value=True
        ), patch.object(
            turn, "rebuild_for_overflow"
        ) as rebuild:
            result = svc.chat_across_messages(
                [(_provider(), "m1")],
                payload,
                max_retries=0,
                turn=turn,
            )

        assert result["success"] is True
        assert len(result["attempts"]) == 2
        rebuild.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_messages_overflow_retry(self):
        """Case A (async messages): overflow → rebuild_for_overflow with payload re-injection."""
        from app.services.async_chat_service import AsyncChatService

        client = _FakeClient()
        client.set_outcomes([
            _OverflowError(),
            "compacted reply async msgs",
        ])
        svc = _make_svc(AsyncChatService, client)
        turn = _make_turn()
        payload = {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}

        with patch.object(
            turn.context_manager, "should_retry_compacted", return_value=True
        ), patch.object(
            turn, "rebuild_for_overflow"
        ) as rebuild:
            result = await svc.achat_across_messages(
                [(_provider(), "m1")],
                payload,
                max_retries=0,
                turn=turn,
            )

        assert result["success"] is True
        assert len(result["attempts"]) == 2
        rebuild.assert_called_once()


# ---------------------------------------------------------------------------
# Edge: overflow metrics
# ---------------------------------------------------------------------------

class TestOverflowMetrics:

    def test_metrics_inc_on_overflow_retry(self):
        """Counter increments exactly once per overflow retry."""
        from app.services.chat_service import ChatService

        client = _FakeClient()
        client.set_outcomes([
            _OverflowError(),
            "ok",
        ])
        svc = _make_svc(ChatService, client)
        turn = _make_turn()

        with patch.object(
            turn.context_manager, "should_retry_compacted", return_value=True
        ), patch.object(
            relay_metrics.continuity_overflow_retries, "inc"
        ) as inc:
            svc.chat_across(
                [(_provider(), "m1")],
                "hello",
                max_retries=0,
                turn=turn,
            )

        inc.assert_called_once()


# ---------------------------------------------------------------------------
# Edge: _exc on Attempt
# ---------------------------------------------------------------------------

class TestAttemptExc:

    def test_exc_preserved_on_failure(self):
        """_exc is set on Attempt for non-overflow errors too."""
        from app.services.chat_service import ChatService

        client = _FakeClient()
        client.set_outcomes([
            _NonOverflowError("rate limited"),
        ])
        svc = _make_svc(ChatService, client)

        result = svc.chat_across(
            [(_provider(), "m1")],
            "hello",
            max_retries=0,
        )

        assert result["success"] is False
        attempt_dict = result["attempts"][0]
        assert "rate limited" in attempt_dict["reason"]

    def test_exc_not_set_on_success(self):
        """_exc is None on successful Attempt."""
        from app.services.chat_service import ChatService

        client = _FakeClient()
        client.set_outcomes(["hello world"])
        svc = _make_svc(ChatService, client)

        result = svc.chat_across(
            [(_provider(), "m1")],
            "hello",
            max_retries=0,
        )

        assert result["success"] is True
        attempt_dict = result["attempts"][0]
        assert "exc" not in attempt_dict or attempt_dict.get("exc") is None


# ---------------------------------------------------------------------------
# rebuild_for_overflow: envelope actually changes (Case F)
# ---------------------------------------------------------------------------

class TestRebuildEnvelopeChanges:

    def test_rebuild_for_overflow_changes_envelope(self):
        """rebuild_for_overflow produces a different, more compact envelope."""
        turn = _make_turn_with_committed_turns(4)
        original_envelope = dict(turn.envelope)
        original_tail = original_envelope.get("tail", "")
        had_compacted = "compacted" in original_envelope

        turn.rebuild_for_overflow()

        assert turn.envelope is not None
        assert turn.envelope != original_envelope
        # The rebuilt envelope must be re-compacted (has "compacted" field)
        assert "compacted" in turn.envelope
        # The tail must have been re-truncated (fewer items after aggressive compaction)
        assert turn.envelope.get("tail", "") != original_tail or not had_compacted

    def test_rebuild_for_overflow_empty_envelope(self):
        """Case G: turn with no envelope → rebuild_for_overflow sets one."""
        turn = _make_turn_with_committed_turns(2)
        turn.envelope = None
        turn._injected_payload = None

        turn.rebuild_for_overflow()

        assert turn.envelope is not None
        assert "conversation_id" in turn.envelope

    def test_rebuild_for_overflow_clears_payload_cache(self):
        """Payload cache is cleared so inject_* sees the new envelope."""
        turn = _make_turn_with_committed_turns(2)
        turn._injected_payload = {"old": "payload"}

        turn.rebuild_for_overflow()

        assert turn._injected_payload is None


# ---------------------------------------------------------------------------
# One-retry invariant
# ---------------------------------------------------------------------------

class TestOneRetryInvariant:

    def test_at_most_one_overflow_retry(self):
        """Even with overflow errors, only one overflow retry fires per candidate."""
        from app.services.chat_service import ChatService

        call_count = 0

        def counting_chat(provider, model, message, **kwargs):
            nonlocal call_count
            call_count += 1
            raise _OverflowError()

        client = _FakeClient()
        client.chat = counting_chat
        svc = _make_svc(ChatService, client)
        turn = _make_turn()

        with patch.object(
            turn.context_manager, "should_retry_compacted", return_value=True
        ):
            result = svc.chat_across(
                [(_provider(), "m1")],
                "hello",
                max_retries=0,
                turn=turn,
            )

        assert result["success"] is False
        # 1 initial + 1 overflow retry = 2 calls
        assert call_count == 2


# ---------------------------------------------------------------------------
# Double-envelope regression tests (Phase 10B fix)
# ---------------------------------------------------------------------------

class TestDoubleEnvelopeRegression:
    """
    Verify that overflow retry re-injects using the ORIGINAL pre-injection
    input, so the retry payload contains exactly ONE envelope — never
    [new_envelope, old_envelope, original_content].
    """

    # -- A. String/message path (sync) ------------------------------------

    def test_string_overflow_retry_single_envelope_sync(self):
        """Sync chat_across: after overflow retry, the message sent to the
        provider contains exactly one [continuity context] block."""
        from app.services.chat_service import ChatService

        client = _FakeClient()
        client.set_outcomes([
            _OverflowError(),
            "compacted reply",
        ])
        svc = _make_svc(ChatService, client)
        turn = _make_turn_with_committed_turns(4)

        result = svc.chat_across(
            [(_provider(), "m1")],
            "hello",
            max_retries=0,
            turn=turn,
        )

        assert result["success"] is True
        assert len(result["attempts"]) == 2

        # The second call (retry) is the one that succeeded.
        retry_message = client.chat_calls[1][2]
        envelope_count = retry_message.count("[continuity context]")
        assert envelope_count == 1, (
            f"Expected exactly 1 envelope in retry message, got {envelope_count}"
        )
        assert retry_message.endswith("hello"), (
            "Original user message must appear at the end"
        )

    def test_string_no_double_envelope_regression_sync(self):
        """Regression: overflow retry must NOT produce
        [new_envelope, old_envelope, original_message]."""
        from app.services.chat_service import ChatService

        client = _FakeClient()
        client.set_outcomes([
            _OverflowError(),
            "ok",
        ])
        svc = _make_svc(ChatService, client)
        turn = _make_turn_with_committed_turns(4)

        result = svc.chat_across(
            [(_provider(), "m1")],
            "hello",
            max_retries=0,
            turn=turn,
        )

        assert result["success"] is True
        retry_message = client.chat_calls[1][2]

        # There must be exactly one envelope.
        assert retry_message.count("[continuity context]") == 1

    # -- A2. String/message path (async) ----------------------------------

    @pytest.mark.asyncio
    async def test_string_overflow_retry_single_envelope_async(self):
        """Async achat_across: after overflow retry, the message contains
        exactly one [continuity context] block."""
        from app.services.async_chat_service import AsyncChatService

        client = _FakeClient()
        client.set_outcomes([
            _OverflowError(),
            "compacted reply async",
        ])
        svc = _make_svc(AsyncChatService, client)
        turn = _make_turn_with_committed_turns(4)

        result = await svc.achat_across(
            [(_provider(), "m1")],
            "hello",
            max_retries=0,
            turn=turn,
        )

        assert result["success"] is True
        retry_message = client.chat_calls[1][2]
        envelope_count = retry_message.count("[continuity context]")
        assert envelope_count == 1, (
            f"Expected exactly 1 envelope in retry message, got {envelope_count}"
        )
        assert retry_message.endswith("hello"), (
            "Original user message must appear at the end"
        )

    # -- B. Dict/messages path (sync) -------------------------------------

    def test_messages_overflow_retry_single_envelope_sync(self):
        """Sync chat_across_messages: after overflow retry, the payload
        contains exactly one envelope system message."""
        from app.services.chat_service import ChatService

        client = _FakeClient()
        client.set_outcomes([
            _OverflowError(),
            "compacted reply msgs",
        ])
        svc = _make_svc(ChatService, client)
        turn = _make_turn_with_committed_turns(4)
        payload = {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}

        result = svc.chat_across_messages(
            [(_provider(), "m1")],
            payload,
            max_retries=0,
            turn=turn,
        )

        assert result["success"] is True
        assert len(result["attempts"]) == 2

        # Inspect the actual payload sent on the retry call.
        retry_payload = client.chat_messages_calls[-1]
        messages = retry_payload["messages"]

        # Exactly one system message (the envelope).
        system_msgs = [m for m in messages if m["role"] == "system"]
        assert len(system_msgs) == 1, (
            f"Expected exactly 1 system (envelope) message, got {len(system_msgs)}"
        )

        # The original user message must appear exactly once.
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 1, (
            f"Expected exactly 1 user message, got {len(user_msgs)}"
        )
        assert user_msgs[0]["content"] == "hi"

    def test_messages_no_double_envelope_regression_sync(self):
        """Regression: overflow retry must NOT produce
        [new_envelope_sys, old_envelope_sys, ...original_messages]."""
        from app.services.chat_service import ChatService

        client = _FakeClient()
        client.set_outcomes([
            _OverflowError(),
            "ok",
        ])
        svc = _make_svc(ChatService, client)
        turn = _make_turn_with_committed_turns(4)
        payload = {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}

        result = svc.chat_across_messages(
            [(_provider(), "m1")],
            payload,
            max_retries=0,
            turn=turn,
        )

        assert result["success"] is True
        retry_payload = client.chat_messages_calls[-1]
        messages = retry_payload["messages"]
        system_msgs = [m for m in messages if m["role"] == "system"]
        assert len(system_msgs) == 1

    # -- B2. Dict/messages path (async) -----------------------------------

    @pytest.mark.asyncio
    async def test_messages_overflow_retry_single_envelope_async(self):
        """Async achat_across_messages: after overflow retry, the payload
        contains exactly one envelope system message."""
        from app.services.async_chat_service import AsyncChatService

        client = _FakeClient()
        client.set_outcomes([
            _OverflowError(),
            "compacted reply async msgs",
        ])
        svc = _make_svc(AsyncChatService, client)
        turn = _make_turn_with_committed_turns(4)
        payload = {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}

        result = await svc.achat_across_messages(
            [(_provider(), "m1")],
            payload,
            max_retries=0,
            turn=turn,
        )

        assert result["success"] is True
        retry_payload = client.chat_messages_calls[-1]
        messages = retry_payload["messages"]

        system_msgs = [m for m in messages if m["role"] == "system"]
        assert len(system_msgs) == 1, (
            f"Expected exactly 1 system (envelope) message, got {len(system_msgs)}"
        )

        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 1
        assert user_msgs[0]["content"] == "hi"

    # -- D. One-overflow-retry invariant ----------------------------------

    def test_one_overflow_retry_invariant_still_holds_string(self):
        """Exactly one overflow retry fires per candidate (string path)."""
        from app.services.chat_service import ChatService

        call_count = 0

        def counting_chat(provider, model, message, **kwargs):
            nonlocal call_count
            call_count += 1
            raise _OverflowError()

        client = _FakeClient()
        client.chat = counting_chat
        svc = _make_svc(ChatService, client)
        turn = _make_turn_with_committed_turns(4)

        result = svc.chat_across(
            [(_provider(), "m1")],
            "hello",
            max_retries=0,
            turn=turn,
        )

        assert result["success"] is False
        assert call_count == 2  # 1 initial + 1 overflow retry

    def test_one_overflow_retry_invariant_still_holds_messages(self):
        """Exactly one overflow retry fires per candidate (messages path)."""
        from app.services.chat_service import ChatService

        call_count = 0

        def counting_chat_messages(provider, payload):
            nonlocal call_count
            call_count += 1
            raise _OverflowError()

        client = _FakeClient()
        client.chat_messages = counting_chat_messages
        svc = _make_svc(ChatService, client)
        turn = _make_turn_with_committed_turns(4)
        payload = {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}

        result = svc.chat_across_messages(
            [(_provider(), "m1")],
            payload,
            max_retries=0,
            turn=turn,
        )

        assert result["success"] is False
        assert call_count == 2

    # -- E. Cache cleared by rebuild_for_overflow -------------------------

    def test_rebuild_clears_cache_inject_uses_new_envelope_string(self):
        """After rebuild_for_overflow, inject_message uses the NEW envelope,
        not the old cached one. The final message has exactly one envelope
        matching the rebuilt state."""
        from app.services.chat_service import ChatService

        client = _FakeClient()
        client.set_outcomes([
            _OverflowError(),
            "ok",
        ])
        svc = _make_svc(ChatService, client)
        turn = _make_turn_with_committed_turns(4)
        original_envelope = dict(turn.envelope)

        result = svc.chat_across(
            [(_provider(), "m1")],
            "hello",
            max_retries=0,
            turn=turn,
        )

        assert result["success"] is True
        # The envelope must have changed after rebuild.
        assert turn.envelope != original_envelope
        assert "compacted" in turn.envelope
        # The retry message must have exactly one envelope.
        retry_message = client.chat_calls[1][2]
        assert retry_message.count("[continuity context]") == 1

    def test_rebuild_clears_cache_inject_uses_new_envelope_messages(self):
        """After rebuild_for_overflow, inject_payload uses the NEW envelope
        in the system message, not the old cached one."""
        from app.services.chat_service import ChatService

        client = _FakeClient()
        client.set_outcomes([
            _OverflowError(),
            "ok",
        ])
        svc = _make_svc(ChatService, client)
        turn = _make_turn_with_committed_turns(4)
        original_envelope = dict(turn.envelope)
        payload = {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}

        result = svc.chat_across_messages(
            [(_provider(), "m1")],
            payload,
            max_retries=0,
            turn=turn,
        )

        assert result["success"] is True
        assert turn.envelope != original_envelope
        retry_payload = client.chat_messages_calls[-1]
        messages = retry_payload["messages"]
        system_msgs = [m for m in messages if m["role"] == "system"]
        assert len(system_msgs) == 1
        # The system message content must match the NEW envelope render.
        from app.services.handoff import render_envelope
        assert system_msgs[0]["content"] == render_envelope(turn.envelope)
