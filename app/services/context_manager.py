"""
P9b context manager: token estimation, budget split, compaction split,
tail serialization, and the overflow retry decision.

Pure logic; no I/O and never invoked from chat request paths in P9b.
Every knob defaults to the existing ``settings.continuity_*`` value and
can be overridden per-call for tests.
"""

import json
import time
from typing import Any, Callable, List, Optional, Tuple

from app.models.continuity import (
    CompactionMethod,
    CompactionReason,
    CompactionResult,
    SummaryBlock,
)
from app.providers.exceptions import ProviderHTTPError


class ContextOverflowSignal(Exception):
    """
    Marker for a context-overflow failure. Raised by P9c wiring; the
    P9b helper only decides on it.
    """


_CONTEXT_OVERFLOW_MARKERS = (
    "context length",
    "context_length",
    "context window",
    "maximum context",
    "max context",
    "too many tokens",
    "token limit",
    "exceeded the prompt",
)

_TAIL_TRUNCATION_SUFFIX = "...(tail truncated)"


def _turn_cost(turn: dict) -> int:
    """Nominal token cost of one turn's metadata (bounded, non-zero)."""
    try:
        cost = int(turn.get("tokens_in") or 0) + int(turn.get("tokens_out") or 0)
        return max(1, cost)
    except (TypeError, ValueError):
        return 1


def _normalize_turns(turns: Any) -> List[dict]:
    """Coerce arbitrary input into a deterministic, seq-sorted turn list."""
    if not isinstance(turns, (list, tuple)):
        return []

    out: List[dict] = []
    for item in turns:
        if not isinstance(item, dict):
            continue
        try:
            row = dict(item)
            row["seq"] = int(row.get("seq") or 0)
        except (TypeError, ValueError):
            continue
        row["conversation_id"] = str(row.get("conversation_id") or "")
        out.append(row)

    out.sort(key=lambda row: (row["seq"], str(row.get("ts") or "")))
    return out


class ContextManager:
    """
    Deterministic budget/compaction math for the P9 continuity layer.
    """

    def __init__(
        self,
        *,
        char_token_ratio: Optional[int] = None,
        context_token_budget: Optional[int] = None,
        output_reserve_tokens: Optional[int] = None,
        summary_share: Optional[float] = None,
        summary_max_chars: Optional[int] = None,
        tail_max_items: Optional[int] = None,
        summarizer_model: Optional[str] = None,
    ) -> None:
        from app.core.config import settings

        self.char_token_ratio = max(
            1,
            int(
                char_token_ratio
                if char_token_ratio is not None
                else settings.continuity_chars_per_token
            ),
        )
        self.context_token_budget = max(
            1,
            int(
                context_token_budget
                if context_token_budget is not None
                else settings.continuity_context_token_budget
            ),
        )
        self.output_reserve_tokens = max(
            0,
            int(
                output_reserve_tokens
                if output_reserve_tokens is not None
                else settings.continuity_output_reserve_tokens
            ),
        )
        self.summary_share = float(
            summary_share
            if summary_share is not None
            else settings.continuity_summary_share
        )
        self.summary_max_chars = max(
            1,
            int(
                summary_max_chars
                if summary_max_chars is not None
                else settings.continuity_summary_max_chars
            ),
        )
        self.tail_max_items = max(
            1,
            int(
                tail_max_items
                if tail_max_items is not None
                else settings.continuity_tail_max_items
            ),
        )
        self.summarizer_model = (
            (summarizer_model or "").strip()
            if summarizer_model is not None
            else str(settings.continuity_summarizer_model or "").strip()
        )

    def estimate_tokens(self, text: Any) -> int:
        """Estimate tokens as ``max(1, len(text) // ratio)``."""
        return max(1, len(str(text or "")) // self.char_token_ratio)

    def budget_split(
        self,
        budget: Optional[int] = None,
        reserve: Optional[int] = None,
        summary_share: Optional[float] = None,
    ) -> Tuple[int, int]:
        """
        Split the usable context into summary and tail token budgets.
        ``summary_budget = floor((budget - reserve) * share)``.
        """
        budget = int(
            budget if budget is not None else self.context_token_budget
        )
        reserve = int(
            reserve if reserve is not None else self.output_reserve_tokens
        )
        share = float(summary_share if summary_share is not None else self.summary_share)

        usable = max(0, budget - reserve)
        summary_budget = int(usable * share)
        tail_budget = usable - summary_budget
        return summary_budget, tail_budget

    def default_params(self) -> dict:
        """Settings-backed params bundle used by ``compact``."""
        return {
            "char_token_ratio": self.char_token_ratio,
            "context_token_budget": self.context_token_budget,
            "output_reserve_tokens": self.output_reserve_tokens,
            "summary_share": self.summary_share,
            "summary_max_chars": self.summary_max_chars,
            "tail_max_items": self.tail_max_items,
            "summarizer_model": self.summarizer_model,
        }

    def compact(
        self,
        turns: List[dict],
        budget: Optional[int] = None,
        params: Optional[dict] = None,
        *,
        summarizer: Optional[Callable[[List[dict], int], Optional[SummaryBlock]]] = None,
        now: Optional[float] = None,
        reason: str = CompactionReason.PREFLIGHT.value,
    ) -> CompactionResult:
        """
        Pure split: newest items into the tail (up to the tail token
        budget and the tail item cap); older items feed the summary.

        Never raises on any input; over-budget results degrade
        structurally (summary is truncated, tail is capped).
        """
        merged = dict(self.default_params())
        if isinstance(params, dict):
            merged.update(params)

        normalized = _normalize_turns(turns)
        from dataclasses import replace

        conversation_id = (
            normalized[0].get("conversation_id", "")
            if normalized
            else ""
        )
        at = float(now) if now is not None else time.time()

        if not normalized:
            return CompactionResult(
                conversation_id=conversation_id,
                up_to_seq=0,
                reason=reason,
                method=CompactionMethod.TAIL_ONLY.value,
                summary=None,
                tail=[],
                from_tokens=0,
                to_tokens=0,
                summary_tokens=0,
                tail_tokens=0,
                created_at=at,
            )

        budget = int(
            budget if budget is not None else merged["context_token_budget"]
        )
        summary_budget, tail_budget = self.budget_split(
            budget,
            merged["output_reserve_tokens"],
            merged["summary_share"],
        )
        tail_cap = max(0, int(merged["tail_max_items"]))

        tail: List[dict] = []
        tail_tokens = 0
        for turn in reversed(normalized):
            if len(tail) >= tail_cap:
                break
            cost = _turn_cost(turn)
            if tail_tokens + cost > tail_budget:
                break
            tail.append(turn)
            tail_tokens += cost
        tail.reverse()

        summarized = normalized[: len(normalized) - len(tail)]

        summary = None
        summary_tokens = 0
        if summarized:
            if summarizer is None:
                from app.services.summarizer import extractive_summarize

                block = extractive_summarize(
                    list(summarized), summary_budget, params=merged, now=at
                )
            else:
                block = summarizer(list(summarized), summary_budget)
            if block is not None:
                summary = replace(block, created_at=at)
                summary_tokens = int(summary.tokens_out or 0)

        if summarized:
            up_to_seq = max(turn["seq"] for turn in summarized)
            method = CompactionMethod.SUMMARY_TAIL.value
        elif tail:
            up_to_seq = max(turn["seq"] for turn in tail)
            method = CompactionMethod.TAIL_ONLY.value
        else:
            up_to_seq = 0
            method = CompactionMethod.TAIL_ONLY.value

        return CompactionResult(
            conversation_id=conversation_id,
            up_to_seq=up_to_seq,
            reason=reason,
            method=method,
            summary=summary,
            tail=tail,
            from_tokens=sum(_turn_cost(turn) for turn in normalized),
            to_tokens=summary_tokens + tail_tokens,
            summary_tokens=summary_tokens,
            tail_tokens=tail_tokens,
            created_at=at,
        )

    def serialize_tail(self, tail: List[dict]) -> str:
        """
        Deterministic, bounded serialization of the tail metadata for
        the P9c envelope.
        """
        try:
            items = []
            for turn in _normalize_turns(tail):
                items.append(
                    {
                        key: turn[key]
                        for key in sorted(turn)
                        if turn.get(key) is not None
                    }
                )
            text = json.dumps(
                items, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError):
            return "[]"

        bound = max(1, self.summary_max_chars)
        if len(text) > bound:
            keep = max(1, bound - len(_TAIL_TRUNCATION_SUFFIX))
            text = text[:keep] + _TAIL_TRUNCATION_SUFFIX
        return text

    def should_retry_compacted(self, error: Any) -> bool:
        """
        Pure overflow-retry decision: a context-overflow signal means
        "retry once with the compacted context"; anything else means
        "degrade to current-request-only".
        """
        if isinstance(error, ContextOverflowSignal):
            return True

        message = ""
        if isinstance(error, ProviderHTTPError):
            message = str(error.message or "")
        elif isinstance(error, Exception):
            message = str(error)

        lowered = (message or "").lower()
        return any(marker in lowered for marker in _CONTEXT_OVERFLOW_MARKERS)
