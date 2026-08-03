from dataclasses import dataclass
from typing import Dict, List, Optional

from app.core.config import settings
from app.services.signals import (
    SIGNAL_KEYS,
    enabled_attr_for,
    label_for,
    neutral_for,
    weight_attr_for,
)
from app.services.telemetry import TelemetryStats

# Health band ordering, mirroring the CandidateBuilder band map:
# lower is healthier. The band is the primary ordering key and is never
# overridden by fitness.
BAND_HEALTHY = 0
BAND_DEGRADED = 1
BAND_NOT_CHECKED = 2
BAND_UNAVAILABLE = 3
DEFAULT_BAND = BAND_NOT_CHECKED

PRIORITY_WEIGHT = 1.0
SUCCESS_WEIGHT = 1.0
LATENCY_WEIGHT = 1.0
FAILURE_WEIGHT = 1.0
PREFERENCE_WEIGHT = 1.0
TASK_COMPATIBILITY_WEIGHT = 1.0
ADAPTIVE_RELIABILITY_WEIGHT = 1.0
ADAPTIVE_LATENCY_WEIGHT = 1.0
QUALITY_WEIGHT = 1.0
# Future cost placeholder (Phase 7E): no cost data exists yet, so the
# signal resolves to a neutral constant and contributes zero by default.
# Raising the weight adds a constant to every candidate equally and
# therefore cannot reorder anything until real cost data is wired in.
COST_WEIGHT = 0.0
_ADAPTIVE_DEFAULT_MIN_SAMPLES = 10

_PRIORITY_DENOM = 10
_LATENCY_REF_MS = 250
_FAILURE_REF_COUNT = 5


def _as_float(config, name: str, default) -> float:
    """
    Pull a tuning parameter off a settings-like object, falling back to
    the module default when the object has no such attribute (e.g. the
    test fake settings). Invalid values are rejected by Settings at parse
    time; here we only coerce to float.
    """
    return float(getattr(config, name, default))


# Dispatch map from signal key to the scorer method that normalizes the
# signal's raw input to [0, 1].
_SCORE_METHODS = {
    "priority": "priority_score",
    "success": "success_score",
    "latency": "latency_score",
    "failure": "failure_score",
    "preference": "preference_score",
    "task_compatibility": "task_compatibility_score",
    "adaptive_reliability": "adaptive_reliability_score",
    "adaptive_latency": "adaptive_latency_score",
    "quality": "quality_score",
    "cost": "cost_score",
}

@dataclass(frozen=True)
class Rankable:
    """
    A candidate to be ranked, bundled with the signals the scorer needs.
    """

    provider: str
    model: str
    priority: int
    health_band: Optional[int] = None
    health_status: Optional[str] = None
    telemetry: Optional[TelemetryStats] = None
    preference: Optional[int] = None
    task_compatibility: Optional[float] = None
    quality_ewma: Optional[float] = None
    quality_confidence: Optional[float] = None


class CandidateScorer:
    """
    Pure scoring layer for (provider, model) candidates.

    The health band is the primary ordering key; fitness only decides the
    order within the same band. This structurally guarantees that a
    degraded/unhealthy provider can never outrank a healthier one because
    of priority or telemetry alone.

    Signals are normalized to [0, 1] and combined with weights declared
    in the signal registry (app/services/signals.py):
    - priority (higher is better)
    - success rate
    - average latency (lower is better)
    - recent failure history (fewer is better)
    - task-reference preference (earlier is better)
    - task compatibility (catalog match; gated by task_catalog_enabled)
    - adaptive reliability (EWMA success rate; gated by
      adaptive_routing_enabled and min-sample confidence)
    - adaptive latency (EWMA latency; gated by adaptive_routing_enabled
      and min-sample confidence)
    - quality (EWMA user quality rating; gated by
      quality_feedback_enabled and min-sample confidence)
    - cost (future placeholder; always neutral, contributes zero by
      default until cost data and a nonzero weight are configured)

    Weights are a static baseline (from settings) plus optional adaptive
    deltas. Adaptive deltas default to zero, so the effective weights
    equal the baseline and fitness is byte-identical to the legacy
    formula.

    Cold start: candidates with no telemetry resolve every telemetry
    signal to a neutral constant, so ordering reduces to priority and
    task-reference preference order.

    Adaptive signals (Phase 7C): EWMA reliability/latency learn from
    recent outcomes with a capped learning rate. They only contribute
    once a candidate has at least adaptive_min_samples observations;
    below that they resolve to a neutral constant for every candidate,
    so cold-start data never steers ordering. The health band remains
    the primary ordering key, so adaptive signals only reorder within a
    band.

    Quality feedback (Phase 7D): EWMA quality ratings from the
    metadata-only QualityStore. They only contribute once a pair has at
    least quality_feedback_min_samples ratings; below that they resolve
    to a neutral constant, so sparse or noisy feedback never steers
    ordering. Quality is gated by quality_feedback_enabled and only ever
    reorders within an existing health band; it never overrides health
    safety or operational reliability.
    """

    def __init__(self, config=None, adaptive_weights: Optional[Dict[str, float]] = None) -> None:
        cfg = config or settings

        self.priority_weight = _as_float(cfg, "scoring_priority_weight", PRIORITY_WEIGHT)
        self.success_weight = _as_float(cfg, "scoring_success_weight", SUCCESS_WEIGHT)
        self.latency_weight = _as_float(cfg, "scoring_latency_weight", LATENCY_WEIGHT)
        self.failure_weight = _as_float(cfg, "scoring_failure_weight", FAILURE_WEIGHT)
        self.preference_weight = _as_float(
            cfg, "scoring_preference_weight", PREFERENCE_WEIGHT
        )
        self.task_compatibility_weight = _as_float(
            cfg,
            "scoring_task_compatibility_weight",
            TASK_COMPATIBILITY_WEIGHT,
        )
        self.adaptive_reliability_weight = _as_float(
            cfg,
            "adaptive_reliability_weight",
            ADAPTIVE_RELIABILITY_WEIGHT,
        )
        self.adaptive_latency_weight = _as_float(
            cfg,
            "adaptive_latency_weight",
            ADAPTIVE_LATENCY_WEIGHT,
        )
        self.quality_weight = _as_float(
            cfg,
            "quality_feedback_weight",
            QUALITY_WEIGHT,
        )
        self.cost_weight = _as_float(
            cfg,
            "scoring_cost_weight",
            COST_WEIGHT,
        )
        self.priority_denom = _as_float(cfg, "scoring_priority_denom", _PRIORITY_DENOM)
        self.latency_ref_ms = _as_float(cfg, "scoring_latency_ref_ms", _LATENCY_REF_MS)
        self.failure_ref_count = int(
            _as_float(cfg, "scoring_failure_ref_count", _FAILURE_REF_COUNT)
        )

        # Feature gates. When a gated signal is disabled its effective
        # weight is zero regardless of baseline or deltas.
        self.task_catalog_enabled = bool(getattr(cfg, "task_catalog_enabled", False))
        self.adaptive_routing_enabled = bool(
            getattr(cfg, "adaptive_routing_enabled", False)
        )
        self.quality_feedback_enabled = bool(
            getattr(cfg, "quality_feedback_enabled", False)
        )
        # Minimum observations before EWMA state is trusted for the
        # adaptive signals. Below this they resolve to neutral for every
        # candidate.
        self.adaptive_min_samples = int(
            getattr(cfg, "adaptive_min_samples", _ADAPTIVE_DEFAULT_MIN_SAMPLES)
        )

        self._adaptive_weights = dict(adaptive_weights or {})

    def priority_score(self, priority: int) -> float:
        """
        Monotonic normalization of provider priority to [0, 1).
        """
        return priority / (priority + self.priority_denom)

    def success_score(self, success_rate: Optional[float]) -> float:
        """
        Success rate clamped to [0, 1]; neutral when unknown.
        """
        if success_rate is None:
            return neutral_for("success")
        return min(1.0, max(0.0, float(success_rate)))

    def latency_score(self, average_latency_ms: Optional[float]) -> float:
        """
        Inverse latency normalized to (0, 1]; neutral when unknown.
        """
        if average_latency_ms is None:
            return neutral_for("latency")
        latency = max(0.0, float(average_latency_ms))
        return 1.0 / (1.0 + latency / self.latency_ref_ms)

    def failure_score(self, failure_count: int) -> float:
        """
        Recent failure history mapped to [0, 1]; no failures scores 1.0.
        """
        penalty = min(
            1.0,
            max(0.0, int(failure_count)) / self.failure_ref_count,
        )
        return 1.0 - penalty

    def preference_score(self, index: Optional[int]) -> float:
        """
        Task-reference preference mapped to (0, 1]: the earlier a model
        reference appears in a task category, the higher its score.
        Neutral when the candidate is not tied to a task reference.
        """
        if index is None:
            return neutral_for("preference")
        return 1.0 / (1.0 + max(0, int(index)))

    def task_compatibility_score(self, task_compatibility: Optional[float]) -> float:
        """
        Catalog compatibility clamped to [0, 1]; neutral when unknown.
        """
        if task_compatibility is None:
            return neutral_for("task_compatibility")
        return min(1.0, max(0.0, float(task_compatibility)))

    def adaptive_reliability_score(self, ewma_success: Optional[float]) -> float:
        """
        EWMA success rate clamped to [0, 1]; neutral when not confident
        (below adaptive_min_samples observations) or unknown.
        """
        if ewma_success is None:
            return neutral_for("adaptive_reliability")
        return min(1.0, max(0.0, float(ewma_success)))

    def adaptive_latency_score(self, ewma_latency_ms: Optional[float]) -> float:
        """
        Inverse EWMA latency normalized to (0, 1]; neutral when not
        confident (below adaptive_min_samples observations) or unknown.
        """
        if ewma_latency_ms is None:
            return neutral_for("adaptive_latency")
        latency = max(0.0, float(ewma_latency_ms))
        return 1.0 / (1.0 + latency / self.latency_ref_ms)

    def quality_score(self, quality_ewma: Optional[float]) -> float:
        """
        EWMA quality rating clamped to [0, 1]; neutral when not confident
        (below quality_feedback_min_samples ratings) or unknown.
        """
        if quality_ewma is None:
            return neutral_for("quality")
        return min(1.0, max(0.0, float(quality_ewma)))

    def cost_score(self, cost) -> float:
        """
        Future cost placeholder: no cost data exists, so the signal always
        resolves to its neutral constant. Contribution is zero by default
        (COST_WEIGHT), so the placeholder can never reorder anything until
        real cost data and a nonzero weight are configured.
        """
        return neutral_for("cost")

    def _raw_input(self, key: str, rankable: Rankable):
        if key == "priority":
            return rankable.priority
        if key == "success":
            return self._success_rate(rankable.telemetry)
        if key == "latency":
            return self._average_latency(rankable.telemetry)
        if key == "failure":
            return self._failure_history_count(rankable.telemetry)
        if key == "preference":
            return rankable.preference
        if key == "task_compatibility":
            return rankable.task_compatibility
        if key == "adaptive_reliability":
            return self._ewma_success(rankable.telemetry)
        if key == "quality":
            return rankable.quality_ewma
        if key == "cost":
            return None
        return self._ewma_latency(rankable.telemetry)

    def _signal_score(self, key: str, rankable: Rankable) -> float:
        method = getattr(self, _SCORE_METHODS[key])
        return method(self._raw_input(key, rankable))

    def _effective_weight(
        self,
        key: str,
        adaptive_weights: Optional[Dict[str, float]] = None,
    ) -> float:
        """
        Baseline weight plus the adaptive delta, or zero when the signal
        is disabled by its feature gate.
        """
        enabled_attr = enabled_attr_for(key)

        if enabled_attr is not None and not bool(
            getattr(self, enabled_attr, False)
        ):
            return 0.0

        deltas = (
            adaptive_weights
            if adaptive_weights is not None
            else self._adaptive_weights
        )

        return getattr(self, weight_attr_for(key)) + deltas.get(key, 0.0)

    def fitness(
        self,
        priority: int,
        telemetry: Optional[TelemetryStats] = None,
        preference: Optional[int] = None,
        task_compatibility: Optional[float] = None,
        quality_ewma: Optional[float] = None,
        adaptive_weights: Optional[Dict[str, float]] = None,
    ) -> float:
        """
        Weighted combination of the normalized signals.
        """
        proxy = Rankable(
            provider="",
            model="",
            priority=priority,
            telemetry=telemetry,
            preference=preference,
            task_compatibility=task_compatibility,
            quality_ewma=quality_ewma,
        )

        return sum(
            self._effective_weight(key, adaptive_weights)
            * self._signal_score(key, proxy)
            for key in SIGNAL_KEYS
        )

    def score(
        self,
        rankable: Rankable,
        adaptive_weights: Optional[Dict[str, float]] = None,
    ) -> float:
        """
        Fitness for a single candidate.
        """
        return self.fitness(
            rankable.priority,
            rankable.telemetry,
            rankable.preference,
            rankable.task_compatibility,
            rankable.quality_ewma,
            adaptive_weights=adaptive_weights,
        )

    def breakdown(
        self,
        rankable: Rankable,
        adaptive_weights: Optional[Dict[str, float]] = None,
    ) -> dict:
        """
        Per-signal score breakdown for a candidate.

        A breakdown entry is emitted for every registered signal
        (disabled signals emit zero); 'health_band' is the raw band key
        and 'total' the sum of the weighted contributions.
        """
        band = rankable.health_band
        if band is None:
            band = DEFAULT_BAND

        contributions = {}

        for key in SIGNAL_KEYS:
            contributions[key] = round(
                self._effective_weight(key, adaptive_weights)
                * self._signal_score(key, rankable),
                4,
            )

        total = round(sum(contributions.values()), 4)

        result = {"health_band": band}
        result.update(contributions)
        result["total"] = total

        return result

    def signal_detail(
        self,
        rankable: Rankable,
        adaptive_weights: Optional[Dict[str, float]] = None,
    ) -> List[dict]:
        """
        Per-signal observability detail for a candidate: raw input,
        normalized score, effective weight, weighted contribution, and
        whether the signal is enabled (effective weight > 0).

        Emits an entry for every registered signal. This is the explicit
        surface the DecisionEngine packages into SignalBreakdown objects;
        it adds no hidden ordering logic.
        """
        details = []

        for key in SIGNAL_KEYS:
            weight = self._effective_weight(key, adaptive_weights)
            raw = self._raw_input(key, rankable)
            normalized = self._signal_score(key, rankable)

            details.append(
                {
                    "key": key,
                    "label": label_for(key),
                    "raw": raw,
                    "normalized": normalized,
                    "weight": weight,
                    "contribution": round(weight * normalized, 4),
                    "enabled": weight > 0,
                }
            )

        return details

    def effective_weights(
        self,
        adaptive_weights: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """
        Effective weight for every registered signal (baseline plus
        adaptive delta, or zero when a feature gate is off).
        """
        return {
            key: self._effective_weight(key, adaptive_weights)
            for key in SIGNAL_KEYS
        }

    def key(self, rankable: Rankable):
        """
        Sort key: health band first, then fitness (higher fitness first).
        """
        band = rankable.health_band
        if band is None:
            band = DEFAULT_BAND
        return (band, -self.score(rankable))

    def rank(self, candidates: List[Rankable]) -> List[Rankable]:
        """
        Order candidates by (health band, fitness). Stable, so candidates
        with equal keys keep their input order.
        """
        return sorted(candidates, key=self.key)

    def _success_rate(self, telemetry: Optional[TelemetryStats]) -> Optional[float]:
        if telemetry is None or telemetry.request_count == 0:
            return None
        return telemetry.success_count / telemetry.request_count

    def _average_latency(self, telemetry: Optional[TelemetryStats]) -> Optional[float]:
        if telemetry is None:
            return None
        return telemetry.average_latency_ms

    def _failure_history_count(self, telemetry: Optional[TelemetryStats]) -> int:
        if telemetry is None:
            return 0
        return len(telemetry.recent_failures)

    def _ewma_success(self, telemetry: Optional[TelemetryStats]) -> Optional[float]:
        """
        EWMA success rate, or None until the candidate has enough
        observations to be trusted (adaptive_min_samples). Neutral in
        both cases, so cold-start data never steers ordering.
        """
        if (
            telemetry is None
            or telemetry.request_count < self.adaptive_min_samples
            or telemetry.ewma_success is None
        ):
            return None
        return telemetry.ewma_success

    def _ewma_latency(self, telemetry: Optional[TelemetryStats]) -> Optional[float]:
        """
        EWMA latency, or None until the candidate has enough observations
        to be trusted (adaptive_min_samples).
        """
        if (
            telemetry is None
            or telemetry.request_count < self.adaptive_min_samples
            or telemetry.ewma_latency_ms is None
        ):
            return None
        return telemetry.ewma_latency_ms


__all__ = [
    "BAND_DEGRADED",
    "BAND_HEALTHY",
    "BAND_NOT_CHECKED",
    "BAND_UNAVAILABLE",
    "DEFAULT_BAND",
    "CandidateScorer",
    "Rankable",
]
