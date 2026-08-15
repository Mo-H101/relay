"""
Tests for the diagnostics/observability layer (Phase 5B).
"""

import json

import pytest

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.relay import Relay
from app.main import app as fastapi_app
from app.providers.base import Provider
from app.services.diagnostics import DiagnosticsService
from app.services.health_checker import DEGRADED, HEALTHY, ProviderHealth
from app.services.state_store import StateStore

import app.api.diagnostics


def make_provider(
    name,
    models,
    priority=1,
    api_key="test-key",
    enabled=True,
):
    return Provider(
        name=name,
        base_url=f"https://{name.lower()}.invalid",
        api_key=api_key,
        enabled=enabled,
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


@pytest.fixture
def wired_relay(monkeypatch):
    relays = {}

    def _build():
        relay = Relay()
        monkeypatch.setattr(app.api.diagnostics, "relay", relay)
        relays[id(relay)] = relay
        return relay

    yield _build

    for relay in relays.values():
        monkeypatch.setattr(app.api.diagnostics, "relay", relay)


@pytest.fixture
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


def _no_api_key_keys(payload):
    if isinstance(payload, dict):
        assert "api_key" not in payload
        for value in payload.values():
            _no_api_key_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            _no_api_key_keys(item)


class TestDiagnosticsEndpoint:
    def test_endpoint_returns_full_shape(self, wired_relay, client):
        provider = make_provider("A", ["a-1"], priority=10)
        relay = wired_relay()
        relay.provider_manager.register(provider)

        response = client.get("/diagnostics")

        assert response.status_code == 200
        payload = response.json()
        assert set(payload.keys()) == {
            "generated_at",
            "providers",
            "learned_health",
            "telemetry",
            "operations",
            "scoring",
            "adaptive",
            "quality",
            "actual_decisions",
            "persistence",
        }
        assert payload["generated_at"]

    def test_endpoint_accepts_task_query(self, wired_relay, client):
        provider = make_provider("A", ["a-1"], priority=10)
        relay = wired_relay()
        relay.provider_manager.register(provider)

        response = client.get("/diagnostics", params={"task": "coding"})

        assert response.status_code == 200
        assert response.json()["scoring"]["decision"]["task"] == "coding"


class TestActualDecisionsSection:
    """
    Phase 8C: the diagnostics snapshot surfaces recent actual routing
    decision records. Read-only, bounded, metadata only, and gated by the
    same store the /decision/explain/actual endpoint serves.
    """

    def _record(self, relay, cid, model="a-1"):
        from app.services.decision_record import record_actual_decision

        provider = make_provider("A", ["a-1"])
        record_actual_decision(
            relay.decision_record_store,
            correlation_id=cid,
            requested_model=None,
            routed_task=None,
            routed=True,
            candidates=[(provider, "a-1")],
            provider="A",
            model=model,
            attempts=[
                {"provider": "A", "model": model, "success": True,
                 "latency_ms": 5}
            ],
            outcome="succeeded",
        )

    def test_empty_when_no_records(self, wired_relay, client):
        relay = wired_relay()
        relay.provider_manager.register(make_provider("A", ["a-1"]))

        response = client.get("/diagnostics")

        section = response.json()["actual_decisions"]
        assert section["available"] is True
        assert section["records"] == []

    def test_recent_records_surfaced_metadata_only(self, wired_relay, client):
        relay = wired_relay()
        relay.provider_manager.register(make_provider("A", ["a-1"]))
        self._record(relay, "req-1")
        self._record(relay, "req-2")

        response = client.get("/diagnostics")

        section = response.json()["actual_decisions"]
        assert section["available"] is True
        assert section["limit"] == 50
        assert section["max_records"] == relay.decision_record_store.max_records
        assert [r["correlation_id"] for r in section["records"]] == [
            "req-1",
            "req-2",
        ]
        assert section["records"][-1]["selected_model"] == "a-1"
        assert section["records"][-1]["outcome"] == "succeeded"
        # Metadata only: no prompts, responses, content, or credentials.
        raw = json.dumps(section)
        assert "api_key" not in raw
        assert "sk-secret" not in raw
        assert "prompt" not in raw
        assert "content" not in raw

    def test_records_bounded(self, wired_relay, client):
        relay = wired_relay()
        relay.provider_manager.register(make_provider("A", ["a-1"]))

        for i in range(60):
            self._record(relay, f"req-{i}", model="a-1")

        response = client.get("/diagnostics")

        records = response.json()["actual_decisions"]["records"]
        assert len(records) == 50
        # Most recent 50 are kept; the oldest are evicted from the output.
        assert records[0]["correlation_id"] == "req-10"
        assert records[-1]["correlation_id"] == "req-59"


class TestProvidersSection:
    def test_provider_snapshot_visible(self, wired_relay, client):
        provider = make_provider(
            "A",
            ["meta/llama-3-70b", "nvidia/nim-embedding"],
            priority=10,
        )
        relay = wired_relay()
        relay.provider_manager.register(provider)
        relay.health_store.save(
            make_report(
                "A",
                HEALTHY,
                healthy=("meta/llama-3-70b",),
                unsupported=("nvidia/nim-embedding",),
            )
        )

        response = client.get("/diagnostics")

        entry = response.json()["providers"]["providers"][0]
        assert entry["name"] == "A"
        assert entry["enabled"] is True
        assert entry["priority"] == 10
        assert entry["requires_api_key"] is True
        assert entry["has_api_key"] is True
        assert entry["status"] == HEALTHY
        assert entry["connectivity"] is True
        assert entry["last_checked"] == "now"
        assert entry["healthy_models"] == ["meta/llama-3-70b"]
        assert entry["unsupported_models"] == ["nvidia/nim-embedding"]

    def test_provider_without_snapshot_is_not_checked(self, wired_relay, client):
        provider = make_provider("A", ["a-1"])
        relay = wired_relay()
        relay.provider_manager.register(provider)

        response = client.get("/diagnostics")

        entry = response.json()["providers"]["providers"][0]
        assert entry["status"] == "not_checked"
        assert entry["connectivity"] is None
        assert entry["healthy_models"] == []


class TestLearnedHealth:
    def test_degraded_models_and_summary(self, wired_relay, client):
        provider = make_provider("A", ["a-1", "a-2"])
        relay = wired_relay()
        relay.provider_manager.register(provider)

        relay.health_store.record_failure("A", "a-1", "timeout")
        relay.health_store.record_failure("A", "a-1", "timeout")
        relay.health_store.record_failure("A", "a-2", "timeout")
        relay.health_store.record_failure("A", "a-2", "timeout")

        response = client.get("/diagnostics")

        learned = response.json()["learned_health"]
        assert learned["summary"]["degraded_models"] == 2
        assert learned["summary"]["unavailable_models"] == 0
        assert learned["providers"][0]["provider"] == "A"
        assert set(learned["providers"][0]["degraded_models"]) == {
            "a-1",
            "a-2",
        }

    def test_unavailable_provider_status(self, wired_relay, client):
        provider = make_provider("A", ["a-1"])
        relay = wired_relay()
        relay.provider_manager.register(provider)

        relay.health_store.record_failure("A", "a-1", "auth_error")

        response = client.get("/diagnostics")

        learned = response.json()["learned_health"]
        assert learned["providers"][0]["status"] == "unavailable"
        assert learned["summary"]["unavailable_providers"] == 1


class TestTelemetry:
    def test_entries_and_totals(self, wired_relay, client):
        provider = make_provider("A", ["a-1"])
        relay = wired_relay()
        relay.provider_manager.register(provider)

        relay.telemetry.record_attempt("A", "a-1", True, latency_ms=100)
        relay.telemetry.record_attempt("A", "a-1", True, latency_ms=300)
        relay.telemetry.record_attempt("A", "a-1", False, latency_ms=200)

        response = client.get("/diagnostics")

        telemetry = response.json()["telemetry"]
        assert telemetry["summary"]["total_requests"] == 3
        assert telemetry["summary"]["total_successes"] == 2
        assert telemetry["summary"]["total_failures"] == 1
        assert telemetry["summary"]["success_rate"] == round(2 / 3, 4)

        entry = telemetry["entries"][0]
        assert entry["provider"] == "A"
        assert entry["model"] == "a-1"
        assert entry["request_count"] == 3
        assert entry["average_latency_ms"] == 200.0
        assert entry["recent_failure_count"] == 1
        assert entry["success_rate"] == round(2 / 3, 4)


class TestScoring:
    def test_ranking_and_explanation(self, wired_relay, client, monkeypatch):
        monkeypatch.setattr(settings, "health_aware_routing", True)

        p_a = make_provider("A", ["a-1"], priority=10)
        p_b = make_provider("B", ["b-1"], priority=1)
        relay = wired_relay()
        relay.provider_manager.register(p_a)
        relay.provider_manager.register(p_b)
        relay.health_store.save(
            make_report("A", DEGRADED, degraded=("a-1",))
        )
        relay.health_store.save(
            make_report("B", HEALTHY, healthy=("b-1",))
        )

        response = client.get("/diagnostics")

        scoring = response.json()["scoring"]
        assert set(scoring["weights"].keys()) == {
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
        }
        assert scoring["references"]["latency_ref_ms"] > 0

        ranking = scoring["ranking"]
        assert [c["provider"] for c in ranking] == ["B", "A"]
        assert ranking[0]["rank"] == 1
        assert set(ranking[0]["score_breakdown"].keys()) == {
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

        decision = scoring["decision"]
        assert decision["selected"] == {"provider": "B", "model": "b-1"}
        assert len(decision["candidates"]) == 2

    def test_decision_engine_section_disabled_by_default(
        self, wired_relay, client
    ):
        relay = wired_relay()
        relay.provider_manager.register(make_provider("A", ["a-1"]))

        response = client.get("/diagnostics")

        engine = response.json()["scoring"]["decision_engine"]
        assert engine["enabled"] is False
        assert engine["stats"] == {
            "decisions": 0,
            "candidates": 0,
            "selected": {},
            "by_band": {},
        }
        assert engine["scores"] == []

    def test_decision_engine_section_populated_when_enabled(
        self, wired_relay, client, monkeypatch
    ):
        monkeypatch.setattr(settings, "decision_engine_enabled", True)
        relay = wired_relay()
        relay.provider_manager.register(make_provider("A", ["a-1"]))

        response = client.get("/diagnostics")

        engine = response.json()["scoring"]["decision_engine"]
        assert engine["enabled"] is True
        assert engine["scores"][0]["provider"] == "A"
        assert engine["scores"][0]["health_band"] == 2
        assert engine["scores"][0]["fitness"] == pytest.approx(
            engine["scores"][0]["total"], abs=1e-4
        )
        assert set(
            signal["key"] for signal in engine["scores"][0]["signals"]
        ) == {
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
        }
        assert engine["stats"]["decisions"] == 0

    def test_service_snapshot_matches_endpoint(self, wired_relay, client):
        provider = make_provider("A", ["a-1"])
        relay = wired_relay()
        relay.provider_manager.register(provider)

        service = DiagnosticsService().build_snapshot(relay)
        endpoint = client.get("/diagnostics").json()

        for section in ("providers", "learned_health", "telemetry", "scoring", "adaptive"):
            service_value = dict(service[section])
            endpoint_value = dict(endpoint[section])

            if section == "scoring":
                service_value["decision"].pop("generated_at", None)
                endpoint_value["decision"].pop("generated_at", None)

            assert endpoint_value == service_value


class TestPersistence:
    def test_disabled_reports_disabled(self, wired_relay, client, monkeypatch):
        monkeypatch.setattr(settings, "persistence_enabled", False)
        monkeypatch.setattr(
            settings,
            "persistence_path",
            str("should_not_be_reported.db"),
        )

        relay = wired_relay()
        assert relay.state_store is None

        response = client.get("/diagnostics")

        persistence = response.json()["persistence"]
        assert persistence["enabled"] is False
        assert persistence["available"] is False
        assert persistence["path"] is None
        assert persistence["load_count"] == 0
        assert persistence["flush_count"] == 0
        assert persistence["initialization_error"] is None

    def test_enabled_reports_load_and_flush(
        self, wired_relay, client, monkeypatch, tmp_path
    ):
        path = tmp_path / "diag_state.db"
        monkeypatch.setattr(settings, "persistence_enabled", True)
        monkeypatch.setattr(settings, "persistence_path", str(path))

        relay = wired_relay()
        assert relay.state_store is not None

        relay.health_store.record_failure("A", "a-1", "timeout")
        relay.health_store.record_failure("A", "a-1", "timeout")
        relay.telemetry.record_attempt("A", "a-1", True, latency_ms=50)
        relay.state_flusher.flush()

        response = client.get("/diagnostics")

        persistence = response.json()["persistence"]
        assert persistence["enabled"] is True
        assert persistence["available"] is True
        assert persistence["path"] == str(path)
        assert persistence["schema_version"] == StateStore.SCHEMA_VERSION
        assert persistence["storage_status"] == "ok"
        assert persistence["learned_memory"] == {
            "learned_providers": 1,
            "telemetry_pairs": 1,
            "quality_pairs": 0,
            "decision_stats_rows": 1,
        }
        assert persistence["retention_days"] == settings.persistence_retention_days
        assert persistence["load_count"] == 4
        assert persistence["flush_count"] == 1
        assert persistence["last_load_at"]
        assert persistence["last_flush_at"]
        assert persistence["load_errors"] == []
        assert persistence["flush_errors"] == []


class TestAdaptiveSection:
    def test_disabled_reports_config_and_zero_confidence(self, wired_relay, client):
        provider = make_provider("A", ["a-1"], priority=10)
        relay = wired_relay()
        relay.provider_manager.register(provider)
        relay.telemetry.record_attempt("A", "a-1", True, latency_ms=50)

        response = client.get("/diagnostics")

        adaptive = response.json()["adaptive"]
        assert adaptive["config"]["enabled"] is False
        assert adaptive["config"]["min_samples"] == settings.adaptive_min_samples
        assert adaptive["state"][0]["confidence"] == 0.0

    def test_enabled_reports_learned_state(
        self, wired_relay, client, monkeypatch
    ):
        monkeypatch.setattr(settings, "adaptive_routing_enabled", True)
        monkeypatch.setattr(settings, "adaptive_min_samples", 1)
        provider = make_provider("A", ["a-1"], priority=10)
        relay = wired_relay()
        relay.provider_manager.register(provider)
        relay.telemetry.record_attempt("A", "a-1", True, latency_ms=50)

        response = client.get("/diagnostics")

        adaptive = response.json()["adaptive"]
        assert adaptive["config"]["enabled"] is True
        state = adaptive["state"][0]
        assert state["provider"] == "A"
        assert state["model"] == "a-1"
        assert state["request_count"] == 1
        assert state["confidence"] == 1.0
        assert state["ewma_success"] == 1.0
        assert state["ewma_latency_ms"] == 50.0
        assert state["latency_trend_ms"] == 0.0


class TestPrivacy:
    def test_no_api_keys_or_user_data(self, wired_relay, client, monkeypatch):
        monkeypatch.setattr(settings, "openai_api_key", "sk-super-secret")
        provider = make_provider(
            "A",
            ["a-1"],
            api_key="sk-provider-secret",
        )
        relay = wired_relay()
        relay.provider_manager.register(provider)
        relay.telemetry.record_attempt("A", "a-1", True, latency_ms=50)
        relay.health_store.record_failure("A", "a-1", "timeout")

        response = client.get("/diagnostics")

        raw = json.dumps(response.json())
        for secret in (
            "sk-super-secret",
            "sk-provider-secret",
            "SECRET_PROMPT",
            "SECRET_RESPONSE",
        ):
            assert secret not in raw


class TestOperationsSection:
    def test_operations_block_present_and_empty(self, wired_relay, client):
        from app.services.ops_store import ops_store

        ops_store.clear()

        provider = make_provider("A", ["a-1"], priority=10)
        relay = wired_relay()
        relay.provider_manager.register(provider)

        operations = DiagnosticsService().build_snapshot(relay)["operations"]

        assert operations["requests"] == 0
        assert operations["successes"] == 0
        assert operations["failures"] == 0
        assert operations["success_rate"] is None
        assert operations["average_latency_ms"] is None
        assert operations["p50_latency_ms"] is None
        assert operations["p95_latency_ms"] is None
        assert operations["streaming"]["requests"] == 0
        assert operations["providers"] == []
        assert operations["endpoints"] == []
        assert set(operations["auth"].keys()) == {"failures", "authenticated"}

    def test_operations_block_reflects_requests(self, wired_relay, client):
        from app.services.ops_store import ops_store

        ops_store.clear()

        provider = make_provider("A", ["a-1"], priority=10)
        relay = wired_relay()
        relay.provider_manager.register(provider)

        client.get("/health")
        response = client.get("/diagnostics")

        operations = response.json()["operations"]
        assert operations["requests"] == 1
        assert operations["successes"] == 1
        assert operations["success_rate"] == 1.0
        assert operations["endpoints"] == [
            {
                "route": "/health",
                "requests": 1,
                "average_latency_ms": operations["average_latency_ms"],
            }
        ]

        _no_api_key_keys(response.json())
