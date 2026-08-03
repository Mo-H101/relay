"""
Phase 7D: Quality feedback loop tests.

Covers the metadata-only QualityStore, the auth-gated POST /feedback
endpoint with strict schema rejection, the within-band quality scoring
signal (gated by the feature flag and min-sample confidence, never
crossing health bands), diagnostics aggregation, and the routing
integration.

Rules under test:
- Never store prompts/responses/generated content/identity; the feedback
  API rejects any payload carrying prompt/message/response/content.
- Quality feedback only reorders inside the same health band; the band
  order HEALTHY > DEGRADED > NOT_CHECKED > UNAVAILABLE stays primary.
- When quality feedback is disabled, routing is byte-identical to the
  legacy formula (quality contributes exactly zero).
"""

import json
from types import SimpleNamespace
import threading

import pytest

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import settings
from app.core.relay import Relay
from app.main import app as fastapi_app
from app.models.feedback import FeedbackCategory, FeedbackRequest
from app.providers.base import Provider
from app.services.candidate_builder import CandidateBuilder
from app.services.health_checker import DEGRADED, HEALTHY, ProviderHealth
from app.services.health_store import HealthStore
from app.services.memory_contract import contains_never_captured
from app.services.quality import QualityStore
from app.services.scoring import CandidateScorer, Rankable
from app.services.telemetry import TelemetryStore

import app.api.diagnostics
import app.api.feedback


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


class TestQualityStore:
    def test_record_and_aggregate(self):
        store = QualityStore(min_samples=1)
        store.record("A", "a-1", 5)
        store.record("A", "a-1", 4)

        agg = store.aggregate("A", "a-1")
        assert agg["sample_count"] == 2
        assert agg["positive_count"] == 2
        assert agg["negative_count"] == 0
        assert agg["neutral_count"] == 0
        assert agg["positive_rate"] == 1.0
        assert agg["confidence"] == 1.0

    def test_positive_negative_neutral_counts(self):
        store = QualityStore()
        store.record("A", "a-1", 5)
        store.record("A", "a-1", 4)
        store.record("A", "a-1", 3)
        store.record("A", "a-1", 2)
        store.record("A", "a-1", 1)

        agg = store.aggregate("A", "a-1")
        assert agg["sample_count"] == 5
        assert agg["positive_count"] == 2
        assert agg["negative_count"] == 2
        assert agg["neutral_count"] == 1
        assert agg["positive_rate"] == round(2 / 5, 4)

    def test_ratings_clamped_to_range(self):
        store = QualityStore(min_samples=1)
        store.record("A", "a-1", 0)
        store.record("A", "a-1", 9)

        agg = store.aggregate("A", "a-1")
        assert agg["sample_count"] == 2
        assert agg["negative_count"] == 1
        assert agg["positive_count"] == 1

    def test_signal_none_without_feedback(self):
        store = QualityStore()
        assert store.quality_signal("A", "a-1") is None

    def test_signal_gated_by_min_samples(self):
        store = QualityStore(min_samples=3)
        store.record("A", "a-1", 5)
        store.record("A", "a-1", 5)

        signal = store.quality_signal("A", "a-1")
        assert signal is not None
        assert signal.score is None
        assert signal.confidence == round(2 / 3, 4)

        store.record("A", "a-1", 5)

        signal = store.quality_signal("A", "a-1")
        assert signal.score == 1.0
        assert signal.confidence == 1.0

    def test_confidence_ramps_with_samples(self):
        store = QualityStore(min_samples=4)
        expected = [0.25, 0.5, 0.75, 1.0]

        for index, value in enumerate(expected, start=1):
            store.record("A", "a-1", 5)
            signal = store.quality_signal("A", "a-1")
            assert signal.confidence == value

    def test_ewma_tracks_ratings(self):
        store = QualityStore(min_samples=1, learning_rate=0.5)
        store.record("A", "a-1", 5)
        assert store.quality_signal("A", "a-1").score == 1.0

        store.record("A", "a-1", 1)
        assert store.quality_signal("A", "a-1").score == pytest.approx(0.5)

    def test_learning_rate_is_capped(self):
        store = QualityStore(min_samples=1, learning_rate=0.5)
        store.set_alpha(5.0)
        store.record("A", "a-1", 5)
        store.record("A", "a-1", 1)

        assert store.quality_signal("A", "a-1").score == pytest.approx(0.0)

    def test_duplicate_correlation_id_is_ignored(self):
        store = QualityStore()

        assert store.record("A", "a-1", 5, correlation_id="cid-1") is True
        assert store.record("A", "a-1", 4, correlation_id="cid-1") is False

        assert store.aggregate("A", "a-1")["sample_count"] == 1

    def test_different_correlation_ids_count_separately(self):
        store = QualityStore()

        store.record("A", "a-1", 5, correlation_id="cid-1")
        store.record("A", "a-1", 4, correlation_id="cid-2")

        assert store.aggregate("A", "a-1")["sample_count"] == 2

    def test_retention_limit_evicts_least_recent(self):
        store = QualityStore(retention_limit=2)
        store.record("A", "a-1", 5)
        store.record("B", "b-1", 5)
        store.record("C", "c-1", 5)

        keys = {(agg["provider"], agg["model"]) for agg in store.aggregates()}
        assert keys == {("B", "b-1"), ("C", "c-1")}

    def test_retention_limit_refreshed_and_evicts(self):
        store = QualityStore(retention_limit=5)
        store.record("A", "a-1", 5)
        store.record("B", "b-1", 5)
        store.record("C", "c-1", 5)

        store.set_retention_limit(2)

        keys = {(agg["provider"], agg["model"]) for agg in store.aggregates()}
        assert keys == {("B", "b-1"), ("C", "c-1")}

    def test_categories_tallied(self):
        store = QualityStore()
        store.record("A", "a-1", 5, category="speed")
        store.record("A", "a-1", 4, category="accuracy")
        store.record("A", "a-1", 3, category="speed")

        assert store.aggregate("A", "a-1")["categories"] == {
            "speed": 2,
            "accuracy": 1,
        }

    def test_stats_summary(self):
        store = QualityStore(min_samples=2)
        store.record("A", "a-1", 5)
        store.record("A", "a-1", 5)
        store.record("B", "b-1", 5)

        stats = store.stats()
        assert stats["pairs"] == 2
        assert stats["total_ratings"] == 3
        assert stats["confident_pairs"] == 1

    def test_clear_removes_everything(self):
        store = QualityStore()
        store.record("A", "a-1", 5, correlation_id="cid-1")
        store.clear()

        assert store.aggregate("A", "a-1") is None
        assert store.stats()["total_ratings"] == 0
        assert store.record("A", "a-1", 5, correlation_id="cid-1") is True

    def test_store_output_is_metadata_only(self):
        store = QualityStore()
        store.record(
            "A",
            "a-1",
            5,
            category="speed",
            correlation_id="cid-1",
        )

        assert not contains_never_captured(store.aggregates())
        assert not contains_never_captured(store.aggregate("A", "a-1"))
        assert not contains_never_captured(store.stats())
        assert not contains_never_captured(store.quality_signal("A", "a-1"))

    def test_thread_safe_records(self):
        store = QualityStore()
        errors = []

        def worker():
            try:
                for _ in range(100):
                    store.record("A", "a-1", 5)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert store.aggregate("A", "a-1")["sample_count"] == 400


class TestFeedbackModel:
    def test_valid_payload(self):
        request = FeedbackRequest(
            provider="A",
            model="a-1",
            rating=4,
            category=FeedbackCategory.ACCURACY,
            correlation_id="cid-1",
        )

        assert request.rating == 4
        assert request.category is FeedbackCategory.ACCURACY

    def test_rating_bounds(self):
        with pytest.raises(ValidationError):
            FeedbackRequest(provider="A", model="a-1", rating=0)
        with pytest.raises(ValidationError):
            FeedbackRequest(provider="A", model="a-1", rating=6)

    @pytest.mark.parametrize(
        "field",
        [
            "prompt",
            "prompts",
            "message",
            "messages",
            "response",
            "responses",
            "content",
        ],
    )
    def test_content_fields_rejected(self, field):
        with pytest.raises(ValidationError):
            FeedbackRequest(
                provider="A",
                model="a-1",
                rating=4,
                **{field: "anything"},
            )

    def test_unknown_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            FeedbackRequest(provider="A", model="a-1", rating=4, note="hi")

    def test_invalid_category_rejected(self):
        with pytest.raises(ValidationError):
            FeedbackRequest(
                provider="A",
                model="a-1",
                rating=4,
                category="not-a-category",
            )


@pytest.fixture
def wired_relay(monkeypatch):
    """
    Build a fresh Relay and wire it into the feedback and diagnostics
    routers in place of the module-level singleton.
    """
    relays = {}

    def _build():
        relay = Relay()
        monkeypatch.setattr(app.api.feedback, "relay", relay)
        monkeypatch.setattr(app.api.diagnostics, "relay", relay)
        relays[id(relay)] = relay
        return relay

    yield _build


@pytest.fixture
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


def _feedback_payload(**overrides):
    payload = {"provider": "A", "model": "a-1", "rating": 4}
    payload.update(overrides)
    return payload


class TestFeedbackEndpoint:
    def test_submit_records_feedback(self, wired_relay, client):
        relay = wired_relay()

        response = client.post("/feedback", json=_feedback_payload())

        assert response.status_code == 202
        assert response.json()["stored"] is True

        agg = relay.quality_store.aggregate("A", "a-1")
        assert agg["sample_count"] == 1
        assert agg["positive_count"] == 1

    def test_submit_requires_auth(self, wired_relay, client, monkeypatch):
        monkeypatch.setattr(settings, "relay_api_key", "test-secret")
        wired_relay()

        denied = client.post("/feedback", json=_feedback_payload())
        assert denied.status_code == 401

        allowed = client.post(
            "/feedback",
            json=_feedback_payload(),
            headers={"X-Relay-API-Key": "test-secret"},
        )
        assert allowed.status_code == 202

    @pytest.mark.parametrize(
        "field",
        ["prompt", "prompts", "message", "messages", "response", "responses", "content"],
    )
    def test_rejects_content_fields(self, wired_relay, client, field):
        wired_relay()

        response = client.post(
            "/feedback",
            json=_feedback_payload(**{field: "secret-content"}),
        )

        assert response.status_code == 422

    def test_rejects_out_of_range_rating(self, wired_relay, client):
        wired_relay()

        assert (
            client.post("/feedback", json=_feedback_payload(rating=0)).status_code
            == 422
        )
        assert (
            client.post("/feedback", json=_feedback_payload(rating=6)).status_code
            == 422
        )

    def test_duplicate_correlation_id_not_counted_twice(
        self, wired_relay, client
    ):
        relay = wired_relay()
        payload = _feedback_payload(correlation_id="cid-9")

        first = client.post("/feedback", json=payload)
        second = client.post("/feedback", json=payload)

        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["stored"] is True
        assert second.json()["stored"] is False
        assert relay.quality_store.aggregate("A", "a-1")["sample_count"] == 1

    def test_unknown_pair_accepted(self, wired_relay, client):
        relay = wired_relay()

        response = client.post(
            "/feedback",
            json=_feedback_payload(provider="Ghost", model="ghost-1"),
        )

        assert response.status_code == 202
        assert relay.quality_store.aggregate("Ghost", "ghost-1")["sample_count"] == 1

    def test_response_is_metadata_only(self, wired_relay, client):
        wired_relay()

        response = client.post("/feedback", json=_feedback_payload())

        assert not contains_never_captured(response.json())


class TestFeedbackPairValidation:
    """
    Phase 7D audit hardening: /feedback rejects a pair only when it can be
    positively confirmed as unknown. Unknown providers and unverifiable
    states are accepted, so metadata recording never rejects legitimate
    feedback.
    """

    def test_known_provider_known_model_accepted(self, wired_relay, client):
        relay = wired_relay()
        relay.provider_manager.register(make_provider("A", ["a-1"]))

        response = client.post("/feedback", json=_feedback_payload())

        assert response.status_code == 202
        assert relay.quality_store.aggregate("A", "a-1")["sample_count"] == 1

    def test_known_provider_unknown_model_rejected(self, wired_relay, client):
        relay = wired_relay()
        relay.provider_manager.register(make_provider("A", ["a-1"]))

        response = client.post(
            "/feedback",
            json=_feedback_payload(provider="A", model="ghost-model"),
        )

        assert response.status_code == 400
        assert relay.quality_store.aggregate("A", "ghost-model") is None

    def test_unknown_provider_accepted(self, wired_relay, client):
        relay = wired_relay()
        relay.provider_manager.register(make_provider("A", ["a-1"]))

        response = client.post(
            "/feedback",
            json=_feedback_payload(provider="Ghost", model="ghost-1"),
        )

        assert response.status_code == 202
        assert relay.quality_store.aggregate("Ghost", "ghost-1")["sample_count"] == 1


class TestScoringIntegration:
    def test_quality_contributes_zero_when_disabled(self):
        scorer = CandidateScorer()

        breakdown = scorer.breakdown(
            Rankable("A", "a-1", priority=5, quality_ewma=1.0)
        )

        assert breakdown["quality"] == 0.0
        assert scorer.fitness(5, quality_ewma=1.0) == scorer.fitness(
            5,
            quality_ewma=0.0,
        )

    def test_quality_score_normalized_when_enabled(self):
        scorer = CandidateScorer(
            config=SimpleNamespace(
                quality_feedback_enabled=True,
                quality_feedback_weight=1.0,
            )
        )

        breakdown = scorer.breakdown(
            Rankable("A", "a-1", priority=5, quality_ewma=0.8)
        )

        assert breakdown["quality"] == pytest.approx(0.8)
        assert scorer.fitness(5, quality_ewma=1.0) > scorer.fitness(
            5,
            quality_ewma=0.0,
        )

    def test_quality_gated_by_weight(self):
        scorer = CandidateScorer(
            config=SimpleNamespace(
                quality_feedback_enabled=True,
                quality_feedback_weight=0.0,
            )
        )

        breakdown = scorer.breakdown(
            Rankable("A", "a-1", priority=5, quality_ewma=1.0)
        )

        assert breakdown["quality"] == 0.0

    def test_quality_reorders_within_band_only(self):
        scorer = CandidateScorer(
            config=SimpleNamespace(
                quality_feedback_enabled=True,
                quality_feedback_weight=1.0,
            )
        )

        low_quality_healthy = Rankable(
            "A", "a-1", priority=1, health_band=0, quality_ewma=0.0
        )
        high_quality_degraded = Rankable(
            "B", "b-1", priority=1, health_band=1, quality_ewma=1.0
        )
        high_quality_healthy = Rankable(
            "C", "c-1", priority=1, health_band=0, quality_ewma=1.0
        )

        ranked = scorer.rank(
            [low_quality_healthy, high_quality_degraded, high_quality_healthy]
        )

        assert [item.provider for item in ranked] == ["C", "A", "B"]

    def test_quality_never_moves_unhealthy_above_healthy(self):
        scorer = CandidateScorer(
            config=SimpleNamespace(
                quality_feedback_enabled=True,
                quality_feedback_weight=10.0,
            )
        )

        best_quality_unavailable = Rankable(
            "A", "a-1", priority=1, health_band=3, quality_ewma=1.0
        )
        worst_quality_healthy = Rankable(
            "B", "b-1", priority=1, health_band=0, quality_ewma=0.0
        )

        ranked = scorer.rank(
            [best_quality_unavailable, worst_quality_healthy]
        )

        assert [item.provider for item in ranked] == ["B", "A"]


class TestBuilderIntegration:
    def test_quality_reorders_within_band(self, monkeypatch):
        monkeypatch.setattr(settings, "health_aware_routing", True)
        monkeypatch.setattr(settings, "quality_feedback_enabled", True)

        quality = QualityStore(min_samples=1)
        quality.record("A", "a-1", 1)
        quality.record("B", "b-1", 5)

        builder = CandidateBuilder(
            health_store=HealthStore(),
            quality_store=quality,
        )
        builder.health_store.save(
            make_report("A", HEALTHY, healthy=["a-1"])
        )
        builder.health_store.save(
            make_report("B", HEALTHY, healthy=["b-1"])
        )

        candidates = builder.build(
            [
                make_provider("A", ["a-1"]),
                make_provider("B", ["b-1"]),
            ]
        )

        assert [(provider.name, model) for provider, model in candidates] == [
            ("B", "b-1"),
            ("A", "a-1"),
        ]

    def test_quality_never_crosses_health_band(self, monkeypatch):
        monkeypatch.setattr(settings, "health_aware_routing", True)
        monkeypatch.setattr(settings, "quality_feedback_enabled", True)

        quality = QualityStore(min_samples=1)
        quality.record("A", "a-1", 5)
        quality.record("B", "b-1", 1)

        builder = CandidateBuilder(
            health_store=HealthStore(),
            quality_store=quality,
        )
        builder.health_store.save(
            make_report("A", DEGRADED, degraded=["a-1"])
        )
        builder.health_store.save(
            make_report("B", HEALTHY, healthy=["b-1"])
        )

        candidates = builder.build(
            [
                make_provider("A", ["a-1"]),
                make_provider("B", ["b-1"]),
            ]
        )

        assert [(provider.name, model) for provider, model in candidates] == [
            ("B", "b-1"),
            ("A", "a-1"),
        ]

    def test_below_min_samples_is_neutral(self, monkeypatch):
        monkeypatch.setattr(settings, "health_aware_routing", True)
        monkeypatch.setattr(settings, "quality_feedback_enabled", True)

        quality = QualityStore(min_samples=5)
        quality.record("A", "a-1", 5)
        quality.record("B", "b-1", 1)

        builder = CandidateBuilder(
            health_store=HealthStore(),
            quality_store=quality,
        )
        builder.health_store.save(
            make_report("A", HEALTHY, healthy=["a-1"])
        )
        builder.health_store.save(
            make_report("B", HEALTHY, healthy=["b-1"])
        )

        candidates = builder.build(
            [
                make_provider("A", ["a-1"]),
                make_provider("B", ["b-1"]),
            ]
        )

        assert [(provider.name, model) for provider, model in candidates] == [
            ("A", "a-1"),
            ("B", "b-1"),
        ]

    def test_disabled_quality_keeps_input_order(self, monkeypatch):
        monkeypatch.setattr(settings, "health_aware_routing", True)

        quality = QualityStore(min_samples=1)
        quality.record("A", "a-1", 5)
        quality.record("B", "b-1", 1)

        builder = CandidateBuilder(
            health_store=HealthStore(),
            quality_store=quality,
        )
        builder.health_store.save(
            make_report("A", HEALTHY, healthy=["a-1"])
        )
        builder.health_store.save(
            make_report("B", HEALTHY, healthy=["b-1"])
        )

        candidates = builder.build(
            [
                make_provider("A", ["a-1"]),
                make_provider("B", ["b-1"]),
            ]
        )

        assert [(provider.name, model) for provider, model in candidates] == [
            ("A", "a-1"),
            ("B", "b-1"),
        ]


class TestQualityDiagnostics:
    def test_quality_section_shape(self, wired_relay, client, monkeypatch):
        monkeypatch.setattr(settings, "quality_feedback_enabled", True)
        relay = wired_relay()
        relay.quality_store.record("A", "a-1", 5)
        relay.quality_store.record("A", "a-1", 5)
        relay.quality_store.record("B", "b-1", 2)

        response = client.get("/diagnostics")

        assert response.status_code == 200
        quality = response.json()["quality"]
        assert quality["enabled"] is True
        assert quality["config"]["min_samples"] == (
            settings.quality_feedback_min_samples
        )
        assert quality["config"]["learning_rate"] == (
            settings.quality_feedback_learning_rate
        )
        assert quality["config"]["retention_limit"] == (
            settings.quality_feedback_retention_limit
        )
        assert quality["summary"]["total_ratings"] == 3
        assert quality["summary"]["pairs"] == 2
        assert quality["summary"]["confident_pairs"] == 0

        pairs = {
            (entry["provider"], entry["model"]) for entry in quality["pairs"]
        }
        assert pairs == {("A", "a-1"), ("B", "b-1")}

    def test_quality_section_never_captures_content(
        self, wired_relay, client, monkeypatch
    ):
        monkeypatch.setattr(settings, "quality_feedback_enabled", True)
        relay = wired_relay()
        relay.quality_store.record(
            "A",
            "a-1",
            5,
            category="speed",
            correlation_id="cid-1",
        )

        response = client.get("/diagnostics")

        assert not contains_never_captured(response.json())


class TestRelayWiring:
    def test_relay_owns_quality_store(self):
        relay = Relay()

        assert relay.quality_store is not None
        assert relay.candidate_builder.quality_store is relay.quality_store

    def test_quality_store_uses_settings_defaults(self):
        relay = Relay()
        stats = relay.quality_store.stats()

        assert stats["min_samples"] == settings.quality_feedback_min_samples
        assert stats["learning_rate"] == pytest.approx(
            settings.quality_feedback_learning_rate
        )
        assert stats["retention_limit"] == settings.quality_feedback_retention_limit
