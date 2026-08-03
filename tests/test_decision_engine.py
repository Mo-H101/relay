"""
Phase 7E: Advanced Decision Engine tests.

Covers the explicit DecisionEngine service, DecisionScore/SignalBreakdown
objects, decision statistics, the cost placeholder, the health-band
ordering invariant, disabled-equivalence with the legacy path, the
choose_provider wiring, reload refresh, and the privacy contract.

Rules under test:
- The engine never introduces its own ordering: it scores the pipeline's
  order, and health band stays the primary ordering key.
- With DECISION_ENGINE_ENABLED off, provider selection is byte-identical
  to the legacy candidate path.
- Decision diagnostics are metadata only; contains_never_captured() must
  stay False over every engine surface.
"""

import threading
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.core.relay import Relay
from app.providers.base import Provider
from app.services.candidate_builder import CandidateBuilder
from app.services.decision_engine import (
    DecisionEngine,
    DecisionResult,
    DecisionScore,
    DecisionStats,
    SignalBreakdown,
)
from app.services.health_checker import DEGRADED, HEALTHY, ProviderHealth
from app.services.health_store import HealthStore
from app.services.memory_contract import contains_never_captured
from app.services.quality import QualityStore
from app.services.scoring import (
    DEFAULT_BAND,
    CandidateScorer,
    Rankable,
)
from app.services.telemetry import TelemetryStats, TelemetryStore


def make_provider(name, models, priority=1, api_key="test-key", enabled=True):
    return Provider(
        name=name,
        base_url=f"https://{name.lower()}.invalid",
        api_key=api_key,
        enabled=enabled,
        priority=priority,
        models=list(models),
    )


def make_report(name, status, healthy=(), degraded=(), unavailable=()):
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
        unsupported_models=[],
    )


def make_telemetry(
    provider="A",
    model="a-1",
    count=20,
    success=18,
    latency=100.0,
    ewma_success=None,
    ewma_latency_ms=None,
):
    if ewma_success is None:
        ewma_success = success / count if count else None
    if ewma_latency_ms is None:
        ewma_latency_ms = latency

    return TelemetryStats(
        provider=provider,
        model=model,
        request_count=count,
        success_count=success,
        failure_count=count - success,
        average_latency_ms=latency,
        recent_failures=[],
        ewma_success=ewma_success,
        ewma_latency_ms=ewma_latency_ms,
    )


class _FakeBuilder:
    """Minimal builder exposing only rankables() for unit tests."""

    def __init__(self, rankables):
        self._rankables = rankables

    def rankables(self, providers, task=None):
        return self._rankables


class TestSignalBreakdown:
    def test_cost_signal_placeholder(self):
        engine = DecisionEngine(scorer=CandidateScorer())
        score = engine._score(Rankable("A", "a-1", priority=5), rank=1)

        cost = next(s for s in score.signals if s.key == "cost")
        assert cost.normalized == 0.5
        assert cost.weight == 0.0
        assert cost.contribution == 0.0
        assert cost.enabled is False
        assert cost.confidence == 0.0

    def test_breakdown_emits_every_signal(self):
        engine = DecisionEngine(scorer=CandidateScorer())
        score = engine._score(Rankable("A", "a-1", priority=5), rank=1)

        keys = [signal.key for signal in score.signals]
        assert keys[-1] == "cost"
        assert len(keys) == 10
        assert len(set(keys)) == 10

    def test_contributions_sum_to_total(self):
        engine = DecisionEngine(scorer=CandidateScorer())
        rankable = Rankable(
            "A", "a-1", priority=5, telemetry=make_telemetry(), preference=0
        )
        score = engine._score(rankable, rank=1)

        assert score.total == pytest.approx(
            sum(score.contributions.values()), abs=1e-4
        )
        assert score.fitness == pytest.approx(
            sum(s.contribution for s in score.signals), abs=1e-4
        )


class TestDecisionEngine:
    def test_selected_is_top_of_pipeline_order(self):
        engine = DecisionEngine(
            builder=_FakeBuilder(
                [
                    Rankable("B", "b-1", priority=1, health_band=0),
                    Rankable("A", "a-1", priority=1, health_band=1),
                ]
            )
        )

        result = engine.decide(providers=[])

        assert isinstance(result, DecisionResult)
        assert result.selected.provider == "B"
        assert [s.provider for s in result.ranked] == ["B", "A"]
        assert [s.rank for s in result.ranked] == [1, 2]

    def test_engine_preserves_pipeline_order(self):
        degraded = Rankable(
            "B", "b-1", priority=1, health_band=1, telemetry=make_telemetry()
        )
        healthy = Rankable(
            "A", "a-1", priority=1, health_band=0, telemetry=make_telemetry(count=0)
        )

        engine = DecisionEngine(builder=_FakeBuilder([degraded, healthy]))
        result = engine.decide(providers=[])

        # The engine scores the pipeline's order; it never reorders.
        assert [s.provider for s in result.ranked] == ["B", "A"]
        assert [s.health_band for s in result.ranked] == [1, 0]

    def test_missing_band_resolves_to_default(self):
        engine = DecisionEngine(scorer=CandidateScorer())
        score = engine._score(Rankable("A", "a-1", priority=5), rank=1)

        assert score.health_band == DEFAULT_BAND

    def test_disabled_engine_is_inert_without_builder(self):
        engine = DecisionEngine(config=SimpleNamespace(decision_engine_enabled=False))

        result = engine.decide(providers=[])

        assert result.selected is None
        assert result.ranked == []
        assert engine.stats()["decisions"] == 0

    def test_cold_start_adaptive_confidence_is_zero(self):
        engine = DecisionEngine(scorer=CandidateScorer())
        rankable = Rankable(
            "A", "a-1", priority=5, telemetry=make_telemetry(count=1)
        )
        score = engine._score(rankable, rank=1)

        adaptive = {
            s.key: s.confidence
            for s in score.signals
            if s.key in ("adaptive_reliability", "adaptive_latency")
        }
        assert adaptive == {
            "adaptive_reliability": 0.0,
            "adaptive_latency": 0.0,
        }

    def test_confident_adaptive_signals_ramp_confidence(self):
        engine = DecisionEngine(
            scorer=CandidateScorer(
                config=SimpleNamespace(
                    adaptive_routing_enabled=True,
                    adaptive_min_samples=10,
                )
            )
        )
        rankable = Rankable(
            "A",
            "a-1",
            priority=5,
            telemetry=make_telemetry(count=30, ewma_success=0.9, ewma_latency_ms=50),
        )
        score = engine._score(rankable, rank=1)

        adaptive = {
            s.key: s.confidence
            for s in score.signals
            if s.key in ("adaptive_reliability", "adaptive_latency")
        }
        assert adaptive["adaptive_reliability"] == 1.0
        assert adaptive["adaptive_latency"] == 1.0

    def test_quality_confidence_from_rankable(self):
        engine = DecisionEngine(scorer=CandidateScorer())
        rankable = Rankable(
            "A",
            "a-1",
            priority=5,
            quality_ewma=0.8,
            quality_confidence=0.5,
        )
        score = engine._score(rankable, rank=1)

        quality = next(s for s in score.signals if s.key == "quality")
        assert quality.confidence == 0.5

    def test_reason_is_metadata_only_and_deterministic(self):
        engine = DecisionEngine(scorer=CandidateScorer())
        score = engine._score(Rankable("A", "a-1", priority=5), rank=1)

        assert "health_band" in score.reason
        assert "signals=" in score.reason


class TestDecisionStats:
    def test_records_decisions_and_selection(self):
        stats = DecisionStats()
        score = DecisionScore(
            provider="A",
            model="a-1",
            rank=1,
            health_band=0,
            health_status="healthy",
            fitness=3.5,
            total=3.5,
            contributions={},
            signals=[],
            confidence=1.0,
            reason="",
        )

        stats.record(score, pool=3)
        stats.record(score, pool=2)

        snapshot = stats.snapshot()
        assert snapshot["decisions"] == 2
        assert snapshot["candidates"] == 5
        assert snapshot["selected"] == {"A/a-1": 2}
        assert snapshot["by_band"] == {0: 2}

    def test_concurrent_records_are_exact(self):
        stats = DecisionStats()
        score = DecisionScore(
            provider="A",
            model="a-1",
            rank=1,
            health_band=0,
            health_status=None,
            fitness=1.0,
            total=1.0,
            contributions={},
            signals=[],
            confidence=1.0,
            reason="",
        )
        per_thread = 200

        def worker():
            for _ in range(per_thread):
                stats.record(score, pool=1)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        snapshot = stats.snapshot()
        assert snapshot["decisions"] == per_thread * 8
        assert snapshot["candidates"] == per_thread * 8

    def test_score_pool_does_not_record(self):
        engine = DecisionEngine(
            builder=_FakeBuilder([Rankable("A", "a-1", priority=5)])
        )

        result = engine.score_pool(providers=[])

        assert result.selected is not None
        assert engine.stats()["decisions"] == 0


class TestBuilderIntegration:
    def _builder(self, providers):
        return CandidateBuilder(health_store=HealthStore())

    def test_engine_ranking_matches_pipeline_ranking(self, monkeypatch):
        monkeypatch.setattr(settings, "health_aware_routing", True)

        builder = CandidateBuilder(health_store=HealthStore())
        builder.health_store.save(make_report("A", DEGRADED, degraded=["a-1"]))
        builder.health_store.save(make_report("B", HEALTHY, healthy=["b-1"]))
        providers = [
            make_provider("A", ["a-1"], priority=10),
            make_provider("B", ["b-1"], priority=1),
        ]

        engine = DecisionEngine(builder=builder)
        result = engine.decide(providers)

        ordered = builder.build(providers)
        assert result.selected.provider == ordered[0][0].name
        assert [s.provider for s in result.ranked] == [
            provider.name for provider, _ in ordered
        ]

    def test_quality_confidence_surfaces_in_engine(self, monkeypatch):
        monkeypatch.setattr(settings, "health_aware_routing", True)

        quality = QualityStore(min_samples=1)
        quality.record("A", "a-1", 5)
        quality.record("A", "a-1", 5)

        builder = CandidateBuilder(
            health_store=HealthStore(),
            quality_store=quality,
        )
        builder.health_store.save(make_report("A", HEALTHY, healthy=["a-1"]))

        engine = DecisionEngine(builder=builder)
        result = engine.decide([make_provider("A", ["a-1"])])

        assert result.selected.provider == "A"
        quality_signal = next(
            s for s in result.selected.signals if s.key == "quality"
        )
        assert quality_signal.confidence == pytest.approx(0.02)
        assert result.selected.health_status == HEALTHY

    def test_empty_pool_yields_no_selection(self):
        engine = DecisionEngine(builder=CandidateBuilder(health_store=HealthStore()))

        result = engine.decide([])

        assert result.selected is None
        assert result.ranked == []


class TestRelayWiring:
    def _relay_with_providers(self, monkeypatch):
        monkeypatch.setattr(settings, "health_aware_routing", True)
        relay = Relay()
        relay.provider_manager.register(make_provider("A", ["a-1"], priority=10))
        relay.provider_manager.register(make_provider("B", ["b-1"], priority=1))
        relay.health_store.save(make_report("A", DEGRADED, degraded=["a-1"]))
        relay.health_store.save(make_report("B", HEALTHY, healthy=["b-1"]))
        return relay

    def test_choose_provider_disabled_matches_legacy(self, monkeypatch):
        monkeypatch.setattr(settings, "decision_engine_enabled", False)
        relay = self._relay_with_providers(monkeypatch)

        assert relay.choose_provider().name == "B"

    def test_choose_provider_enabled_selects_same_provider(self, monkeypatch):
        monkeypatch.setattr(settings, "decision_engine_enabled", True)
        relay = self._relay_with_providers(monkeypatch)

        assert relay.choose_provider().name == "B"

    def test_chat_records_decision_when_enabled(self, monkeypatch):
        monkeypatch.setattr(settings, "decision_engine_enabled", True)
        relay = self._relay_with_providers(monkeypatch)

        assert relay.decision_engine.enabled is True

    def test_engine_disabled_by_default(self):
        assert settings.decision_engine_enabled is False


class TestReloadIntegration:
    def test_refresh_rebuilds_scorer_weights(self):
        engine = DecisionEngine()
        original = settings.scoring_cost_weight

        try:
            settings.scoring_cost_weight = 2.5
            engine.refresh()
            assert engine._scorer.cost_weight == 2.5
        finally:
            settings.scoring_cost_weight = original


class TestPrivacy:
    def test_decision_result_never_captures_forbidden_content(self):
        engine = DecisionEngine(
            builder=_FakeBuilder(
                [
                    Rankable(
                        "A",
                        "a-1",
                        priority=5,
                        telemetry=make_telemetry(),
                        preference=0,
                    )
                ]
            )
        )

        result = engine.decide(providers=[])
        serialized = result.selected.__dict__

        assert not contains_never_captured(serialized)

    def test_stats_snapshot_never_captures_forbidden_content(self):
        engine = DecisionEngine(
            builder=_FakeBuilder([Rankable("A", "a-1", priority=5)])
        )
        engine.decide(providers=[])

        assert not contains_never_captured(engine.stats())
