from app.providers.base import Provider
from app.services.candidate_builder import CandidateBuilder
from app.services.health_checker import (
    DEGRADED,
    HEALTHY,
    UNAVAILABLE,
    ProviderHealth,
)
from app.services.health_store import HealthStore
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


def make_report(
    name,
    status,
    healthy=(),
    degraded=(),
    unavailable=(),
    unsupported=(),
):
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
        unavailable_models=list(unavailable),
        unsupported_models=list(unsupported),
    )


class _FakeSettings:
    def __init__(self, health_aware_routing=False):
        self.health_aware_routing = health_aware_routing


def build_candidates(store, providers, enabled=True, task=None, telemetry=None):
    builder = CandidateBuilder(
        health_store=store,
        telemetry=telemetry,
        config=_FakeSettings(health_aware_routing=enabled),
    )
    return builder.build(providers, task=task)


def record_stats(store, provider, model, successes, failures, latency=100):
    for _ in range(successes):
        store.record_attempt(provider, model, True, latency)
    for _ in range(failures):
        store.record_attempt(
            provider, model, False, latency, "server_error"
        )


class TestHealthAwareCandidateBuilder:
    def test_flag_off_returns_identical_candidates(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        store.save(make_report("B", UNAVAILABLE, unavailable=("b-1",)))
        p_a = make_provider("A", ["a-1"], priority=5)
        p_b = make_provider("B", ["b-1"], priority=10)

        candidates = build_candidates(store, [p_b, p_a], enabled=False)

        assert [(p.name, m) for p, m in candidates] == [
            ("B", "b-1"),
            ("A", "a-1"),
        ]

    def test_no_health_data_preserves_ordering(self):
        store = HealthStore()
        p_a = make_provider("A", ["a-1"], priority=5)
        p_b = make_provider("B", ["b-1"], priority=10)

        candidates = build_candidates(store, [p_b, p_a], enabled=True)

        assert [(p.name, m) for p, m in candidates] == [
            ("B", "b-1"),
            ("A", "a-1"),
        ]

    def test_expired_health_data_preserves_ordering(self):
        store = HealthStore(ttl_seconds=-1)
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        store.save(make_report("B", DEGRADED, degraded=("b-1",)))
        p_a = make_provider("A", ["a-1"], priority=5)
        p_b = make_provider("B", ["b-1"], priority=10)

        candidates = build_candidates(store, [p_b, p_a], enabled=True)

        assert [(p.name, m) for p, m in candidates] == [
            ("B", "b-1"),
            ("A", "a-1"),
        ]

    def test_healthy_provider_preferred_over_degraded(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        store.save(make_report("B", DEGRADED, degraded=("b-1",)))
        p_a = make_provider("A", ["a-1"], priority=1)
        p_b = make_provider("B", ["b-1"], priority=10)

        candidates = build_candidates(store, [p_b, p_a], enabled=True)

        assert [(p.name, m) for p, m in candidates] == [
            ("A", "a-1"),
            ("B", "b-1"),
        ]

    def test_unavailable_models_removed(self):
        store = HealthStore()
        store.save(
            make_report(
                "A",
                HEALTHY,
                healthy=("a-1",),
                unavailable=("a-2",),
            )
        )
        provider = make_provider("A", ["a-1", "a-2"])

        candidates = build_candidates(store, [provider], enabled=True)

        assert [(p.name, m) for p, m in candidates] == [("A", "a-1")]

    def test_unsupported_models_removed(self):
        store = HealthStore()
        store.save(
            make_report(
                "A",
                HEALTHY,
                healthy=("a-1",),
                unsupported=("a-3",),
            )
        )
        provider = make_provider("A", ["a-1", "a-3"])

        candidates = build_candidates(store, [provider], enabled=True)

        assert [(p.name, m) for p, m in candidates] == [("A", "a-1")]

    def test_all_models_unhealthy_falls_back_to_original(self):
        store = HealthStore()
        store.save(
            make_report(
                "A",
                UNAVAILABLE,
                unavailable=("a-1", "a-2"),
            )
        )
        provider = make_provider("A", ["a-1", "a-2"])

        candidates = build_candidates(store, [provider], enabled=True)

        assert [(p.name, m) for p, m in candidates] == [
            ("A", "a-1"),
            ("A", "a-2"),
        ]

    def test_same_health_band_keeps_priority_order(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        store.save(make_report("B", HEALTHY, healthy=("b-1",)))
        store.save(make_report("C", HEALTHY, healthy=("c-1",)))
        p_a = make_provider("A", ["a-1"], priority=1)
        p_b = make_provider("B", ["b-1"], priority=10)
        p_c = make_provider("C", ["c-1"], priority=5)

        candidates = build_candidates(
            store, [p_b, p_c, p_a], enabled=True
        )

        assert [(p.name, m) for p, m in candidates] == [
            ("B", "b-1"),
            ("C", "c-1"),
            ("A", "a-1"),
        ]


class TestHealthAwareCandidateBuilderFeedback:
    def test_learned_degradation_demotes_provider(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        store.save(make_report("B", HEALTHY, healthy=("b-1",)))
        store.record_failure("A", "a-1", "rate_limit")
        p_a = make_provider("A", ["a-1"], priority=1)
        p_b = make_provider("B", ["b-1"], priority=10)

        candidates = build_candidates(store, [p_a, p_b], enabled=True)

        assert [(p.name, m) for p, m in candidates] == [
            ("B", "b-1"),
            ("A", "a-1"),
        ]

    def test_learned_unavailable_provider_goes_last(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        store.record_failure("A", "a-1", "auth_error")
        p_a = make_provider("A", ["a-1"], priority=1)
        p_b = make_provider("B", ["b-1"], priority=10)

        candidates = build_candidates(store, [p_b, p_a], enabled=True)

        assert [(p.name, m) for p, m in candidates] == [
            ("B", "b-1"),
            ("A", "a-1"),
        ]

    def test_learned_unavailable_model_removed(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        for _ in range(5):
            store.record_failure("A", "a-1", "timeout")
        p_a = make_provider("A", ["a-1"])
        p_b = make_provider("B", ["b-1"])

        candidates = build_candidates(store, [p_a, p_b], enabled=True)

        assert [(p.name, m) for p, m in candidates] == [("B", "b-1")]

    def test_success_clears_learned_degradation(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        store.save(make_report("B", HEALTHY, healthy=("b-1",)))
        store.record_failure("A", "a-1", "rate_limit")
        store.record_success("A", "a-1")
        p_a = make_provider("A", ["a-1"], priority=1)
        p_b = make_provider("B", ["b-1"], priority=10)

        candidates = build_candidates(store, [p_a, p_b], enabled=True)

        assert [(p.name, m) for p, m in candidates] == [
            ("A", "a-1"),
            ("B", "b-1"),
        ]

    def test_all_models_learned_unavailable_falls_back(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        for _ in range(5):
            store.record_failure("A", "a-1", "timeout")
        p_a = make_provider("A", ["a-1"])

        candidates = build_candidates(store, [p_a], enabled=True)

        assert [(p.name, m) for p, m in candidates] == [("A", "a-1")]


class TestHealthAwareCandidateBuilderScoring:
    def test_no_telemetry_keeps_priority_order(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        store.save(make_report("B", HEALTHY, healthy=("b-1",)))
        telemetry = TelemetryStore()
        p_a = make_provider("A", ["a-1"], priority=10)
        p_b = make_provider("B", ["b-1"], priority=1)

        candidates = build_candidates(
            store, [p_a, p_b], enabled=True, telemetry=telemetry
        )

        assert [(p.name, m) for p, m in candidates] == [
            ("A", "a-1"),
            ("B", "b-1"),
        ]

    def test_same_band_candidates_reorder_by_latency(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        store.save(make_report("B", HEALTHY, healthy=("b-1",)))
        telemetry = TelemetryStore()
        record_stats(telemetry, "A", "a-1", 1, 0, latency=1000)
        record_stats(telemetry, "B", "b-1", 1, 0, latency=50)
        p_a = make_provider("A", ["a-1"], priority=10)
        p_b = make_provider("B", ["b-1"], priority=1)

        candidates = build_candidates(
            store, [p_a, p_b], enabled=True, telemetry=telemetry
        )

        assert [(p.name, m) for p, m in candidates] == [
            ("B", "b-1"),
            ("A", "a-1"),
        ]

    def test_same_band_candidates_reorder_by_success_rate(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        store.save(make_report("B", HEALTHY, healthy=("b-1",)))
        telemetry = TelemetryStore()
        record_stats(telemetry, "A", "a-1", 1, 4, latency=100)
        record_stats(telemetry, "B", "b-1", 5, 0, latency=100)
        p_a = make_provider("A", ["a-1"], priority=10)
        p_b = make_provider("B", ["b-1"], priority=1)

        candidates = build_candidates(
            store, [p_a, p_b], enabled=True, telemetry=telemetry
        )

        assert [(p.name, m) for p, m in candidates] == [
            ("B", "b-1"),
            ("A", "a-1"),
        ]

    def test_healthy_band_beats_degraded_despite_telemetry(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        store.save(make_report("B", DEGRADED, degraded=("b-1",)))
        telemetry = TelemetryStore()
        record_stats(telemetry, "A", "a-1", 0, 5, latency=5000)
        record_stats(telemetry, "B", "b-1", 5, 0, latency=5)
        p_a = make_provider("A", ["a-1"], priority=1)
        p_b = make_provider("B", ["b-1"], priority=100)

        candidates = build_candidates(
            store, [p_b, p_a], enabled=True, telemetry=telemetry
        )

        assert [(p.name, m) for p, m in candidates] == [
            ("A", "a-1"),
            ("B", "b-1"),
        ]

    def test_disabled_flag_keeps_old_behavior(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        store.save(make_report("B", HEALTHY, healthy=("b-1",)))
        telemetry = TelemetryStore()
        record_stats(telemetry, "A", "a-1", 0, 5, latency=1000)
        record_stats(telemetry, "B", "b-1", 5, 0, latency=50)
        p_a = make_provider("A", ["a-1"], priority=10)
        p_b = make_provider("B", ["b-1"], priority=1)

        candidates = build_candidates(
            store, [p_a, p_b], enabled=False, telemetry=telemetry
        )

        assert [(p.name, m) for p, m in candidates] == [
            ("A", "a-1"),
            ("B", "b-1"),
        ]

    def test_stable_order_when_scores_equal(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        store.save(make_report("B", HEALTHY, healthy=("b-1",)))
        telemetry = TelemetryStore()
        record_stats(telemetry, "A", "a-1", 1, 0, latency=100)
        record_stats(telemetry, "B", "b-1", 1, 0, latency=100)
        p_a = make_provider("A", ["a-1"], priority=5)
        p_b = make_provider("B", ["b-1"], priority=5)

        candidates = build_candidates(
            store, [p_a, p_b], enabled=True, telemetry=telemetry
        )

        assert [(p.name, m) for p, m in candidates] == [
            ("A", "a-1"),
            ("B", "b-1"),
        ]
