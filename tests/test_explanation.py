from app.providers.base import Provider
from app.services.candidate_builder import CandidateBuilder
from app.services.explanation import ExplanationService
from app.services.health_checker import DEGRADED, HEALTHY, ProviderHealth
from app.services.health_store import HealthStore
from app.services.routing import RoutingEngine
from app.services.scoring import (
    BAND_HEALTHY,
    BAND_NOT_CHECKED,
    CandidateScorer,
    Rankable,
)
from app.services.telemetry import (
    FailureEvent,
    TelemetryStats,
    TelemetryStore,
)


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
    health_aware_routing=True,
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


def build_builder(
    store=None,
    telemetry=None,
    task_coding=None,
    cross_provider=False,
    health_aware=True,
):
    settings = make_settings(
        task_coding=task_coding,
        cross_provider=cross_provider,
        health_aware_routing=health_aware,
    )
    routing = RoutingEngine(config=settings)
    return CandidateBuilder(
        routing=routing,
        health_store=store,
        telemetry=telemetry,
        config=settings,
    )


def build(
    store,
    providers,
    health_aware=True,
    telemetry=None,
    task=None,
    task_coding=None,
    cross_provider=False,
):
    builder = build_builder(
        store=store,
        telemetry=telemetry,
        task_coding=task_coding,
        cross_provider=cross_provider,
        health_aware=health_aware,
    )
    return builder.ranked_candidates(providers, task=task)


def record_stats(store, provider, model, successes, failures, latency=100):
    for _ in range(successes):
        store.record_attempt(provider, model, True, latency)
    for _ in range(failures):
        store.record_attempt(
            provider, model, False, latency, "server_error"
        )


def make_stats(
    request_count=4,
    success_count=3,
    failure_count=1,
    average_latency_ms=100.0,
    recent_failures=None,
):
    return TelemetryStats(
        provider="A",
        model="a-1",
        request_count=request_count,
        success_count=success_count,
        failure_count=failure_count,
        average_latency_ms=average_latency_ms,
        recent_failures=recent_failures or [],
    )


def make_failures(kinds):
    return [
        FailureEvent(failure_type=kind, ts=float(index))
        for index, kind in enumerate(kinds)
    ]


class TestScoreBreakdown:
    def test_breakdown_matches_expected_contributions(self):
        scorer = CandidateScorer()
        rankable = Rankable(
            "A",
            "a-1",
            priority=10,
            health_band=BAND_HEALTHY,
            telemetry=make_stats(),
            preference=0,
        )

        breakdown = scorer.breakdown(rankable)

        assert breakdown["health_band"] == BAND_HEALTHY
        assert breakdown["priority"] == 0.5
        assert breakdown["success"] == 0.75
        assert breakdown["latency"] == 0.7143
        assert breakdown["failure"] == 1.0
        assert breakdown["preference"] == 1.0
        assert breakdown["total"] == 3.9643

        components = sum(
            breakdown[key]
            for key in ("priority", "success", "latency", "failure", "preference")
        )

        assert abs(breakdown["total"] - components) < 1e-9

    def test_breakdown_neutral_on_cold_start(self):
        scorer = CandidateScorer()
        rankable = Rankable("A", "a-1", priority=5)

        breakdown = scorer.breakdown(rankable)

        assert breakdown["health_band"] == BAND_NOT_CHECKED
        assert breakdown["success"] == 0.5
        assert breakdown["latency"] == 0.5
        assert breakdown["failure"] == 1.0
        assert breakdown["preference"] == 0.5

    def test_breakdown_reflects_failure_penalty(self):
        scorer = CandidateScorer()
        rankable = Rankable(
            "A",
            "a-1",
            priority=5,
            telemetry=make_stats(
                request_count=5,
                success_count=0,
                failure_count=5,
                recent_failures=make_failures(["timeout"] * 5),
            ),
        )

        breakdown = scorer.breakdown(rankable)

        assert breakdown["failure"] == 0.0


class TestSelectedCandidateExplanation:
    def test_selected_candidate_reasons_and_breakdown(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        ranked = build(store, [make_provider("A", ["a-1"], priority=5)])

        result = ExplanationService().explain(ranked, health_aware=True)

        assert result["selected"] == {"provider": "A", "model": "a-1"}
        top = result["candidates"][0]
        assert top["rank"] == 1
        assert top["provider"] == "A"
        assert top["model"] == "a-1"
        assert top["score_breakdown"]["health_band"] == 0

        joined = " ".join(top["reasons"])
        assert "Health band: healthy." in joined
        assert "Selected: best-ranked candidate." in joined
        assert "No telemetry recorded yet (cold start)." in joined


class TestHealthInfluenceExplanation:
    def test_worse_health_band_explains_lower_rank(self):
        store = HealthStore()
        store.save(make_report("A", DEGRADED, degraded=("a-1",)))
        store.save(make_report("B", HEALTHY, healthy=("b-1",)))
        p_a = make_provider("A", ["a-1"], priority=10)
        p_b = make_provider("B", ["b-1"], priority=1)

        ranked = build(store, [p_a, p_b])
        result = ExplanationService().explain(ranked, health_aware=True)

        assert result["selected"] == {"provider": "B", "model": "b-1"}
        lower = result["candidates"][1]
        assert lower["provider"] == "A"
        assert lower["rank"] == 2
        assert "worse health band (degraded vs healthy)" in " ".join(
            lower["reasons"]
        )


class TestTelemetryInfluenceExplanation:
    def test_telemetry_difference_explains_lower_rank(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        store.save(make_report("B", HEALTHY, healthy=("b-1",)))
        telemetry = TelemetryStore()
        record_stats(telemetry, "A", "a-1", 1, 0, latency=1000)
        record_stats(telemetry, "B", "b-1", 1, 0, latency=50)
        p_a = make_provider("A", ["a-1"], priority=10)
        p_b = make_provider("B", ["b-1"], priority=1)

        ranked = build(
            store,
            [p_a, p_b],
            telemetry=telemetry,
        )
        result = ExplanationService().explain(ranked, health_aware=True)

        assert result["selected"] == {"provider": "B", "model": "b-1"}
        lower = result["candidates"][1]
        assert lower["provider"] == "A"
        joined = " ".join(lower["reasons"])
        assert "worse average-latency contribution" in joined
        assert "Telemetry:" in joined


class TestPreferenceInfluenceExplanation:
    def test_preference_reason_and_breakdown(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("m1",)))
        store.save(make_report("B", HEALTHY, healthy=("m2",)))
        p_a = make_provider("A", ["m1"], priority=5)
        p_b = make_provider("B", ["m2"], priority=5)

        ranked = build(
            store,
            [p_a, p_b],
            task="coding",
            task_coding=["m1", "m2"],
            cross_provider=True,
        )
        result = ExplanationService().explain(
            ranked, task="coding", health_aware=True
        )

        top = result["candidates"][0]
        assert (top["provider"], top["model"]) == ("A", "m1")
        assert top["score_breakdown"]["preference"] == 1.0
        assert "Task preference: reference #1." in " ".join(top["reasons"])

        lower = result["candidates"][1]
        assert "lower task-preference contribution" in " ".join(
            lower["reasons"]
        )


class TestCrossProviderExplanation:
    def test_both_cross_provider_candidates_explained(self):
        store = HealthStore()
        store.save(make_report("A", DEGRADED, degraded=("m1",)))
        store.save(make_report("B", HEALTHY, healthy=("m1",)))
        p_a = make_provider("A", ["m1"], priority=10)
        p_b = make_provider("B", ["m1"], priority=1)

        ranked = build(
            store,
            [p_a, p_b],
            task="coding",
            task_coding=["m1"],
            cross_provider=True,
        )
        result = ExplanationService().explain(ranked, health_aware=True)

        assert [(c["provider"], c["model"]) for c in result["candidates"]] == [
            ("B", "m1"),
            ("A", "m1"),
        ]
        assert result["selected"] == {"provider": "B", "model": "m1"}
        assert [c["rank"] for c in result["candidates"]] == [1, 2]


class TestExplanationEdgeCases:
    def test_empty_ranking(self):
        result = ExplanationService().explain([], health_aware=True)

        assert result["selected"] is None
        assert result["candidates"] == []
        assert result["generated_at"]

    def test_health_aware_disabled_note(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        ranked = build(store, [make_provider("A", ["a-1"])], health_aware=False)

        result = ExplanationService().explain(ranked, health_aware=False)

        joined = " ".join(result["candidates"][0]["reasons"])
        assert "Health-aware routing disabled" in joined

    def test_ranked_candidates_matches_build_order(self):
        store = HealthStore()
        store.save(make_report("A", HEALTHY, healthy=("a-1",)))
        store.save(make_report("B", DEGRADED, degraded=("b-1",)))
        p_a = make_provider("A", ["a-1"], priority=10)
        p_b = make_provider("B", ["b-1"], priority=1)
        builder = build_builder(store=store)

        build_order = [
            (p.name, m) for p, m in builder.build([p_a, p_b])
        ]
        ranked_order = [
            (rc.provider, rc.model)
            for rc in builder.ranked_candidates([p_a, p_b])
        ]

        assert build_order == ranked_order == [("A", "a-1"), ("B", "b-1")]
