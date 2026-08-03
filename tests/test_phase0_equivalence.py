"""
Phase 0 equivalence harness.

Proves that with all Phase 7A/7B features disabled (the default), every
candidate ordering invariant from the legacy formula holds exactly:
the health band stays the primary key, telemetry/priority/preference
ordering within a band is unchanged, the new task-compatibility signal
contributes exactly zero, and zero adaptive deltas leave fitness
unchanged.
"""

from app.providers.base import Provider
from app.services.candidate_builder import CandidateBuilder
from app.services.health_checker import DEGRADED, HEALTHY, ProviderHealth
from app.services.health_store import HealthStore
from app.services.scoring import CandidateScorer, Rankable
from app.services.telemetry import TelemetryStore


def make_provider(name, models, priority=1):
    return Provider(
        name=name,
        base_url=f"https://{name.lower()}.invalid",
        api_key="test-key",
        enabled=True,
        priority=priority,
        models=list(models),
    )


def make_report(name, status, healthy=(), degraded=()):
    return ProviderHealth(
        name=name,
        status=status,
        latency_ms=5,
        last_checked="now",
        details="ok",
        connectivity=True,
        rate_limit_status="ok",
        last_successful_request=None,
        healthy_models=list(healthy),
        degraded_models=list(degraded),
        unavailable_models=[],
        unsupported_models=[],
    )


class _FakeSettings:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


def build_candidates(store, providers, enabled=True, task=None, telemetry=None):
    builder = CandidateBuilder(
        health_store=store,
        telemetry=telemetry,
        config=_FakeSettings(
            health_aware_routing=enabled,
            task_catalog_enabled=False,
        ),
    )
    return builder.build(providers, task=task)


def record_stats(store, provider, model, successes, failures, latency=100):
    for _ in range(successes):
        store.record_attempt(provider, model, True, latency)
    for _ in range(failures):
        store.record_attempt(
            provider, model, False, latency, "server_error"
        )


class TestFlagsOffBuildEquivalence:
    def test_health_off_preserves_priority_order(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        store.save(make_report("B", DEGRADED, degraded=("b-1",)))
        p_a = make_provider("A", ["a-1"], priority=10)
        p_b = make_provider("B", ["b-1"], priority=1)
        from app.services.provider_manager import ProviderManager

        manager = ProviderManager()
        manager.register(p_b)
        manager.register(p_a)

        candidates = build_candidates(store, manager.ranked(), enabled=False)

        assert [(p.name, m) for p, m in candidates] == [
            ("A", "a-1"),
            ("B", "b-1"),
        ]

    def test_health_band_invariant_preserved(self):
        store = HealthStore()
        store.save(make_report("A", DEGRADED, degraded=("a-1",)))
        store.save(make_report("B", HEALTHY, healthy=("b-1",)))
        telemetry = TelemetryStore()
        record_stats(telemetry, "A", "a-1", 10, 0, latency=1)
        record_stats(telemetry, "B", "b-1", 0, 10, latency=5000)
        p_a = make_provider("A", ["a-1"], priority=100)
        p_b = make_provider("B", ["b-1"], priority=1)

        candidates = build_candidates(
            store,
            [p_a, p_b],
            enabled=True,
            telemetry=telemetry,
        )

        assert [(p.name, m) for p, m in candidates] == [
            ("B", "b-1"),
            ("A", "a-1"),
        ]

    def test_within_band_telemetry_ordering_preserved(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        store.save(make_report("B", HEALTHY, healthy=("b-1",)))
        telemetry = TelemetryStore()
        record_stats(telemetry, "A", "a-1", 1, 0, latency=1000)
        record_stats(telemetry, "B", "b-1", 1, 0, latency=50)
        p_a = make_provider("A", ["a-1"], priority=10)
        p_b = make_provider("B", ["b-1"], priority=1)

        candidates = build_candidates(
            store,
            [p_a, p_b],
            enabled=True,
            telemetry=telemetry,
        )

        assert [(p.name, m) for p, m in candidates] == [
            ("B", "b-1"),
            ("A", "a-1"),
        ]

    def test_task_routing_preference_ordering_preserved(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("m1",)))
        store.save(make_report("B", HEALTHY, healthy=("m2",)))
        p_a = make_provider("A", ["m1"], priority=5)
        p_b = make_provider("B", ["m2"], priority=5)
        from app.services.routing import RoutingEngine

        settings = _FakeSettings(
            task_routing_enabled=True,
            task_coding=["m1", "m2"],
            task_vision=[],
            task_reasoning=[],
            task_general=[],
            task_creative=[],
            task_translation=[],
            cross_provider_model_selection=True,
            health_aware_routing=False,
            task_catalog_enabled=False,
        )

        builder = CandidateBuilder(
            routing=RoutingEngine(config=settings),
            health_store=store,
            config=settings,
        )

        candidates = builder.build([p_a, p_b], task="coding")

        assert [(p.name, m) for p, m in candidates] == [
            ("A", "m1"),
            ("B", "m2"),
        ]


class TestCatalogOffIdentity:
    def test_ordering_identical_to_legacy_when_catalog_off(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("gpt-3.5-turbo",)))
        store.save(make_report("B", HEALTHY, healthy=("gpt-5.6-sol",)))
        telemetry = TelemetryStore()
        record_stats(telemetry, "A", "gpt-3.5-turbo", 1, 0, latency=100)
        record_stats(telemetry, "B", "gpt-5.6-sol", 1, 0, latency=100)
        p_a = make_provider("A", ["gpt-3.5-turbo"], priority=5)
        p_b = make_provider("B", ["gpt-5.6-sol"], priority=5)

        candidates = build_candidates(
            store,
            [p_a, p_b],
            enabled=True,
            task="coding",
            telemetry=telemetry,
        )

        # Equal legacy fitness: stable input order, even though the
        # catalog (if enabled) would prefer B for "coding".
        assert [(p.name, m) for p, m in candidates] == [
            ("A", "gpt-3.5-turbo"),
            ("B", "gpt-5.6-sol"),
        ]


class TestScorerEquivalence:
    def test_task_compatibility_contributes_zero_when_disabled(self):
        scorer = CandidateScorer()

        breakdown = scorer.breakdown(Rankable("A", "a-1", priority=5))

        assert breakdown["task_compatibility"] == 0.0
        assert scorer.fitness(5) == scorer.fitness(5, task_compatibility=0.9)

    def test_adaptive_deltas_zero_equal_baseline(self):
        scorer = CandidateScorer()
        zero_deltas = {
            key: 0.0
            for key in (
                "priority",
                "success",
                "latency",
                "failure",
                "preference",
                "task_compatibility",
            )
        }

        assert scorer.fitness(5, adaptive_weights=zero_deltas) == scorer.fitness(5)

    def test_legacy_breakdown_values_unchanged_when_disabled(self):
        scorer = CandidateScorer()
        rankable = Rankable(
            "A",
            "a-1",
            priority=10,
            health_band=0,
            preference=0,
        )

        breakdown = scorer.breakdown(rankable)

        assert breakdown["priority"] == 0.5
        assert breakdown["success"] == 0.5
        assert breakdown["latency"] == 0.5
        assert breakdown["failure"] == 1.0
        assert breakdown["preference"] == 1.0
        assert breakdown["task_compatibility"] == 0.0
        assert breakdown["total"] == 3.5

    def test_breakdown_always_emits_every_signal_key(self):
        scorer = CandidateScorer()

        breakdown = scorer.breakdown(Rankable("A", "a-1", priority=5))

        assert set(breakdown) == {
            "health_band",
            "priority",
            "success",
            "latency",
            "failure",
            "preference",
            "task_compatibility",
            "adaptive_reliability",
            "adaptive_latency",
            "quality",
            "cost",
            "total",
        }
