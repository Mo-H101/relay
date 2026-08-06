"""
P9b summary verifier: pure structural checks plus the redaction hard
guard. Rejects summaries that are unknown, out-of-order, or that carry
forbidden keys — before any persistence.
"""

from typing import Any, List, Optional

from app.models.continuity import SUMMARY_VERSION, SummaryBlock
from app.services.memory_contract import contains_never_captured


def _norm_summary(summary: Any) -> Optional[dict]:
    if summary is None:
        return None
    if isinstance(summary, SummaryBlock):
        return summary.to_dict()
    if isinstance(summary, dict):
        return dict(summary)
    return None


def _turn_seq(turn: Any) -> Optional[int]:
    if not isinstance(turn, dict):
        return None
    try:
        return int(turn.get("seq") or -1)
    except (TypeError, ValueError):
        return None


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _nonneg_int(value: Any) -> bool:
    try:
        return int(value) >= 0
    except (TypeError, ValueError):
        return False


def verify(
    summary: Any,
    conversation: Any,
    turns: List[dict],
    *,
    latest_up_to_seq: Optional[int] = None,
) -> bool:
    """
    Pure structural verification of a summary against a conversation and
    its turns. Rejects summaries with forbidden keys, unknown versions,
    unknown or out-of-order ``up_to_seq``, or inconsistent token counts.
    """
    data = _norm_summary(summary)
    if data is None:
        return False

    if conversation is None or not isinstance(conversation, dict):
        return False

    if not isinstance(turns, list):
        return False

    if contains_never_captured(data):
        return False

    try:
        version = int(data.get("version") or -1)
    except (TypeError, ValueError):
        return False
    if version != SUMMARY_VERSION:
        return False

    if data.get("conversation_id") != conversation.get("id"):
        return False

    try:
        up_to_seq = int(data.get("up_to_seq") or -1)
    except (TypeError, ValueError):
        return False
    if up_to_seq < 1:
        return False

    valid_turns = [turn for turn in turns if _turn_seq(turn) is not None]
    seqs = sorted(_turn_seq(turn) for turn in valid_turns)
    if not seqs or up_to_seq not in seqs:
        return False

    if latest_up_to_seq is not None:
        try:
            if up_to_seq <= int(latest_up_to_seq):
                return False
        except (TypeError, ValueError):
            return False

    region = [turn for turn in valid_turns if _turn_seq(turn) <= up_to_seq]
    region_in = sum(_int_or_zero(turn.get("tokens_in")) for turn in region)

    tokens_in = data.get("tokens_in")
    if tokens_in is not None:
        if not _nonneg_int(tokens_in):
            return False
        if region_in > 0 and int(tokens_in) > region_in:
            return False

    tokens_out = data.get("tokens_out")
    if tokens_out is not None and not _nonneg_int(tokens_out):
        return False

    return True
