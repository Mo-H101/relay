"""
Actual routing decision records (Phase 7 orchestration truth layer).

The DecisionEngine is a scoring/observability layer: its ``decide`` result
describes the pool at decision time and is not guaranteed to equal the
candidate a request actually executed (failover can move to a later
candidate, and the return value was previously discarded). This module
introduces the missing truth surface: an explicit, metadata-only record of
the decision an actual request made.

A ``DecisionRecord`` is created only *after* the request has executed, so
``selected_provider``/``selected_model`` always describe the candidate the
provider layer actually used. It carries the ordered candidate pool, the
per-attempt metadata, the classified task, the correlation id, and (when
the decision engine is enabled) the score-based reason/confidence/signals
for the executed candidate.

Privacy contract: the record is metadata only. It never stores prompts,
responses, generated content, API keys, credentials, raw user identity, or
conversation content, and it is held in a bounded in-memory store only
(never persisted). ``contains_never_captured()`` stays False over every
serialized surface.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class DecisionCandidate:
    """
    One candidate in the ordered pool at decision time.
    ``rank`` is 1-based pipeline order (the position the candidate would
    be tried first).
    """

    provider: str
    model: str
    rank: int


@dataclass(frozen=True)
class DecisionAttempt:
    """
    Metadata for one provider attempt during execution: which candidate
    was tried and how it ended. The failure reason string is deliberately
    omitted so error text can never leak content-shaped data.
    """

    provider: str
    model: str
    success: bool
    latency_ms: Optional[int] = None
    failure_type: Optional[str] = None


@dataclass(frozen=True)
class DecisionRecord:
    """
    The actual routing decision made for one completed request.

    ``outcome`` describes execution state: "succeeded", "failed", or
    "stream_started" (final stream outcome is attached in place when the
    stream finishes). ``selected_rank`` is the position of the executed
    candidate in the pool (1 = no failover). ``signals`` holds the
    executed candidate's per-signal contributions when the decision engine
    produced them, keyed by signal name.
    """

    correlation_id: str
    timestamp: float
    requested_model: Optional[str]
    classified_task: Optional[str]
    routed: bool
    selected_provider: str
    selected_model: str
    candidates: Tuple[DecisionCandidate, ...] = ()
    attempts: Tuple[DecisionAttempt, ...] = ()
    outcome: str = "succeeded"
    selected_rank: Optional[int] = None
    decision_reason: Optional[str] = None
    confidence: Optional[float] = None
    signals: Optional[Dict[str, float]] = None

    def to_dict(self) -> dict:
        """
        Serialize the record. Metadata only: no forbidden key appears at
        any nesting depth (asserted by the memory-contract tests).
        """
        return {
            "correlation_id": self.correlation_id,
            "timestamp": datetime.fromtimestamp(
                self.timestamp, tz=timezone.utc
            ).isoformat(),
            "requested_model": self.requested_model,
            "classified_task": self.classified_task,
            "routed": self.routed,
            "selected_provider": self.selected_provider,
            "selected_model": self.selected_model,
            "selected_rank": self.selected_rank,
            "candidates": [
                {
                    "provider": candidate.provider,
                    "model": candidate.model,
                    "rank": candidate.rank,
                }
                for candidate in self.candidates
            ],
            "attempts": [
                {
                    "provider": attempt.provider,
                    "model": attempt.model,
                    "success": attempt.success,
                    "latency_ms": attempt.latency_ms,
                    "failure_type": attempt.failure_type,
                }
                for attempt in self.attempts
            ],
            "outcome": self.outcome,
            "decision_reason": self.decision_reason,
            "confidence": self.confidence,
            "signals": dict(self.signals) if self.signals else None,
        }


class DecisionRecordStore:
    """
    Bounded, thread-safe in-memory store of the most recent actual
    decisions. Metadata only and never persisted: this is the Phase 7
    observability surface, not a new database schema. Old records are
    evicted beyond ``max_records``.
    """

    def __init__(self, max_records: int = 200) -> None:
        self._max_records = max(1, int(max_records))
        self._lock = threading.Lock()
        self._records: Deque[DecisionRecord] = deque(maxlen=self._max_records)

    @property
    def max_records(self) -> int:
        return self._max_records

    def record(self, record: DecisionRecord) -> None:
        """
        Append an actual decision. The most recent record is evicted once
        the bound is exceeded.
        """
        with self._lock:
            self._records.append(record)

    def update(self, correlation_id: str, **fields: Any) -> bool:
        """
        Attach late-known execution metadata (e.g. a stream's final
        outcome) to the matching record, replacing it in place. Returns
        False when no record matches the correlation id.
        """
        with self._lock:
            for index, record in enumerate(self._records):
                if record.correlation_id == correlation_id:
                    self._records[index] = replace(record, **fields)
                    return True
        return False

    def most_recent(self) -> Optional[DecisionRecord]:
        """
        The newest recorded decision, or None when none exists yet.
        """
        with self._lock:
            return self._records[-1] if self._records else None

    def get(self, correlation_id: str) -> Optional[DecisionRecord]:
        """
        The decision for a specific correlation id, or None.
        """
        with self._lock:
            for record in self._records:
                if record.correlation_id == correlation_id:
                    return record
        return None

    def snapshot(self, limit: Optional[int] = None) -> List[dict]:
        """
        Serialized decisions, oldest to newest (most recent last). An
        optional ``limit`` keeps only the trailing records.
        """
        with self._lock:
            records = list(self._records)

        if limit is not None:
            records = records[-int(limit):]

        return [record.to_dict() for record in records]


def build_attempts(attempts: Optional[List[dict]]) -> Tuple[DecisionAttempt, ...]:
    """
    Convert raw per-attempt metadata from the chat service result into a
    bounded tuple of DecisionAttempt records. Only provider/model/outcome
    fields are kept; attempt reason strings are dropped.
    """
    if not attempts:
        return ()

    collected: List[DecisionAttempt] = []

    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue

        collected.append(
            DecisionAttempt(
                provider=str(attempt.get("provider") or ""),
                model=str(attempt.get("model") or ""),
                success=bool(attempt.get("success")),
                latency_ms=attempt.get("latency_ms"),
                failure_type=attempt.get("failure_type"),
            )
        )

    return tuple(collected)


def build_candidates(candidates) -> Tuple[DecisionCandidate, ...]:
    """
    Convert the ordered (provider, model) candidate list into ranked
    candidate metadata. Ranks are 1-based pipeline order.
    """
    return tuple(
        DecisionCandidate(
            provider=provider.name,
            model=model,
            rank=rank,
        )
        for rank, (provider, model) in enumerate(candidates, start=1)
    )


def selected_rank(
    provider: str, model: str, candidates: Tuple[DecisionCandidate, ...]
) -> Optional[int]:
    """
    The pool position of the executed candidate (1 = top candidate, no
    failover), or None when it is not part of the recorded pool.
    """
    for candidate in candidates:
        if candidate.provider == provider and candidate.model == model:
            return candidate.rank
    return None


def decision_score_for(
    decision_result,
    provider: str,
    model: str,
):
    """
    The DecisionScore matching the executed (provider, model), or None.
    Used to attach the engine's reason/confidence/signals for the
    candidate that actually ran (never the predicted top candidate).
    """
    if decision_result is None:
        return None

    for score in decision_result.ranked:
        if score.provider == provider and score.model == model:
            return score

    return None


def record_actual_decision(
    store,
    *,
    correlation_id: str,
    requested_model: Optional[str],
    routed_task: Optional[str],
    routed: bool,
    candidates,
    provider: str,
    model: str,
    attempts,
    outcome: str,
    decision_result=None,
) -> None:
    """
    Record the decision a completed request actually made: the
    (provider, model) that executed, the ordered candidate pool, and
    (when the decision engine produced scores) its reason/confidence/
    signals for the *executed* candidate. Metadata only; the correlation
    id ties the record to the request.

    This is the single shared implementation for every request surface
    (/v1 passthrough and routed paths, and the legacy /chat path), so the
    actual-decision truth surface can never drift between endpoints.
    """
    ranked = build_candidates(candidates)
    attempts_meta = build_attempts(attempts)
    rank = selected_rank(provider, model, ranked)
    score = decision_score_for(decision_result, provider, model)

    if score is not None:
        reason = score.reason
        confidence = score.confidence
        signals = dict(score.contributions)
    else:
        reason = None
        confidence = None
        signals = None

    if reason is None:
        if not routed:
            reason = "explicit upstream model passthrough"
        elif rank == 1:
            reason = "routed to top-ranked candidate"
        else:
            reason = f"routed; executed candidate rank {rank}"

    record = DecisionRecord(
        correlation_id=correlation_id,
        timestamp=time.time(),
        requested_model=requested_model,
        classified_task=routed_task,
        routed=routed,
        selected_provider=provider,
        selected_model=model,
        candidates=ranked,
        attempts=attempts_meta,
        outcome=outcome,
        selected_rank=rank,
        decision_reason=reason,
        confidence=confidence,
        signals=signals,
    )
    store.record(record)


__all__ = [
    "DecisionAttempt",
    "DecisionCandidate",
    "DecisionRecord",
    "DecisionRecordStore",
    "build_attempts",
    "build_candidates",
    "decision_score_for",
    "record_actual_decision",
    "selected_rank",
]
