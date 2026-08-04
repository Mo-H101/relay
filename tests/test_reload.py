"""
Behavior tests for hot configuration reload (Phase 6E).

reload_config mutates the in-process settings singleton and Provider
objects in place, so each test injects a hermetic ``env`` object and a
lightweight fake relay. The global settings singleton is snapshotted
and restored around every test.
"""

import json
import os
from types import SimpleNamespace

import pytest

import app.services.reload as reload_module
from app.core.config import settings
from app.providers.base import Provider
from app.providers.registry import PROVIDER_REGISTRY
from app.services.candidate_builder import CandidateBuilder
from app.services.health_store import HealthStore
from app.services.provider_manager import ProviderManager
from app.services.reload import reload_config
from app.services.routing import RoutingEngine
from app.services.quality import QualityStore
from app.services.telemetry import TelemetryStore
from app.services.provider_manager import _LEGACY_NAME_TO_ID


class FakeProviderManager:
    def __init__(self):
        self.providers = {}

    def get(self, name):
        provider = self.providers.get(name)

        if provider is None:
            provider = self.providers.get(
                _LEGACY_NAME_TO_ID.get(name, name)
            )

        return provider

    def register(self, provider):
        self.providers[provider.identity()] = provider


class FakeRefreshable:
    def __init__(self):
        self.calls = 0

    def refresh(self):
        self.calls += 1

    def refresh_thresholds(self):
        self.calls += 1

    def refresh_scorer(self):
        self.calls += 1

    def set_ewma_alpha(self, alpha):
        self.calls += 1
        self.last_ewma_alpha = alpha

    def set_alpha(self, alpha):
        self.calls += 1
        self.last_quality_alpha = alpha

    def set_min_samples(self, min_samples):
        self.calls += 1
        self.last_quality_min_samples = min_samples

    def set_retention_limit(self, limit):
        self.calls += 1
        self.last_quality_retention_limit = limit

    def set_retention_days(self, days):
        self.calls += 1
        self.last_retention_days = days


class FakeRelay:
    def __init__(self):
        self.provider_manager = FakeProviderManager()
        self.routing = FakeRefreshable()
        self.health_store = FakeRefreshable()
        self.candidate_builder = FakeRefreshable()
        self.telemetry = FakeRefreshable()
        self.quality_store = FakeRefreshable()
        self.decision_engine = FakeRefreshable()
        self.state_flusher = FakeRefreshable()


@pytest.fixture(autouse=True)
def restore_settings():
    fields = [
        field
        for field in reload_module._RELOADABLE_FIELDS
        if hasattr(settings, field)
    ]
    snapshot = {field: getattr(settings, field) for field in fields}
    yield
    for field, value in snapshot.items():
        setattr(settings, field, value)


class TestDryRun:
    def test_reports_changes_without_mutation(self):
        relay = FakeRelay()
        before = settings.request_timeout

        result = reload_config(
            relay, dry_run=True, env=SimpleNamespace(request_timeout=before + 5)
        )

        assert result["reloaded"] is True
        assert result["dry_run"] is True
        assert result["applied"] == ["request_timeout"]
        assert result["failures"] == []
        assert settings.request_timeout == before
        assert relay.routing.calls == 0

    def test_unchanged_fields_are_not_applied(self):
        relay = FakeRelay()

        result = reload_config(
            relay,
            dry_run=True,
            env=SimpleNamespace(request_timeout=settings.request_timeout),
        )

        assert result["reloaded"] is True
        assert result["applied"] == []
        assert "request_timeout" in result["unchanged"]


class TestApply:
    def test_mutates_settings_and_refreshes_components(self):
        relay = FakeRelay()
        before = settings.request_timeout

        result = reload_config(
            relay, env=SimpleNamespace(request_timeout=before + 5)
        )

        assert result["reloaded"] is True
        assert result["applied"] == ["request_timeout"]
        assert settings.request_timeout == before + 5
        assert relay.routing.calls == 1
        assert relay.health_store.calls == 1
        assert relay.candidate_builder.calls == 1
        assert relay.telemetry.calls == 1

    def test_provider_enable_side_effect(self):
        relay = FakeRelay()
        provider = Provider(
            id="nvidia",
            name="NVIDIA",
            base_url="https://nvidia.invalid",
            api_key="old-key",
            enabled=False,
            models=["m1"],
        )
        relay.provider_manager.register(provider)

        result = reload_config(
            relay, env=SimpleNamespace(nvidia_enabled=True)
        )

        assert result["reloaded"] is True
        assert "nvidia_enabled" in result["applied"]
        assert provider.enabled is True

    def test_apply_failure_rolls_back(self, monkeypatch):
        relay = FakeRelay()
        before = settings.request_timeout

        def boom():
            raise RuntimeError("refresh exploded")

        monkeypatch.setattr(relay.routing, "refresh", boom)

        result = reload_config(
            relay, env=SimpleNamespace(request_timeout=before + 5)
        )

        assert result["reloaded"] is False
        assert result["error_kind"] == "apply"
        assert settings.request_timeout == before


class TestRegisterNewProvider:
    """
    The register-new-provider branch of _apply_provider_side_effects must
    hand the registry-driven factory its ProviderDefinition (P4.2.1 fix):
    without it the factory call raises TypeError and a provider that was
    never registered at startup can never be brought up by reload.
    """

    def test_register_new_runtime_provider_through_reload(self, monkeypatch):
        monkeypatch.setattr(
            "app.providers.nvidia_client.NvidiaClient.list_models",
            lambda self, provider: ["m1", "m2"],
        )

        relay = FakeRelay()

        result = reload_config(
            relay,
            env=SimpleNamespace(
                nvidia_enabled=True,
                nvidia_api_key="sk-test",
            ),
        )

        assert result["reloaded"] is True
        assert "nvidia_enabled" in result["applied"]
        assert "nvidia_api_key" in result["applied"]
        assert result["failures"] == []
        assert "nvidia" in relay.provider_manager.providers

        provider = relay.provider_manager.providers["nvidia"]
        assert provider.name == "NVIDIA"
        assert provider.id == "nvidia"
        assert provider.enabled is True
        assert provider.api_key == "sk-test"
        assert provider.models == ["m1", "m2"]

    def test_factory_receives_provider_definition(self, monkeypatch):
        received = {}

        def spy_factory(defn):
            received["defn"] = defn
            return Provider(
                id=defn.id,
                name=defn.provider_name,
                base_url=defn.base_url_default,
                enabled=True,
            )

        spec = {
            "id": "nvidia",
            "prefix": "nvidia",
            "defn": PROVIDER_REGISTRY["nvidia"],
            "factory": spy_factory,
            "client": PROVIDER_REGISTRY["nvidia"].client,
        }
        monkeypatch.setattr(reload_module, "_PROVIDER_SPECS", (spec,))

        relay = FakeRelay()

        result = reload_config(
            relay, env=SimpleNamespace(nvidia_enabled=True)
        )

        assert result["reloaded"] is True
        assert result["failures"] == []
        assert received["defn"] is PROVIDER_REGISTRY["nvidia"]
        assert "nvidia" in relay.provider_manager.providers

    def test_registered_provider_is_mutated_in_place_not_rebuilt(self, monkeypatch):
        called = {"count": 0}

        def spy_factory(defn):
            called["count"] += 1
            return Provider(
                id=defn.id,
                name=defn.provider_name,
                base_url=defn.base_url_default,
                enabled=True,
            )

        monkeypatch.setattr(
            reload_module, "build_runtime_provider", spy_factory
        )

        relay = FakeRelay()
        provider = Provider(
            id="nvidia",
            name="NVIDIA",
            base_url="https://nvidia.invalid",
            api_key="old-key",
            enabled=False,
            models=["m1"],
        )
        relay.provider_manager.register(provider)

        result = reload_config(
            relay, env=SimpleNamespace(nvidia_enabled=True)
        )

        assert result["reloaded"] is True
        assert provider.enabled is True
        assert called["count"] == 0
        assert relay.provider_manager.providers["nvidia"] is provider


class TestScoringRefresh:
    """
    Prove a reload updates the *effective* scoring configuration, not
    just the settings singleton: the CandidateScorer is rebuilt (its
    weights and adaptive gates snapshots at construction) and the
    TelemetryStore's EWMA learning rate is refreshed.
    """

    def _real_relay(self):
        return SimpleNamespace(
            provider_manager=ProviderManager(),
            routing=RoutingEngine(),
            health_store=HealthStore(),
            candidate_builder=CandidateBuilder(),
            telemetry=TelemetryStore(),
            quality_store=QualityStore(),
            decision_engine=FakeRefreshable(),
            state_flusher=FakeRefreshable(),
        )

    def test_reload_updates_effective_scoring_and_adaptive_config(self):
        relay = self._real_relay()

        result = reload_config(
            relay,
            env=SimpleNamespace(
                scoring_latency_weight=2.5,
                adaptive_routing_enabled=True,
                adaptive_min_samples=5,
                adaptive_learning_rate=0.4,
                adaptive_latency_weight=2.0,
                adaptive_reliability_weight=1.5,
            ),
        )

        assert result["reloaded"] is True

        scorer = relay.candidate_builder._scorer
        assert scorer.latency_weight == 2.5
        assert scorer.adaptive_routing_enabled is True
        assert scorer.adaptive_min_samples == 5
        assert scorer.adaptive_latency_weight == 2.0
        assert scorer.adaptive_reliability_weight == 1.5

    def test_reload_updates_telemetry_ewma_learning_rate(self):
        relay = self._real_relay()
        relay.telemetry.record_attempt("P", "m", True, latency_ms=10)

        result = reload_config(
            relay, env=SimpleNamespace(adaptive_learning_rate=0.4)
        )

        assert result["reloaded"] is True
        relay.telemetry.record_attempt("P", "m", False, latency_ms=10)

        stats = relay.telemetry.get("P", "m")
        assert stats.ewma_success == pytest.approx(1.0 + 0.4 * (0.0 - 1.0))

    def test_rollback_restores_ewma_learning_rate(self, monkeypatch):
        relay = self._real_relay()
        before_alpha = settings.adaptive_learning_rate
        before_enabled = settings.adaptive_routing_enabled

        def boom():
            raise RuntimeError("refresh exploded")

        monkeypatch.setattr(
            relay.candidate_builder, "refresh_scorer", boom
        )

        result = reload_config(
            relay,
            env=SimpleNamespace(
                adaptive_learning_rate=0.4,
                adaptive_routing_enabled=True,
            ),
        )

        assert result["reloaded"] is False
        assert settings.adaptive_learning_rate == pytest.approx(before_alpha)
        assert settings.adaptive_routing_enabled is before_enabled
        assert relay.telemetry._ewma_alpha == pytest.approx(before_alpha)

    def test_reload_updates_effective_quality_config(self):
        relay = self._real_relay()

        result = reload_config(
            relay,
            env=SimpleNamespace(
                quality_feedback_enabled=True,
                quality_feedback_weight=2.0,
                quality_feedback_learning_rate=0.4,
                quality_feedback_min_samples=5,
                quality_feedback_retention_limit=200,
            ),
        )

        assert result["reloaded"] is True

        scorer = relay.candidate_builder._scorer
        assert scorer.quality_feedback_enabled is True
        assert scorer.quality_weight == 2.0

    def test_reload_updates_quality_store_learning_params(self):
        relay = self._real_relay()
        relay.quality_store.record("P", "m", 5)
        relay.quality_store.record("P", "m", 5)

        result = reload_config(
            relay,
            env=SimpleNamespace(
                quality_feedback_learning_rate=0.4,
                quality_feedback_min_samples=3,
                quality_feedback_retention_limit=50,
            ),
        )

        assert result["reloaded"] is True

        stats = relay.quality_store.stats()
        assert stats["learning_rate"] == 0.4
        assert stats["min_samples"] == 3
        assert stats["retention_limit"] == 50

    def test_rollback_restores_quality_learning_params(self, monkeypatch):
        relay = self._real_relay()
        before_alpha = settings.quality_feedback_learning_rate
        before_enabled = settings.quality_feedback_enabled
        relay.quality_store.set_alpha(0.5)

        def boom():
            raise RuntimeError("refresh exploded")

        monkeypatch.setattr(
            relay.candidate_builder, "refresh_scorer", boom
        )

        result = reload_config(
            relay,
            env=SimpleNamespace(
                quality_feedback_learning_rate=0.4,
                quality_feedback_enabled=True,
            ),
        )

        assert result["reloaded"] is False
        assert settings.quality_feedback_learning_rate == pytest.approx(
            before_alpha
        )
        assert settings.quality_feedback_enabled is before_enabled
        assert relay.quality_store.stats()["learning_rate"] == pytest.approx(
            before_alpha
        )


class TestPersistenceRetentionReload:
    def test_retention_days_reload_applies_to_flusher(self):
        relay = FakeRelay()
        before = settings.persistence_retention_days
        new_value = before + 1

        result = reload_config(
            relay, env=SimpleNamespace(persistence_retention_days=new_value)
        )

        assert result["reloaded"] is True
        assert "persistence_retention_days" in result["applied"]
        assert settings.persistence_retention_days == new_value
        assert relay.state_flusher.last_retention_days == new_value

    def test_retention_days_rolls_back_with_other_changes(self, monkeypatch):
        relay = FakeRelay()
        before = settings.persistence_retention_days

        def boom():
            raise RuntimeError("refresh exploded")

        monkeypatch.setattr(relay.routing, "refresh", boom)

        result = reload_config(
            relay,
            env=SimpleNamespace(
                persistence_retention_days=before + 1,
                request_timeout=settings.request_timeout + 1,
            ),
        )

        assert result["reloaded"] is False
        assert result["error_kind"] == "apply"
        assert settings.persistence_retention_days == before
        assert relay.state_flusher.last_retention_days == before


class TestSecrets:
    def test_secret_values_never_appear_in_report(self, monkeypatch):
        monkeypatch.setattr(
            "app.providers.nvidia_client.NvidiaClient.list_models",
            lambda self, provider: ["m1", "m2"],
        )

        relay = FakeRelay()
        relay.provider_manager.providers["nvidia"] = Provider(
            id="nvidia",
            name="NVIDIA",
            base_url="https://nvidia.invalid",
            api_key="old-key",
            enabled=True,
            models=["m1"],
        )
        secret = "sk-super-secret-value"

        result = reload_config(
            relay,
            env=SimpleNamespace(nvidia_enabled=True, nvidia_api_key=secret),
        )

        serialized = json.dumps(result)
        assert "nvidia_api_key" in result["applied"]
        assert secret not in serialized

    def test_validation_error_is_redacted_to_field_name(self, monkeypatch):
        def bad_settings():
            raise ValueError(
                "Invalid value for REQUEST_TIMEOUT: 'abc' "
                "(expected an integer)."
            )

        monkeypatch.setattr(reload_module, "Settings", bad_settings)

        result = reload_config(relay=FakeRelay(), env=None)

        assert result["reloaded"] is False
        assert result["error_kind"] == "validation"
        assert result["error"] == "Invalid value for REQUEST_TIMEOUT"


class TestDotenvOverlay:
    def test_overlay_restores_environment(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("REQUEST_TIMEOUT=99\n")
        monkeypatch.setenv("REQUEST_TIMEOUT", "30")

        with reload_module._env_overlay(str(env_file)):
            assert os.environ.get("REQUEST_TIMEOUT") == "99"

        assert os.environ.get("REQUEST_TIMEOUT") == "30"

    def test_dotenv_path_feeds_validation(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("REQUEST_TIMEOUT=77\n")
        monkeypatch.setattr(settings, "request_timeout", 30)

        result = reload_config(
            FakeRelay(), env=None, dotenv_path=str(env_file)
        )

        assert result["reloaded"] is True
        assert "request_timeout" in result["applied"]
        assert settings.request_timeout == 77
