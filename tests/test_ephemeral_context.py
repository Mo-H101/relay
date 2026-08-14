"""
P9f tests: ephemeral content context (Phase 5/6).

Covers the pure content-summary and in-request compaction helpers:
bounded/redacted digests, token estimation, budget-driven compaction
with a deterministic tail, malformed-input no-ops, and the data-marking
frame that neutralizes instruction-shaped digest text.
"""

import pytest

from app.services.context_manager import ContextManager
from app.services.ephemeral_context import (
    compact,
    content_summary,
    estimate_messages_tokens,
    message_text,
)

_SECRET = "sk-abcdefghijklmnop"


def _manager(**kwargs):
    defaults = dict(
        char_token_ratio=4,
        context_token_budget=16,
        output_reserve_tokens=0,
        summary_share=0.5,
        summary_max_chars=4096,
        tail_max_items=2,
    )
    defaults.update(kwargs)
    return ContextManager(**defaults)


def _msgs(*contents, roles=None):
    roles = roles or ["user", "assistant"]
    return [
        {"role": roles[i % len(roles)], "content": content}
        for i, content in enumerate(contents)
    ]


class TestMessageText:
    def test_plain_string(self):
        assert message_text("hello") == "hello"

    def test_list_of_parts(self):
        content = [
            {"type": "text", "text": "look at "},
            {"type": "text", "text": "this"},
        ]
        assert message_text(content) == "look at  this"

    def test_none_and_falsy(self):
        assert message_text(None) == ""
        assert message_text("") == ""

    def test_non_string_scalar(self):
        assert message_text(42) == "42"


class TestContentSummary:
    def test_counts_and_first_last_user(self):
        summary = content_summary(
            _msgs("first request", "first answer", "second request"),
            max_chars=4096,
        )
        assert "messages: 3" in summary
        assert "first user request: first request" in summary
        assert "latest user request: second request" in summary
        assert "assistant responses: 1" in summary

    def test_no_assistant_lines_when_none(self):
        summary = content_summary(_msgs("only a user message"), max_chars=4096)
        assert "assistant responses" not in summary
        assert "messages: 1" in summary

    def test_bounded_by_max_chars(self):
        summary = content_summary(
            _msgs("x" * 5000), max_chars=200
        )
        assert len(summary) <= 200
        assert summary.endswith("...(truncated)")

    def test_redacts_secret_shapes(self):
        summary = content_summary(_msgs(f"key {_SECRET}"), max_chars=4096)
        assert _SECRET not in summary
        assert "<redacted>" in summary

    def test_empty_and_malformed_degrade(self):
        assert content_summary([]) == ""
        assert content_summary(None) == ""
        assert content_summary([{"role": "user"}]) != ""


class TestEstimateTokens:
    def test_deterministic_and_nonzero(self):
        manager = _manager()
        a = estimate_messages_tokens(_msgs("hello world"), manager)
        b = estimate_messages_tokens(_msgs("hello world"), manager)
        assert a == b > 0

    def test_empty_degrades_to_zero(self):
        assert estimate_messages_tokens([], _manager()) == 0
        assert estimate_messages_tokens(None, _manager()) == 0


class TestCompact:
    def test_fits_budget_forwards_unchanged(self):
        manager = _manager(context_token_budget=64)
        messages = _msgs("hello", "hi there")
        replacement, stats = compact(messages, manager=manager)
        assert replacement is None
        assert stats["compacted"] is False
        assert stats["from_tokens"] == stats["to_tokens"]

    def test_over_budget_produces_digest_and_tail(self):
        manager = _manager(context_token_budget=32)
        messages = [
            {"role": "user", "content": "first request text for compaction"},
            {"role": "assistant", "content": "first answer text for compaction"},
            {"role": "user", "content": "second request text"},
        ]
        replacement, stats = compact(messages, manager=manager)
        assert replacement is not None
        assert stats["compacted"] is True
        assert stats["omitted_count"] == 2
        assert stats["tail_count"] == 1

        # The newest message survives verbatim; the older ones are folded
        # into a leading redacted digest system message.
        assert replacement[-1] == messages[-1]
        assert replacement[0]["role"] == "system"
        assert "[summary of earlier conversation content" in replacement[0]["content"]
        assert "user: first request text for compaction" in replacement[0]["content"]
        assert messages[0] not in replacement

    def test_tail_respects_item_cap(self):
        # char_token_ratio=1 makes estimates exact in characters; the tail
        # token budget alone would hold two messages, so the item cap is
        # the binding constraint.
        manager = _manager(
            char_token_ratio=1,
            context_token_budget=200,
            summary_share=0.5,
            tail_max_items=1,
        )
        messages = _msgs(*[f"aaaaaaaaaaaaaaaaaaaa (message {i})" for i in range(6)])
        replacement, stats = compact(messages, manager=manager)
        assert stats["compacted"] is True
        assert stats["tail_count"] == 1
        assert replacement[-1] == messages[-1]

    def test_digest_is_redacted(self):
        manager = _manager(context_token_budget=32)
        messages = _msgs(f"secret {_SECRET}", f"answer {_SECRET}", "latest request")
        replacement, _stats = compact(messages, manager=manager)
        assert replacement is not None
        assert _SECRET not in replacement[0]["content"]
        assert "<redacted>" in replacement[0]["content"]

    def test_empty_and_malformed_degrade(self):
        replacement, stats = compact([], manager=_manager())
        assert replacement is None
        assert stats["compacted"] is False
        replacement, stats = compact(None, manager=_manager())
        assert replacement is None
        assert stats["compacted"] is False

    def test_deterministic(self):
        manager = _manager(context_token_budget=32)
        messages = _msgs(
            "first request text for compaction",
            "first answer text for compaction",
            "second request text",
        )
        r1, _ = compact(messages, manager=manager)
        r2, _ = compact(messages, manager=manager)
        assert r1 == r2
