"""
P9b tests: ContextManager estimation, budget split, compaction split,
tail serialization, overflow-retry decision, determinism and no-raise
guarantees.
"""

import pytest

from app.models.continuity import CompactionMethod, CompactionReason
from app.providers.exceptions import ProviderHTTPError
from app.services.context_manager import ContextManager, ContextOverflowSignal


def _turns(count, *, cost=100, conv="c1"):
    return [
        {
            "conversation_id": conv,
            "seq": i,
            "provider": "p1",
            "model": "m1",
            "outcome": "ok",
            "task": "probe",
            "tokens_in": cost,
            "tokens_out": 0,
            "latency_ms": 10,
            "ts": 1000.0 + i,
        }
        for i in range(1, count + 1)
    ]


def _manager(**overrides):
    kwargs = {
        "char_token_ratio": 4,
        "context_token_budget": 1000,
        "output_reserve_tokens": 200,
        "summary_share": 0.4,
        "summary_max_chars": 4096,
        "tail_max_items": 5,
    }
    kwargs.update(overrides)
    return ContextManager(**kwargs)


class TestEstimateTokens:
    def test_math_with_ratio_four(self):
        mgr = _manager()
        assert mgr.estimate_tokens("") == 1
        assert mgr.estimate_tokens("a") == 1
        assert mgr.estimate_tokens("abcd") == 1
        assert mgr.estimate_tokens("abcdefgh") == 2

    def test_unicode_boundary(self):
        mgr = _manager()
        assert mgr.estimate_tokens("é" * 4) == 1
        assert mgr.estimate_tokens("é" * 8) == 2

    def test_none_is_treated_as_empty(self):
        mgr = _manager()
        assert mgr.estimate_tokens(None) == 1

    def test_default_ratio_smoke(self):
        assert ContextManager().estimate_tokens("hello world") >= 1


class TestBudgetSplit:
    def test_default_split(self):
        mgr = _manager()
        summary, tail = mgr.budget_split(1000, 200, 0.4)
        assert summary == 320
        assert tail == 480

    def test_share_zero(self):
        mgr = _manager()
        summary, tail = mgr.budget_split(1000, 200, 0.0)
        assert summary == 0
        assert tail == 800

    def test_share_one(self):
        mgr = _manager()
        summary, tail = mgr.budget_split(1000, 200, 1.0)
        assert summary == 800
        assert tail == 0

    def test_budget_below_reserve_clamps_to_zero(self):
        mgr = _manager()
        summary, tail = mgr.budget_split(100, 200, 0.4)
        assert summary == 0
        assert tail == 0


class TestCompact:
    def test_tail_then_summary_split(self):
        mgr = _manager()
        result = mgr.compact(
            _turns(20), now=1.0, reason=CompactionReason.PREFLIGHT.value
        )
        assert [t["seq"] for t in result.tail] == [17, 18, 19, 20]
        assert result.summary is not None
        assert result.summary.up_to_seq == 16
        assert result.method == CompactionMethod.SUMMARY_TAIL.value
        assert result.from_tokens == 2000
        assert result.tail_tokens == 400
        assert result.to_tokens == result.summary_tokens + result.tail_tokens

    def test_item_cap_beats_token_budget(self):
        mgr = _manager(tail_max_items=3)
        result = mgr.compact(_turns(20), now=1.0)
        assert [t["seq"] for t in result.tail] == [18, 19, 20]

    def test_small_conversation_stays_entirely_in_tail(self):
        mgr = _manager()
        result = mgr.compact(_turns(3), now=1.0)
        assert result.summary is None
        assert result.method == CompactionMethod.TAIL_ONLY.value
        assert [t["seq"] for t in result.tail] == [1, 2, 3]

    def test_empty_turns_never_raises(self):
        mgr = _manager()
        result = mgr.compact([], now=1.0)
        assert result.summary is None
        assert result.tail == []
        assert result.from_tokens == 0

    def test_adversarial_input_never_raises(self):
        mgr = _manager()
        adversarial = [
            None,
            "nope",
            42,
            {"seq": "not-an-int"},
            {"seq": 3, "tokens_in": {"weird": True}},
            {},
        ]
        result = mgr.compact(adversarial, budget=0, now=1.0)
        assert isinstance(result.summary, object)
        assert result.from_tokens >= 0
        assert result.to_tokens >= 0

    def test_adversarial_params_never_raise(self):
        mgr = _manager()
        result = mgr.compact(
            _turns(5),
            params={
                "summary_share": 5.0,
                "tail_max_items": -3,
                "summary_max_chars": 0,
            },
            now=1.0,
        )
        assert result.from_tokens >= 0

    def test_determinism_same_input_same_output(self):
        mgr = _manager()
        turns = _turns(20)
        first = mgr.compact(turns, now=1234.0)
        second = mgr.compact(list(turns), now=1234.0)
        assert first == second
        assert first.to_dict() == second.to_dict()

    def test_overflow_degrades_within_budget(self):
        mgr = _manager(summary_max_chars=64)
        result = mgr.compact(_turns(20), now=1.0)
        assert result.summary is not None
        assert len(result.summary.content) <= 64

    def test_result_export_passes_memory_contract(self):
        from app.services.memory_contract import contains_never_captured

        mgr = _manager()
        result = mgr.compact(_turns(20), now=1.0)
        assert not contains_never_captured(result.to_dict())


class TestSerializeTail:
    def test_deterministic_and_bounded(self):
        from app.services.memory_contract import contains_never_captured

        mgr = _manager()
        result = mgr.compact(_turns(20), now=1.0)
        first = mgr.serialize_tail(result.tail)
        second = mgr.serialize_tail(list(result.tail))
        assert first == second
        assert len(first) <= mgr.summary_max_chars
        assert not contains_never_captured(first)

    def test_empty_tail(self):
        mgr = _manager()
        assert mgr.serialize_tail([]) == "[]"


class TestShouldRetryCompacted:
    def test_overflow_signal_retries(self):
        mgr = _manager()
        assert mgr.should_retry_compacted(ContextOverflowSignal()) is True

    def test_http_context_length_retries(self):
        mgr = _manager()
        error = ProviderHTTPError(400, "maximum context length is 4096 tokens")
        assert mgr.should_retry_compacted(error) is True

    def test_http_server_error_degrades(self):
        mgr = _manager()
        error = ProviderHTTPError(500, "internal error")
        assert mgr.should_retry_compacted(error) is False

    def test_unrelated_exception_degrades(self):
        mgr = _manager()
        assert mgr.should_retry_compacted(RuntimeError("boom")) is False
        assert mgr.should_retry_compacted(None) is False
