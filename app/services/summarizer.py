"""
P9b summarizer: deterministic extractive summaries from turn metadata,
an optional llm wrapper (single serial provider call, off by default),
and verify-then-persist orchestration.

Summaries are built from turn metadata only — never raw prompts or
responses — and are bounded by the existing continuity settings.
"""

import time
from dataclasses import replace
from typing import Any, Callable, List, Optional

from app.models.continuity import SUMMARY_VERSION, SummaryBlock, SummaryMethod
from app.services.memory_contract import FORBIDDEN_KEYS

_TRUNCATION_SUFFIX = "...(truncated)"


def _char_ratio(params: Optional[dict]) -> int:
    from app.core.config import settings

    if params and params.get("char_token_ratio") is not None:
        return max(1, int(params["char_token_ratio"]))
    return max(1, int(settings.continuity_chars_per_token))


def _summary_max_chars(params: Optional[dict]) -> int:
    from app.core.config import settings

    if params and params.get("summary_max_chars") is not None:
        return max(1, int(params["summary_max_chars"]))
    return max(1, int(settings.continuity_summary_max_chars))


def _sorted_turns(turns: Any) -> List[dict]:
    out: List[dict] = []
    if not isinstance(turns, (list, tuple)):
        return out
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


def _region_tokens_in(turns: List[dict]) -> Optional[int]:
    total = 0
    for turn in turns:
        try:
            total += int(turn.get("tokens_in") or 0)
        except (TypeError, ValueError):
            continue
    return total if total > 0 else None


def _build_block(
    turns: List[dict],
    content: str,
    method: str,
    model: Optional[str],
    params: Optional[dict],
    now: Optional[float],
) -> SummaryBlock:
    ratio = _char_ratio(params)
    conversation_id = ""
    up_to_seq = 0
    for turn in turns:
        conversation_id = turn.get("conversation_id") or conversation_id
        up_to_seq = max(up_to_seq, turn["seq"])
    return SummaryBlock(
        conversation_id=conversation_id,
        up_to_seq=up_to_seq,
        version=SUMMARY_VERSION,
        method=method,
        content=content,
        created_at=float(now) if now is not None else time.time(),
        tokens_in=_region_tokens_in(turns),
        tokens_out=max(1, len(content) // ratio),
        model=model,
    )


def _truncate(content: str, budget: Optional[int], params: Optional[dict]) -> str:
    limit = _summary_max_chars(params)
    if budget is not None and int(budget) > 0:
        char_budget = max(1, int(budget) * _char_ratio(params))
        limit = min(limit, char_budget)
    if len(content) > limit:
        keep = max(1, limit - len(_TRUNCATION_SUFFIX))
        content = content[:keep].rstrip() + _TRUNCATION_SUFFIX
    return content


def extractive_summarize(
    turns: List[dict],
    budget: Optional[int] = None,
    *,
    params: Optional[dict] = None,
    now: Optional[float] = None,
) -> SummaryBlock:
    """
    Deterministic structured summary of turn metadata
    (goal/context, decisions, outcomes, unresolved items), bounded by
    the summary char cap and the summary token budget.
    """
    normalized = _sorted_turns(turns)
    if not normalized:
        return SummaryBlock(
            conversation_id="",
            up_to_seq=0,
            version=SUMMARY_VERSION,
            method=SummaryMethod.EXTRACTIVE.value,
            content="",
            created_at=float(now) if now is not None else time.time(),
            tokens_in=None,
            tokens_out=0,
            model=None,
        )

    lines: List[str] = []
    tasks: List[str] = []
    for turn in normalized:
        task = str(turn.get("task") or "").strip()
        if task and task not in tasks:
            tasks.append(task)
    if tasks:
        lines.append("Goal/context: " + "; ".join(tasks))

    models: List[str] = []
    for turn in normalized:
        model = str(turn.get("model") or "").strip()
        if model and model not in models:
            models.append(model)
    if models:
        lines.append("Models used: " + ", ".join(models))

    outcomes: dict = {}
    for turn in normalized:
        outcome = str(turn.get("outcome") or "ok")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    if outcomes:
        lines.append(
            "Outcomes: "
            + ", ".join(f"{key}={value}" for key, value in sorted(outcomes.items()))
        )

    unresolved: List[str] = []
    for turn in normalized:
        if str(turn.get("outcome") or "") not in ("failed", "denied"):
            continue
        label = f"seq={turn['seq']}"
        if turn.get("model"):
            label += f" model={turn.get('model')}"
        unresolved.append(label)
    if unresolved:
        lines.append("Unresolved: " + "; ".join(unresolved))

    content = _truncate("\n".join(lines), budget, params)
    return _build_block(normalized, content, SummaryMethod.EXTRACTIVE.value, None, params, now)


def _redaction_suspicious(text: str) -> bool:
    """Heuristic: output echoing forbidden key names is treated as
    redaction-suspicious and rejected (fail-safe fallback)."""
    lowered = text.lower()
    return any(key in lowered or key.replace("_", " ") in lowered for key in FORBIDDEN_KEYS)


def _build_prompt(turns: List[dict]) -> str:
    lines: List[str] = []
    for turn in _sorted_turns(turns):
        lines.append(
            f"seq={turn.get('seq')} provider={turn.get('provider') or ''}"
            f" model={turn.get('model') or ''} outcome={turn.get('outcome') or ''}"
            f" task={turn.get('task') or ''}"
            f" tokens_in={turn.get('tokens_in') or 0}"
            f" tokens_out={turn.get('tokens_out') or 0}"
        )
    return "\n".join(lines)


def default_llm_invoke(model: str, prompt: str) -> str:
    """
    Single serial provider call for the requested model through the
    existing client-registry abstraction. Used only when
    ``CONTINUITY_SUMMARIZER_MODEL`` is set; never on the hot path.
    """
    from app.core.relay import relay
    from app.services.client_registry import ClientRegistry

    registry = ClientRegistry()
    for provider in relay.provider_manager.enabled():
        try:
            models = provider.models or []
        except Exception:  # noqa: BLE001 - best-effort model lookup
            continue
        if model in models:
            client = registry.get(provider.identity())
            return client.chat(provider, model, prompt)
    raise RuntimeError(f"no enabled provider serves model {model!r}")


def llm_summarize(
    turns: List[dict],
    budget: Optional[int] = None,
    *,
    model: Optional[str] = None,
    invoke: Optional[Callable[[str, str], str]] = None,
    params: Optional[dict] = None,
    now: Optional[float] = None,
) -> Optional[SummaryBlock]:
    """
    Optional llm summarizer. Never entered when the configured model is
    empty. On any failure (unavailable provider/model, timeout, error,
    redaction-suspicious output) falls back to extractive, with the
    fallback method recorded in the block provenance. Never raises.
    """
    from app.core.config import settings

    effective_model = (model or "").strip() or str(
        settings.continuity_summarizer_model or ""
    ).strip()
    if not effective_model:
        return None

    normalized = _sorted_turns(turns)
    if not normalized:
        return extractive_summarize(normalized, budget, params=params, now=now)

    invoke = invoke or default_llm_invoke
    try:
        raw = invoke(effective_model, _build_prompt(normalized))
        text = str(raw or "").strip()
        if not text:
            raise ValueError("empty llm summary")
        if _redaction_suspicious(text):
            raise ValueError("redaction-suspicious llm summary")
    except Exception:  # noqa: BLE001 - any failure degrades to extractive
        return extractive_summarize(normalized, budget, params=params, now=now)

    text = _truncate(text, budget, params)
    return _build_block(normalized, text, SummaryMethod.LLM.value, effective_model, params, now)


def summarize_and_persist(
    store,
    conversation_id: str,
    key_id: str,
    turns: List[dict],
    budget: Optional[int] = None,
    *,
    params: Optional[dict] = None,
    now: Optional[float] = None,
    llm_invoke: Optional[Callable[[str, str], str]] = None,
) -> Optional[SummaryBlock]:
    """
    Off-hot-path orchestration: compact (extractive, or the llm wrapper
    when the summarizer model is set), verify via
    ``summary_verifier.verify``, and persist only verified summaries
    through the existing ``ConversationStore`` methods.

    Returns the persisted block, or None when there is nothing to
    summarize or the summary fails verification (never a partial write).
    """
    from app.core.config import settings

    from app.services.context_manager import ContextManager, CompactionReason
    from app.services.summary_verifier import verify

    manager = ContextManager()
    merged = dict(manager.default_params())
    if isinstance(params, dict):
        merged.update(params)

    model = str(merged.get("summarizer_model") or "").strip() or str(
        settings.continuity_summarizer_model or ""
    ).strip()

    def _summarize(region_turns: List[dict], summary_budget: int):
        if model:
            block = llm_summarize(
                region_turns,
                summary_budget,
                model=model,
                invoke=llm_invoke,
                params=merged,
                now=now,
            )
            if block is not None:
                return block
        return extractive_summarize(
            region_turns, summary_budget, params=merged, now=now
        )

    result = manager.compact(
        turns,
        budget,
        merged,
        summarizer=_summarize,
        now=now,
        reason=CompactionReason.PREFLIGHT.value,
    )
    summary = result.summary
    if summary is None:
        return None

    conversation = store.get(conversation_id, key_id)
    if conversation is None:
        return None

    latest = None
    existing = store.summaries(conversation_id, key_id, limit=1)
    if existing:
        latest = existing[0].get("up_to_seq")

    if not verify(summary, conversation, turns, latest_up_to_seq=latest):
        return None

    recorded = store.record_summary(
        conversation_id=conversation_id,
        key_id=key_id,
        up_to_seq=summary.up_to_seq,
        version=summary.version,
        method=summary.method,
        content=summary.content,
        tokens_in=summary.tokens_in,
        tokens_out=summary.tokens_out,
    )
    store.record_compaction(
        conversation_id=conversation_id,
        key_id=key_id,
        reason=result.reason,
        method=result.method,
        from_tokens=result.from_tokens,
        to_tokens=result.to_tokens,
        summary_id=recorded.get("summary_id"),
    )
    return replace(summary, summary_id=recorded.get("summary_id"))
