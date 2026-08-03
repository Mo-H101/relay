from app.providers.base import Provider
from app.services.candidate_builder import CandidateBuilder
from app.services.health_checker import DEGRADED, HEALTHY, ProviderHealth
from app.services.health_store import HealthStore
from app.services.routing import RoutingEngine
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


def make_settings(
    task_coding=None,
    cross_provider=False,
    health_aware_routing=False,
    task_routing_enabled=True,
):
    return _FakeSettings(
        task_routing_enabled=task_routing_enabled,
        task_coding=task_coding or [],
        task_vision=[],
        task_reasoning=[],
        task_general=[],
        task_creative=[],
        task_translation=[],
        cross_provider_model_selection=cross_provider,
        health_aware_routing=health_aware_routing,
    )


def build(
    providers,
    task=None,
    settings=None,
    store=None,
    telemetry=None,
):
    settings = settings or make_settings()
    routing = RoutingEngine(config=settings)
    builder = CandidateBuilder(
        routing=routing,
        health_store=store,
        telemetry=telemetry,
        config=settings,
    )
    return builder.build(providers, task=task)


def record_stats(store, provider, model, successes, failures, latency=100):
    for _ in range(successes):
        store.record_attempt(provider, model, True, latency)
    for _ in range(failures):
        store.record_attempt(
            provider, model, False, latency, "server_error"
        )


class TestRoutingResolution:
    def test_resolve_flag_off_matches_first_provider(self):
        routing = RoutingEngine(config=make_settings(task_coding=["m1"]))
        p_a = make_provider("A", ["m1"], priority=10)
        p_b = make_provider("B", ["m1"], priority=1)

        candidates = routing.resolve(["m1"], [p_a, p_b])

        assert [(p.name, m) for p, m in candidates] == [("A", "m1")]

    def test_resolve_flag_on_matches_all_providers(self):
        routing = RoutingEngine(
            config=make_settings(task_coding=["m1"], cross_provider=True)
        )
        p_a = make_provider("A", ["m1"], priority=10)
        p_b = make_provider("B", ["m1"], priority=1)

        candidates = routing.resolve(["m1"], [p_a, p_b])

        assert [(p.name, m) for p, m in candidates] == [
            ("A", "m1"),
            ("B", "m1"),
        ]

    def test_resolve_weighted_returns_ref_indices(self):
        routing = RoutingEngine(
            config=make_settings(task_coding=["m1", "m2"], cross_provider=True)
        )
        p_a = make_provider("A", ["m1", "m2"])
        p_b = make_provider("B", ["m1"])

        candidates = routing.resolve_weighted(["m1", "m2"], [p_a, p_b])

        assert [(p.name, m, index) for p, m, index in candidates] == [
            ("A", "m1", 0),
            ("B", "m1", 0),
            ("A", "m2", 1),
        ]

    def test_explicit_ref_ignores_cross_provider_flag(self):
        routing = RoutingEngine(
            config=make_settings(task_coding=["B:m1"], cross_provider=True)
        )
        p_a = make_provider("A", ["m1"], priority=10)
        p_b = make_provider("B", ["m1"], priority=1)

        candidates = routing.resolve(["B:m1"], [p_a, p_b])

        assert [(p.name, m) for p, m in candidates] == [("B", "m1")]


class TestCrossProviderSelection:
    def test_same_model_on_multiple_providers_becomes_candidates(self):
        p_a = make_provider("A", ["m1"], priority=10)
        p_b = make_provider("B", ["m1"], priority=1)
        settings = make_settings(task_coding=["m1"], cross_provider=True)

        candidates = build([p_a, p_b], task="coding", settings=settings)

        assert [(p.name, m) for p, m in candidates] == [
            ("A", "m1"),
            ("B", "m1"),
        ]

    def test_flag_off_preserves_first_provider_behavior(self):
        p_a = make_provider("A", ["m1"], priority=10)
        p_b = make_provider("B", ["m1"], priority=1)
        settings = make_settings(task_coding=["m1"], cross_provider=False)

        candidates = build([p_a, p_b], task="coding", settings=settings)

        assert [(p.name, m) for p, m in candidates] == [("A", "m1")]

    def test_flag_on_enables_competition(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("m1",)))
        store.save(make_report("B", HEALTHY, healthy=("m1",)))
        p_a = make_provider("A", ["m1"], priority=10)
        p_b = make_provider("B", ["m1"], priority=1)
        settings = make_settings(
            task_coding=["m1"],
            cross_provider=True,
            health_aware_routing=True,
        )

        candidates = build(
            [p_a, p_b], task="coding", settings=settings, store=store
        )

        assert [(p.name, m) for p, m in candidates] == [
            ("A", "m1"),
            ("B", "m1"),
        ]

    def test_explicit_provider_ref_is_unchanged(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("m1",)))
        store.save(make_report("B", HEALTHY, healthy=("m1",)))
        p_a = make_provider("A", ["m1"], priority=10)
        p_b = make_provider("B", ["m1"], priority=1)
        settings = make_settings(
            task_coding=["B:m1"],
            cross_provider=True,
            health_aware_routing=True,
        )

        candidates = build(
            [p_a, p_b], task="coding", settings=settings, store=store
        )

        assert [(p.name, m) for p, m in candidates] == [("B", "m1")]

    def test_flag_off_health_cannot_select_second_provider(self):
        store = HealthStore()
        store.save(make_report("A", DEGRADED, degraded=("m1",)))
        store.save(make_report("B", HEALTHY, healthy=("m1",)))
        p_a = make_provider("A", ["m1"], priority=10)
        p_b = make_provider("B", ["m1"], priority=1)
        settings = make_settings(
            task_coding=["m1"],
            cross_provider=False,
            health_aware_routing=True,
        )

        candidates = build(
            [p_a, p_b], task="coding", settings=settings, store=store
        )

        assert [(p.name, m) for p, m in candidates] == [("A", "m1")]


class TestHealthAndTelemetryReorder:
    def test_health_beats_priority(self):
        store = HealthStore()
        store.save(make_report("A", DEGRADED, degraded=("m1",)))
        store.save(make_report("B", HEALTHY, healthy=("m1",)))
        p_a = make_provider("A", ["m1"], priority=10)
        p_b = make_provider("B", ["m1"], priority=1)
        settings = make_settings(
            task_coding=["m1"],
            cross_provider=True,
            health_aware_routing=True,
        )

        candidates = build(
            [p_a, p_b], task="coding", settings=settings, store=store
        )

        assert [(p.name, m) for p, m in candidates] == [
            ("B", "m1"),
            ("A", "m1"),
        ]

    def test_telemetry_breaks_same_health_ties(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("m1",)))
        store.save(make_report("B", HEALTHY, healthy=("m1",)))
        telemetry = TelemetryStore()
        record_stats(telemetry, "A", "m1", 1, 0, latency=1000)
        record_stats(telemetry, "B", "m1", 1, 0, latency=50)
        p_a = make_provider("A", ["m1"], priority=10)
        p_b = make_provider("B", ["m1"], priority=1)
        settings = make_settings(
            task_coding=["m1"],
            cross_provider=True,
            health_aware_routing=True,
        )

        candidates = build(
            [p_a, p_b],
            task="coding",
            settings=settings,
            store=store,
            telemetry=telemetry,
        )

        assert [(p.name, m) for p, m in candidates] == [
            ("B", "m1"),
            ("A", "m1"),
        ]

    def test_unrelated_models_are_not_selected(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("m1",)))
        p_a = make_provider("A", ["m1", "m3"])
        p_b = make_provider("B", ["m2"])
        settings = make_settings(
            task_coding=["m1"],
            cross_provider=True,
            health_aware_routing=True,
        )

        candidates = build(
            [p_a, p_b], task="coding", settings=settings, store=store
        )

        assert [(p.name, m) for p, m in candidates] == [("A", "m1")]


class TestTaskPreference:
    def test_task_preference_ordering_preserved(self):
        p_a = make_provider("A", ["m1", "m2"], priority=10)
        p_b = make_provider("B", ["m1"], priority=1)
        settings = make_settings(
            task_coding=["m1", "m2"], cross_provider=True
        )

        candidates = build(
            [p_a, p_b], task="coding", settings=settings
        )

        assert [(p.name, m) for p, m in candidates] == [
            ("A", "m1"),
            ("B", "m1"),
            ("A", "m2"),
        ]

    def test_task_preference_ordering_kept_within_band(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("m1", "m2")))
        store.save(make_report("B", HEALTHY, healthy=("m1",)))
        p_a = make_provider("A", ["m1", "m2"], priority=10)
        p_b = make_provider("B", ["m1"], priority=1)
        settings = make_settings(
            task_coding=["m1", "m2"],
            cross_provider=True,
            health_aware_routing=True,
        )

        candidates = build(
            [p_a, p_b], task="coding", settings=settings, store=store
        )

        assert [(p.name, m) for p, m in candidates] == [
            ("A", "m1"),
            ("B", "m1"),
            ("A", "m2"),
        ]

    def test_preference_weight_keeps_earlier_ref_despite_telemetry(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("m1",)))
        store.save(make_report("B", HEALTHY, healthy=("m2",)))
        telemetry = TelemetryStore()
        record_stats(telemetry, "A", "m1", 1, 0, latency=100)
        record_stats(telemetry, "B", "m2", 1, 0, latency=10)
        p_a = make_provider("A", ["m1"], priority=5)
        p_b = make_provider("B", ["m2"], priority=5)
        settings = make_settings(
            task_coding=["m1", "m2"],
            cross_provider=True,
            health_aware_routing=True,
        )

        candidates = build(
            [p_a, p_b],
            task="coding",
            settings=settings,
            store=store,
            telemetry=telemetry,
        )

        assert [(p.name, m) for p, m in candidates] == [
            ("A", "m1"),
            ("B", "m2"),
        ]
