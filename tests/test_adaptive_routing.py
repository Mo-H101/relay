"""
Phase 7C tests: adaptive routing with EWMA reliability/latency learning.

Covers the EWMA telemetry extensions, the adaptive signals on the
scorer, the AdaptiveWeights observability service, and the guarantees
that matter: adaptive routing is off by default (ordering byte-identical
to the legacy formula), min-sample confidence gates learning, the health
band always stays the primary ordering key, and adaptive state is
metadata-only (no payloads persisted).
"""

import pytest

from app.providers.base import Provider
from app.services.adaptive import AdaptiveWeights
from app.services.candidate_builder import CandidateBuilder
from app.services.health_store import HealthStore
from app.services.scoring import (
    BAND_HEALTHY,
    BAND_DEGRADED,
    CandidateScorer,
    Rankable,
)
from app.services.telemetry import TelemetryStats, TelemetryStore


class _AdaptiveConfig:
    def __init__(self, **kwargs):
        self.health_aware_routing = True
        self.task_catalog_enabled = False
        self.adaptive_routing_enabled = False
        self.adaptive_min_samples = 10
        self.adaptive_learning_rate = 0.1
        self.adaptive_latency_weight = 1.0
        self.adaptive_reliability_weight = 1.0

        for key, value in kwargs.items():
            setattr(self, key, value)


def make_stats(
    request_count,
    success_count,
    ewma_success,
    ewma_latency_ms,
    average_latency_ms=100.0,
    failure_count=None,
):
    if failure_count is None:
        failure_count = request_count - success_count
    return TelemetryStats(
        provider="P",
        model="m",
        request_count=request_count,
        success_count=success_count,
        failure_count=failure_count,
        average_latency_ms=average_latency_ms,
        recent_failures=[],
        ewma_success=ewma_success,
        ewma_latency_ms=ewma_latency_ms,
    )


def make_provider(name, models, priority=1, api_key="test-key"):
    return Provider(
        name=name,
        base_url=f"https://{name.lower()}.invalid",
        api_key=api_key,
        enabled=True,
        priority=priority,
        models=list(models),
    )


class TestEwmaTelemetry:
    def test_first_attempt_seeds_estimates(self):
        store = TelemetryStore(ewma_alpha=0.1)
        store.record_attempt("P", "m", True, latency_ms=100)

        stats = store.get("P", "m")
        assert stats.ewma_success == 1.0
        assert stats.ewma_latency_ms == 100.0

    def test_reliability_ewma_moves_by_alpha(self):
        store = TelemetryStore(ewma_alpha=0.1)
        store.record_attempt("P", "m", True, latency_ms=100)
        store.record_attempt("P", "m", True, latency_ms=100)
        store.record_attempt("P", "m", False, latency_ms=100)

        stats = store.get("P", "m")
        assert stats.ewma_success == pytest.approx(1.0 + 0.1 * (0.0 - 1.0))

    def test_latency_ewma_moves_by_alpha(self):
        store = TelemetryStore(ewma_alpha=0.1)
        store.record_attempt("P", "m", True, latency_ms=100)
        store.record_attempt("P", "m", True, latency_ms=300)
        store.record_attempt("P", "m", True, latency_ms=200)

        stats = store.get("P", "m")
        expected = 100 + 0.1 * (300 - 100)
        expected = expected + 0.1 * (200 - expected)
        assert stats.ewma_latency_ms == pytest.approx(expected)

    def test_set_ewma_alpha_changes_future_updates(self):
        store = TelemetryStore(ewma_alpha=0.1)
        store.record_attempt("P", "m", True, latency_ms=100)
        store.set_ewma_alpha(0.5)
        store.record_attempt("P", "m", False, latency_ms=100)

        stats = store.get("P", "m")
        assert stats.ewma_success == pytest.approx(1.0 + 0.5 * (0.0 - 1.0))

    def test_ewma_alpha_is_capped(self):
        store = TelemetryStore(ewma_alpha=5.0)
        assert store._ewma_alpha == 1.0

        store.set_ewma_alpha(-1.0)
        assert store._ewma_alpha == 0.0

    def test_ewma_success_stays_in_unit_range(self):
        store = TelemetryStore(ewma_alpha=0.9)
        for i in range(20):
            store.record_attempt(
                "P", "m", success=(i % 2 == 0), latency_ms=10
            )

        stats = store.get("P", "m")
        assert 0.0 <= stats.ewma_success <= 1.0

    def test_ewma_round_trips_through_export_import(self):
        source = TelemetryStore(ewma_alpha=0.3)
        source.record_attempt("P", "m", True, latency_ms=100)
        source.record_attempt("P", "m", False, latency_ms=400)

        restored = TelemetryStore(ewma_alpha=0.3)
        restored.import_state(source.export_state())

        source_stats = source.get("P", "m")
        restored_stats = restored.get("P", "m")
        assert restored_stats.ewma_success == source_stats.ewma_success
        assert (
            restored_stats.ewma_latency_ms == source_stats.ewma_latency_ms
        )

    def test_legacy_export_without_ewma_imports_cleanly(self):
        restored = TelemetryStore()
        restored.import_state(
            [
                {
                    "provider": "P",
                    "model": "m",
                    "request_count": 5,
                    "success_count": 5,
                    "failure_count": 0,
                    "total_latency_ms": 500,
                    "recent_failures": [],
                }
            ]
        )

        stats = restored.get("P", "m")
        assert stats.ewma_success is None
        assert stats.ewma_latency_ms is None


class TestScorerAdaptiveSignals:
    def test_disabled_adaptive_signals_are_zero(self):
        scorer = CandidateScorer(config=_AdaptiveConfig())
        rankable = Rankable(
            "A",
            "a-1",
            priority=10,
            health_band=BAND_HEALTHY,
            telemetry=make_stats(
                10, 10, ewma_success=1.0, ewma_latency_ms=50.0
            ),
        )

        breakdown = scorer.breakdown(rankable)
        assert breakdown["adaptive_reliability"] == 0.0
        assert breakdown["adaptive_latency"] == 0.0

    def test_disabled_fitness_matches_legacy_components(self):
        scorer = CandidateScorer(config=_AdaptiveConfig())
        rankable = Rankable(
            "A",
            "a-1",
            priority=10,
            health_band=BAND_HEALTHY,
            telemetry=make_stats(
                10, 8, ewma_success=0.2, ewma_latency_ms=50.0
            ),
            preference=0,
        )

        breakdown = scorer.breakdown(rankable)
        legacy = sum(
            breakdown[key]
            for key in (
                "priority",
                "success",
                "latency",
                "failure",
                "preference",
                "task_compatibility",
            )
        )
        assert breakdown["total"] == round(legacy, 4)

    def test_below_min_samples_resolves_to_neutral(self):
        scorer = CandidateScorer(
            config=_AdaptiveConfig(
                adaptive_routing_enabled=True,
                adaptive_min_samples=10,
            )
        )
        rankable = Rankable(
            "A",
            "a-1",
            priority=10,
            health_band=BAND_HEALTHY,
            telemetry=make_stats(
                5, 5, ewma_success=1.0, ewma_latency_ms=50.0
            ),
        )

        breakdown = scorer.breakdown(rankable)
        assert breakdown["adaptive_reliability"] == pytest.approx(0.5)
        assert breakdown["adaptive_latency"] == pytest.approx(0.5)

    def test_confident_ewma_breaks_cumulative_tie(self):
        cfg = _AdaptiveConfig(
            adaptive_routing_enabled=True,
            adaptive_min_samples=10,
        )
        scorer = CandidateScorer(config=cfg)

        good = Rankable(
            "A",
            "a-1",
            priority=10,
            health_band=BAND_HEALTHY,
            telemetry=make_stats(
                10, 5, ewma_success=1.0, ewma_latency_ms=100.0
            ),
        )
        bad = Rankable(
            "B",
            "b-1",
            priority=10,
            health_band=BAND_HEALTHY,
            telemetry=make_stats(
                10, 5, ewma_success=0.0, ewma_latency_ms=100.0
            ),
        )

        # Cumulative signals are identical (5/10 successes, same latency),
        # so with adaptive disabled the order is input order (stable tie).
        disabled = CandidateScorer(config=_AdaptiveConfig())
        assert disabled.rank([good, bad]) == [good, bad]

        # EWMA reliability breaks the tie when adaptive is enabled.
        ranked = scorer.rank([good, bad])
        assert ranked == [good, bad]

        ranked = scorer.rank([bad, good])
        assert ranked == [good, bad]

    def test_confident_ewma_latency_breaks_tie(self):
        scorer = CandidateScorer(
            config=_AdaptiveConfig(
                adaptive_routing_enabled=True,
                adaptive_min_samples=10,
            )
        )

        fast_now = Rankable(
            "A",
            "a-1",
            priority=10,
            health_band=BAND_HEALTHY,
            telemetry=make_stats(
                10, 10, ewma_success=1.0, ewma_latency_ms=50.0
            ),
        )
        slow_now = Rankable(
            "B",
            "b-1",
            priority=10,
            health_band=BAND_HEALTHY,
            telemetry=make_stats(
                10, 10, ewma_success=1.0, ewma_latency_ms=1000.0
            ),
        )

        ranked = scorer.rank([slow_now, fast_now])
        assert ranked == [fast_now, slow_now]

    def test_health_band_always_primary_with_adaptive(self):
        scorer = CandidateScorer(
            config=_AdaptiveConfig(
                adaptive_routing_enabled=True,
                adaptive_min_samples=1,
            )
        )

        degraded_great = Rankable(
            "A",
            "a-1",
            priority=10,
            health_band=BAND_DEGRADED,
            telemetry=make_stats(
                10, 10, ewma_success=1.0, ewma_latency_ms=10.0
            ),
        )
        healthy_bad = Rankable(
            "B",
            "b-1",
            priority=1,
            health_band=BAND_HEALTHY,
            telemetry=make_stats(
                10, 0, ewma_success=0.0, ewma_latency_ms=1000.0
            ),
        )

        ranked = scorer.rank([degraded_great, healthy_bad])
        assert ranked == [healthy_bad, degraded_great]


class TestAdaptiveWeights:
    def test_config_returns_tuning_parameters(self):
        adaptive = AdaptiveWeights(
            config=_AdaptiveConfig(
                adaptive_routing_enabled=True,
                adaptive_min_samples=4,
                adaptive_learning_rate=0.25,
                adaptive_latency_weight=2.0,
                adaptive_reliability_weight=3.0,
            )
        )

        assert adaptive.config() == {
            "enabled": True,
            "min_samples": 4,
            "learning_rate": 0.25,
            "latency_weight": 2.0,
            "reliability_weight": 3.0,
        }

    def test_learning_rate_is_capped(self):
        adaptive = AdaptiveWeights(
            config=_AdaptiveConfig(adaptive_learning_rate=5.0)
        )
        assert adaptive.learning_rate == 1.0

    def test_state_none_without_telemetry(self):
        adaptive = AdaptiveWeights(
            config=_AdaptiveConfig(adaptive_routing_enabled=True),
            telemetry=TelemetryStore(),
        )

        assert adaptive.state(None) is None

    def test_confidence_ramps_with_samples(self):
        store = TelemetryStore()
        store.record_attempt("P", "m", True, latency_ms=100)

        adaptive = AdaptiveWeights(
            config=_AdaptiveConfig(
                adaptive_routing_enabled=True,
                adaptive_min_samples=10,
            ),
            telemetry=store,
        )

        stats = store.get("P", "m")
        state = adaptive.state(stats)
        assert state.confidence == pytest.approx(0.1)

        for _ in range(9):
            store.record_attempt("P", "m", True, latency_ms=100)
        state = adaptive.state(store.get("P", "m"))
        assert state.confidence == 1.0

    def test_disabled_routing_reports_zero_confidence(self):
        store = TelemetryStore()
        store.record_attempt("P", "m", True, latency_ms=100)

        adaptive = AdaptiveWeights(
            config=_AdaptiveConfig(adaptive_routing_enabled=False),
            telemetry=store,
        )

        state = adaptive.state(store.get("P", "m"))
        assert state.confidence == 0.0

    def test_latency_trend_is_ewma_minus_average(self):
        store = TelemetryStore(ewma_alpha=0.5)
        store.record_attempt("P", "m", True, latency_ms=100)
        store.record_attempt("P", "m", True, latency_ms=300)
        store.record_attempt("P", "m", True, latency_ms=500)

        adaptive = AdaptiveWeights(
            config=_AdaptiveConfig(adaptive_routing_enabled=True),
            telemetry=store,
        )

        stats = store.get("P", "m")
        # Average 300; EWMA 100 -> 200 -> 350 (alpha 0.5).
        assert stats.average_latency_ms == 300.0
        assert stats.ewma_latency_ms == pytest.approx(350.0)
        state = adaptive.state(stats)
        assert state.latency_trend_ms == pytest.approx(50.0)

    def test_pool_relative_deltas(self):
        store = TelemetryStore(ewma_alpha=1.0)
        for _ in range(10):
            store.record_attempt("A", "m1", True, latency_ms=100)
        for _ in range(10):
            store.record_attempt("B", "m2", False, latency_ms=500)

        adaptive = AdaptiveWeights(
            config=_AdaptiveConfig(
                adaptive_routing_enabled=True,
                adaptive_min_samples=10,
            ),
            telemetry=store,
        )

        a_state = adaptive.state(store.get("A", "m1"))
        b_state = adaptive.state(store.get("B", "m2"))

        # Pool EWMA success = 0.5; A is 1.0 (delta +0.5), B is 0.0.
        assert a_state.reliability_delta == pytest.approx(0.5)
        assert b_state.reliability_delta == pytest.approx(-0.5)
        # Pool EWMA latency = 300; A (100) is faster (+0.6667), B (-0.6667).
        assert a_state.latency_delta == pytest.approx(0.6667)
        assert b_state.latency_delta == pytest.approx(-0.6667)

    def test_states_sorted_by_observation_count(self):
        store = TelemetryStore()
        store.record_attempt("A", "m1", True, latency_ms=10)
        for _ in range(20):
            store.record_attempt("B", "m2", True, latency_ms=10)

        adaptive = AdaptiveWeights(
            config=_AdaptiveConfig(adaptive_routing_enabled=True),
            telemetry=store,
        )

        states = adaptive.states()
        assert [s.provider for s in states] == ["B", "A"]


class TestAdaptiveRoutingEndToEnd:
    def test_disabled_keeps_input_order_on_tie(self):
        store = TelemetryStore(ewma_alpha=1.0)
        for _ in range(5):
            store.record_attempt("A", "m1", False, latency_ms=50)
        for _ in range(5):
            store.record_attempt("A", "m1", True, latency_ms=50)
        for _ in range(5):
            store.record_attempt("B", "m2", True, latency_ms=50)
        for _ in range(5):
            store.record_attempt("B", "m2", False, latency_ms=50)

        builder = CandidateBuilder(
            health_store=HealthStore(),
            telemetry=store,
            config=_AdaptiveConfig(adaptive_routing_enabled=False),
        )
        providers = [
            make_provider("A", ["m1"], priority=10),
            make_provider("B", ["m2"], priority=10),
        ]

        ranked = builder.ranked_candidates(providers)
        assert [c.provider for c in ranked] == ["A", "B"]

    def test_enabled_reorders_within_band_by_ewma(self):
        store = TelemetryStore(ewma_alpha=1.0)
        # A: 5 successes then 5 failures -> EWMA reliability 0.0.
        for _ in range(5):
            store.record_attempt("A", "m1", True, latency_ms=50)
        for _ in range(5):
            store.record_attempt("A", "m1", False, latency_ms=50)
        # B: 5 failures then 5 successes -> EWMA reliability 1.0.
        for _ in range(5):
            store.record_attempt("B", "m2", False, latency_ms=50)
        for _ in range(5):
            store.record_attempt("B", "m2", True, latency_ms=50)

        builder = CandidateBuilder(
            health_store=HealthStore(),
            telemetry=store,
            config=_AdaptiveConfig(
                adaptive_routing_enabled=True,
                adaptive_min_samples=10,
            ),
        )
        providers = [
            make_provider("A", ["m1"], priority=10),
            make_provider("B", ["m2"], priority=10),
        ]

        ranked = builder.ranked_candidates(providers)
        assert [c.provider for c in ranked] == ["B", "A"]

        # The adaptive contribution is visible in the breakdown.
        a_breakdown = next(
            c.breakdown for c in ranked if c.provider == "A"
        )
        b_breakdown = next(
            c.breakdown for c in ranked if c.provider == "B"
        )
        assert b_breakdown["adaptive_reliability"] > a_breakdown[
            "adaptive_reliability"
        ]

    def test_below_min_samples_no_reordering(self):
        store = TelemetryStore(ewma_alpha=1.0)
        # A: fail, success, success -> 2/3 successes, EWMA reliability 1.0.
        store.record_attempt("A", "m1", False, latency_ms=50)
        store.record_attempt("A", "m1", True, latency_ms=50)
        store.record_attempt("A", "m1", True, latency_ms=50)
        # B: success, success, fail -> 2/3 successes, EWMA reliability 0.0.
        store.record_attempt("B", "m2", True, latency_ms=50)
        store.record_attempt("B", "m2", True, latency_ms=50)
        store.record_attempt("B", "m2", False, latency_ms=50)

        # Providers are given B first so the EWMA ordering (A better)
        # differs from the input order.
        providers = [
            make_provider("B", ["m2"], priority=10),
            make_provider("A", ["m1"], priority=10),
        ]

        gated = CandidateBuilder(
            health_store=HealthStore(),
            telemetry=store,
            config=_AdaptiveConfig(
                adaptive_routing_enabled=True,
                adaptive_min_samples=10,
            ),
        )
        ranked = gated.ranked_candidates(providers)
        assert [c.provider for c in ranked] == ["B", "A"]

        confident = CandidateBuilder(
            health_store=HealthStore(),
            telemetry=store,
            config=_AdaptiveConfig(
                adaptive_routing_enabled=True,
                adaptive_min_samples=1,
            ),
        )
        ranked = confident.ranked_candidates(providers)
        assert [c.provider for c in ranked] == ["A", "B"]
