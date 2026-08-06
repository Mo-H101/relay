"""
P9b summary verifier: pure structural checks plus the redaction hard
guard. P9e adds the instruction-shape guard: a summary whose content
reads like a system prompt or command set is rejected as untrusted data
before any persistence. Rejects summaries that are unknown,
out-of-order, instruction-shaped, or that carry forbidden keys.
"""

import re
from typing import Any, List, Optional

from app.models.continuity import SUMMARY_VERSION, SummaryBlock
from app.services.memory_contract import contains_never_captured

# P9e instruction-shape detection (deterministic, local, no external
# model dependency -- Option C). A summary is derived data; if its text
# reads like instructions or commands, it must never be persisted or
# counted as trusted state. Leading patterns fire on the first line of
# the summary; embedded patterns fire anywhere.
_INSTRUCTION_LEAD = (
    re.compile(
        r"^\s*(?:you\s+(?:are|'re|have\s+been)|your\s+(?:task|mission))\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*system\s*[:!.)]", re.IGNORECASE),
    re.compile(
        r"^\s*(?:instructions?|rules?|guidelines?|directives?)\s*[:.)]",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*(?:important|attention|warning|note)\s*[:!)]", re.IGNORECASE),
    re.compile(r"^\s*(?:remember|from\s+now\s+on|hereafter|henceforth)\b", re.IGNORECASE),
    re.compile(
        r"^\s*(?:ignore|forget|disregard|never|always|do\s+not|"
        r"you\s+must|you\s+should|you\s+will|you\s+are\s+to)\b",
        re.IGNORECASE,
    ),
)

_INSTRUCTION_EMBEDDED = (
    re.compile(
        r"ignore\s+(?:all\s+)?(?:previous|prior)\s+(?:instructions?|rules?|context)",
        re.IGNORECASE,
    ),
    re.compile(
        r"forget\s+(?:all\s+)?(?:previous|prior|your)\s+(?:instructions?|rules?|context)",
        re.IGNORECASE,
    ),
    re.compile(
        r"disregard\s+(?:all\s+)?(?:previous|prior|the\s+above)\b",
        re.IGNORECASE,
    ),
    re.compile(r"system\s+prompt", re.IGNORECASE),
    re.compile(
        r"(?:reveal|print|show|output|display|dump)\s+(?:out\s+)?"
        r"(?:your|the|your\s+own)\s+(?:system\s+)?(?:prompt|instructions?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"override\s+(?:(?:all|previous|prior|your|old|the|new)\s+)*"
        r"(?:system\s+)?(?:instructions?|prompt|rules?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:instructions?|rules?|prompt)\s+replace\s+(?:the\s+)?"
        r"(?:old|previous|prior)\b",
        re.IGNORECASE,
    ),
    re.compile(r"as\s+your\s+(?:system\s+)?(?:prompt|instructions?)", re.IGNORECASE),
    re.compile(r"jailbreak", re.IGNORECASE),
)


def is_instruction_shaped(content: Any) -> bool:
    """
    True when ``content`` reads like instructions or commands rather than
    a report of prior work. Deterministic local heuristic with no external
    model dependency. Over-rejection is safe: an instruction-shaped
    summary is never persisted and the context simply degrades to the
    metadata tail.
    """
    text = str(content or "")
    if not text.strip():
        return False

    lowered = text.lower()

    if any(pattern.search(lowered) for pattern in _INSTRUCTION_EMBEDDED):
        return True

    first_line = lowered.splitlines()[0]
    return any(pattern.match(first_line) for pattern in _INSTRUCTION_LEAD)


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
    max_summary_tokens: Optional[int] = None,
) -> bool:
    """
    Pure structural verification of a summary against a conversation and
    its turns. Rejects summaries with forbidden keys, instruction-shaped
    content, unknown versions, unknown or out-of-order ``up_to_seq``, or
    inconsistent token counts. When ``max_summary_tokens`` is given, an
    over-budget summary (``tokens_out`` above the configured summary
    budget, R-3) is also rejected.
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

    if is_instruction_shaped(data.get("summary_text")):
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

    if max_summary_tokens is not None:
        try:
            if int(tokens_out or 0) > int(max_summary_tokens):
                return False
        except (TypeError, ValueError):
            return False

    return True
