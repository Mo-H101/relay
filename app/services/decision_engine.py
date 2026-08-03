"""
Explicit explainable decision engine (Phase 7E).

DecisionEngine is the unified decision layer on top of the candidate
pipeline. It consumes the same ordered Rankable candidates that the chat
hot path uses and packages each candidate into a DecisionScore with:

- per-signal contributions (raw input, normalized score, effective
  weight, weighted contribution),
- per-signal confidence,
- an overall confidence and a human-readable reason.

It never introduces its own ordering: the health-band invariant and the
per-signal feature gates are all enforced by CandidateScorer, and the
engine scores the candidates in exactly the order CandidateBuilder
produces. When DECISION_ENGINE_ENABLED is off the engine is inert: Relay
uses the existing candidate path unchanged.

The engine records lightweight, thread-safe decision statistics only
(decision count, candidate volume, selection tallies, band tallies). It
never stores prompts, responses, keys, or user identity.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.core.config import settings
from app.services.candidate_builder import CandidateBuilder
from app.services.scoring import (
    DEFAULT_BAND,
    CandidateScorer,
    Rankable,
)
from app.services.signals import SIGNAL_KEYS, label_for


@dataclass(frozen=True)
class SignalBreakdown:
    """
    Explicit per-signal contribution for one candidate.

    ``confidence`` is the engine's estimate of how trustworthy this
    signal's value is (0 = cold start/no data, 1 = confident). It is
    metadata for decision explainability; it never affects ordering.
    """

    key: str
    label: str
    raw: Optional[object]
    normalized: float
    weight: float
    contribution: float
    enabled: bool
    confidence: float


@dataclass(frozen=True)
class DecisionScore:
    """
    Explicit, explainable score for one candidate.

    ``health_band`` is the resolved band (DEFAULT_BAND when the raw value
    is missing), ``fitness`` the unrounded weighted sum, and ``total``
    the same sum rounded to the breakdown precision. ``ranked`` position
    is 1-based, matching the candidate pipeline order.
    """

    provider: str
    model: str
    rank: int
    health_band: int
    health_status: Optional[str]
    fitness: float
    total: float
    contributions: Dict[str, float]
    signals: List[SignalBreakdown]
    confidence: float
    reason: str


@dataclass(frozen=True)
class DecisionResult:
    """
    Outcome of one decision pass over the candidate pool.

    ``selected`` is the top candidate (None when the pool is empty);
    ``ranked`` is every candidate scored in pipeline order.
    """

    selected: Optional[DecisionScore]
    ranked: List[DecisionScore]
    generated_at: float


def _coerce_count(value) -> int:
    """
    Coerce a persisted decision count to a non-negative integer,
    tolerating malformed or missing values.
    """
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


class DecisionStats:
    """
    Thread-safe counters for decision activity. Metadata only; bounded
    by the pool size and the number of distinct (provider, model) pairs.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._decisions = 0
        self._candidates = 0
        self._selected: Dict[str, int] = {}
        self._bands: Dict[int, int] = {}

    def record(self, selected: Optional[DecisionScore], pool: int) -> None:
        with self._lock:
            self._decisions += 1
            self._candidates += pool
            if selected is not None:
                key = f"{selected.provider}/{selected.model}"
                self._selected[key] = self._selected.get(key, 0) + 1
                self._bands[selected.health_band] = (
                    self._bands.get(selected.health_band, 0) + 1
                )

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "decisions": self._decisions,
                "candidates": self._candidates,
                "selected": dict(
                    sorted(self._selected.items(), key=lambda item: -item[1])
                ),
                "by_band": dict(sorted(self._bands.items())),
            }

    def export_state(self) -> dict:
        """
        Export the counters for persistence (Phase 7F). Bounded numeric
        state only: totals plus per-pair selection tallies and per-band
        tallies keyed by (provider, model) identifiers.
        """
        with self._lock:
            return {
                "decisions": self._decisions,
                "candidates": self._candidates,
                "selected": dict(self._selected),
                "by_band": dict(self._bands),
            }

    def import_state(self, state: dict) -> None:
        """
        Restore the counters from an export (replacing any existing
        data). Values are coerced to non-negative integers; malformed or
        missing fields are treated as zero.
        """
        with self._lock:
            self._decisions = _coerce_count(state.get("decisions", 0))
            self._candidates = _coerce_count(state.get("candidates", 0))
            self._selected = {
                str(key): _coerce_count(value)
                for key, value in (state.get("selected") or {}).items()
            }
            self._bands = {
                int(key): _coerce_count(value)
                for key, value in (state.get("by_band") or {}).items()
            }


class DecisionEngine:
    """
    Scores the candidate pool into explicit DecisionScore objects.

    The engine reads scoring/adaptive parameters from a settings-like
    object so tests can inject a fake config and a hot reload can rebuild
    the scorer.
    """

    def __init__(
        self,
        builder: CandidateBuilder | None = None,
        scorer: CandidateScorer | None = None,
        config=None,
    ) -> None:
        self._settings = config or settings
        self._builder = builder
        self._scorer = scorer or CandidateScorer(config=self._settings)
        self._stats = DecisionStats()

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._settings, "decision_engine_enabled", False))

    def refresh(self) -> None:
        """
        Rebuild the scorer so scoring tuning changes from a hot
        configuration reload take effect. Statistics are retained.
        """
        self._scorer = CandidateScorer(config=self._settings)

    def stats(self) -> dict:
        return self._stats.snapshot()

    def import_state(self, stats: dict) -> None:
        """
        Restore persisted decision statistics into the engine.
        """
        self._stats.import_state(stats)

    def decide(
        self,
        providers,
        task: str | None = None,
    ) -> DecisionResult:
        """
        Score the ordered candidate pool and return the top candidate.
        Records the decision in the engine statistics.
        """
        return self._pass(providers, task, record=True)

    def score_pool(
        self,
        providers,
        task: str | None = None,
    ) -> DecisionResult:
        """
        Read-only scoring pass for diagnostics: identical output to
        ``decide`` but never mutates statistics, so exposing the engine
        in the diagnostics snapshot stays side-effect free.
        """
        return self._pass(providers, task, record=False)

    def _pass(
        self,
        providers,
        task: str | None,
        record: bool,
    ) -> DecisionResult:
        if self._builder is None:
            return DecisionResult(
                selected=None,
                ranked=[],
                generated_at=time.time(),
            )

        rankables = self._builder.rankables(providers, task=task)

        if not rankables:
            return DecisionResult(
                selected=None,
                ranked=[],
                generated_at=time.time(),
            )

        ranked = [
            self._score(rankable, rank)
            for rank, rankable in enumerate(rankables, start=1)
        ]
        selected = ranked[0]

        if record:
            self._stats.record(selected, len(ranked))

        return DecisionResult(
            selected=selected,
            ranked=ranked,
            generated_at=time.time(),
        )

    def _score(self, rankable: Rankable, rank: int) -> DecisionScore:
        details = self._scorer.signal_detail(rankable)

        signals = [
            SignalBreakdown(
                key=detail["key"],
                label=detail["label"],
                raw=detail["raw"],
                normalized=detail["normalized"],
                weight=detail["weight"],
                contribution=detail["contribution"],
                enabled=detail["enabled"],
                confidence=self._signal_confidence(detail["key"], rankable),
            )
            for detail in details
        ]

        contributions = {
            signal.key: signal.contribution for signal in signals
        }
        total = round(sum(contributions.values()), 4)

        band = rankable.health_band
        if band is None:
            band = DEFAULT_BAND

        confidence = self._overall_confidence(signals)

        return DecisionScore(
            provider=rankable.provider,
            model=rankable.model,
            rank=rank,
            health_band=band,
            health_status=rankable.health_status,
            fitness=self._scorer.score(rankable),
            total=total,
            contributions=contributions,
            signals=signals,
            confidence=confidence,
            reason=self._reason(rankable, band, signals, confidence),
        )

    def _overall_confidence(self, signals: List[SignalBreakdown]) -> float:
        enabled = [signal for signal in signals if signal.enabled]

        if not enabled:
            return 0.0

        return round(
            sum(signal.confidence for signal in enabled) / len(enabled),
            4,
        )

    def _signal_confidence(self, key: str, rankable: Rankable) -> float:
        if key == "priority":
            return 1.0

        if key in ("success", "latency", "failure"):
            return 1.0 if rankable.telemetry is not None else 0.0

        if key == "preference":
            return 1.0 if rankable.preference is not None else 0.0

        if key == "task_compatibility":
            return (
                1.0 if rankable.task_compatibility is not None else 0.0
            )

        if key in ("adaptive_reliability", "adaptive_latency"):
            if (
                rankable.telemetry is None
                or rankable.telemetry.request_count < self._scorer.adaptive_min_samples
            ):
                return 0.0
            return min(
                1.0,
                rankable.telemetry.request_count / self._scorer.adaptive_min_samples,
            )

        if key == "quality":
            confidence = rankable.quality_confidence
            if confidence is None:
                return 0.0
            return min(1.0, max(0.0, float(confidence)))

        if key == "cost":
            return 0.0

        return 0.0

    def _reason(
        self,
        rankable: Rankable,
        band: int,
        signals: List[SignalBreakdown],
        confidence: float,
    ) -> str:
        enabled = [signal for signal in signals if signal.enabled]
        signals_str = ", ".join(signal.key for signal in enabled) or "none"

        return (
            f"health_band={band} confidence={confidence:.2f} "
            f"signals={signals_str}"
        )


__all__ = [
    "DecisionEngine",
    "DecisionResult",
    "DecisionScore",
    "DecisionStats",
    "SignalBreakdown",
]
