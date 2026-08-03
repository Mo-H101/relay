"""
Adaptive routing observability (Phase 7C).

AdaptiveWeights derives, from the telemetry store's EWMA estimates, the
per-candidate adaptive state that the scoring layer consumes: sample
confidence, EWMA reliability/latency, and how each candidate compares to
the pool. It is a pure observability/derivation layer: it never mutates
telemetry, health, or routing state, and it is provider-agnostic.

The scoring itself is done by CandidateScorer using the adaptive
signals; this service exists so diagnostics can expose the learned state
without duplicating the EWMA math. Everything here is metadata only.
"""

from dataclasses import dataclass
from typing import List, Optional

from app.core.config import settings
from app.services.telemetry import TelemetryStats

_MAX_LEARNING_RATE = 1.0


@dataclass(frozen=True)
class AdaptiveState:
    """
    Learned adaptive state for one (provider, model) pair.

    ``confidence`` ramps linearly from 0 toward 1 as the pair approaches
    adaptive_min_samples observations, so low-data pairs are reported as
    untrusted. Deltas are pool-relative: how much better/worse the pair
    is than the average of the confident pairs in the pool.
    """

    provider: str
    model: str
    request_count: int
    confidence: float
    ewma_success: Optional[float]
    ewma_latency_ms: Optional[float]
    latency_trend_ms: Optional[float]
    reliability_delta: float
    latency_delta: float


class AdaptiveWeights:
    """
    Derives per-candidate adaptive state from EWMA telemetry.

    Reads the adaptive tuning parameters from a settings-like object so
    tests can inject a fake config and so a reload that rebuilds the
    scorer/adaptive snapshot takes effect.
    """

    def __init__(self, config=None, telemetry=None) -> None:
        cfg = config or settings
        self.enabled = bool(getattr(cfg, "adaptive_routing_enabled", False))
        self.min_samples = int(getattr(cfg, "adaptive_min_samples", 10))
        self.learning_rate = min(
            _MAX_LEARNING_RATE,
            max(0.0, float(getattr(cfg, "adaptive_learning_rate", 0.1))),
        )
        self.latency_weight = float(
            getattr(cfg, "adaptive_latency_weight", 1.0)
        )
        self.reliability_weight = float(
            getattr(cfg, "adaptive_reliability_weight", 1.0)
        )
        self._telemetry = telemetry

    def state(self, stats: Optional[TelemetryStats]) -> Optional[AdaptiveState]:
        """
        Adaptive state for one (provider, model) pair, or None when the
        pair has no telemetry.
        """
        if stats is None:
            return None

        pool = self._pool_averages()
        ewma_success = stats.ewma_success
        ewma_latency = stats.ewma_latency_ms

        if not self.enabled:
            confidence = 0.0
        else:
            confidence = min(1.0, stats.request_count / self.min_samples)

        latency_trend = (
            round(ewma_latency - stats.average_latency_ms, 2)
            if ewma_latency is not None and stats.request_count
            else None
        )

        reliability_delta = self._reliability_delta(
            ewma_success, pool["ewma_success"]
        )
        latency_delta = self._latency_delta(
            ewma_latency, pool["ewma_latency_ms"]
        )

        return AdaptiveState(
            provider=stats.provider,
            model=stats.model,
            request_count=stats.request_count,
            confidence=round(confidence, 4),
            ewma_success=ewma_success,
            ewma_latency_ms=ewma_latency,
            latency_trend_ms=latency_trend,
            reliability_delta=reliability_delta,
            latency_delta=latency_delta,
        )

    def states(self) -> List[AdaptiveState]:
        """
        Adaptive state for every pair with telemetry, most-observed
        first.
        """
        if self._telemetry is None:
            return []

        states = [
            state
            for state in (self.state(stats) for stats in self._telemetry.all())
            if state is not None
        ]

        return sorted(
            states, key=lambda item: item.request_count, reverse=True
        )

    def config(self) -> dict:
        """
        Tuning parameters as a plain dict for diagnostics.
        """
        return {
            "enabled": self.enabled,
            "min_samples": self.min_samples,
            "learning_rate": self.learning_rate,
            "latency_weight": self.latency_weight,
            "reliability_weight": self.reliability_weight,
        }

    def _pool_averages(self) -> dict:
        """
        Average EWMA success/latency over the confident pairs in the
        pool. Used as the neutral reference for deltas.
        """
        if self._telemetry is None:
            return {"ewma_success": None, "ewma_latency_ms": None}

        successes = []
        latencies = []

        for stats in self._telemetry.all():
            if stats.request_count < self.min_samples:
                continue
            if stats.ewma_success is not None:
                successes.append(stats.ewma_success)
            if stats.ewma_latency_ms is not None:
                latencies.append(stats.ewma_latency_ms)

        return {
            "ewma_success": (
                sum(successes) / len(successes) if successes else None
            ),
            "ewma_latency_ms": (
                sum(latencies) / len(latencies) if latencies else None
            ),
        }

    @staticmethod
    def _reliability_delta(ewma_success, pool_success) -> float:
        """
        Signed difference between a pair's EWMA reliability and the pool
        average, clamped to [-1, 1]. Positive means more reliable.
        """
        if ewma_success is None or pool_success is None:
            return 0.0
        return round(
            min(1.0, max(-1.0, ewma_success - pool_success)), 4
        )

    @staticmethod
    def _latency_delta(ewma_latency_ms, pool_latency_ms) -> float:
        """
        Signed pool-relative latency difference normalized by the pool
        average, clamped to [-1, 1]. Positive means faster than the
        pool.
        """
        if (
            ewma_latency_ms is None
            or pool_latency_ms is None
            or pool_latency_ms <= 0.0
        ):
            return 0.0
        value = (pool_latency_ms - ewma_latency_ms) / pool_latency_ms
        return round(min(1.0, max(-1.0, value)), 4)


__all__ = ["AdaptiveState", "AdaptiveWeights"]
