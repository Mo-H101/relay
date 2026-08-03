"""
Full-stack integration tests: the recommended production intelligence
profile with every feature enabled at once.

Verifies the Phase 6E-7F stack works end to end when activated together:
telemetry collection, adaptive EWMA learning, health learning and health
aware routing, quality scoring, the decision engine, and persistence.
A control class confirms the all-off legacy path is unchanged.

The tests use the real Relay/ChatService wiring with fake provider
clients (no network, no API keys). Feature flags are turned on only for
the lifetime of each test via monkeypatch; defaults are untouched.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.relay import Relay
from app.main import app as fastapi_app
from app.providers.base import Provider
from app.providers.exceptions import ProviderError
from app.services.memory_contract import contains_never_captured

import app.api.chat
import app.api.decision
import app.api.diagnostics
import app.api.health
import app.api.providers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_provider(name, models, priority=1, api_key="test-key"):
    return Provider(
        name=name,
        base_url=f"https://{name.lower()}.invalid",
        api_key=api_key,
        enabled=True,
        priority=priority,
        models=list(models),
    )


class FakeClient:
    """Provider client with scripted chat outcomes; no network."""

    def __init__(self):
        self.chat_calls = []
        self._outcomes = {}

    def set_outcomes(self, model, outcomes):
        self._outcomes[model] = list(outcomes)

    def chat(self, provider, model, message, timeout=None, max_tokens=None, **kwargs):
        self.chat_calls.append((provider.name, model))

        queue = self._outcomes.get(model)

        if not queue:
            raise ProviderError(f"no outcome configured for {model}")

        outcome = queue[0]

        if len(queue) > 1:
            queue.pop(0)

        if isinstance(outcome, Exception):
            raise outcome

        return outcome

    async def achat(self, provider, model, message, timeout=None, max_tokens=None, **kwargs):
        """Async version of chat()."""
        self.chat_calls.append((provider.name, model))

        queue = self._outcomes.get(model)

        if not queue:
            raise ProviderError(f"no outcome configured for {model}")

        outcome = queue[0]

        if len(queue) > 1:
            queue.pop(0)

        if isinstance(outcome, Exception):
            raise outcome

        return outcome

    async def achat_stream(self, provider, model, message, **kwargs):
        """Async version of chat_stream()."""
        raise ProviderError("streaming not configured in full-stack tests")

    def chat_stream(self, provider, model, message, **kwargs):
        raise ProviderError("streaming not configured in full-stack tests")

    def probe_model(self, provider, model):
        raise ProviderError("probe not configured in full-stack tests")


def disable_builtin_providers(monkeypatch):
    """Keep Relay from registering NVIDIA/OpenAI/LM Studio factories."""
    monkeypatch.setattr(settings, "nvidia_enabled", False)
    monkeypatch.setattr(settings, "openai_enabled", False)
    monkeypatch.setattr(settings, "lmstudio_enabled", False)


def enable_profile(monkeypatch, tmp_path):
    """
    The recommended production intelligence profile: every intelligence
    feature on with its default thresholds, persistence on, health
    refresh left off (background prober needs a live network).
    """
    disable_builtin_providers(monkeypatch)

    monkeypatch.setattr(settings, "telemetry_enabled", True)

    monkeypatch.setattr(settings, "health_feedback_enabled", True)
    monkeypatch.setattr(settings, "health_aware_routing", True)

    monkeypatch.setattr(settings, "adaptive_routing_enabled", True)

    monkeypatch.setattr(settings, "quality_feedback_enabled", True)

    monkeypatch.setattr(settings, "decision_engine_enabled", True)
    monkeypatch.setattr(settings, "decision_explanations_enabled", True)

    monkeypatch.setattr(settings, "persistence_enabled", True)
    monkeypatch.setattr(
        settings,
        "persistence_path",
        str(tmp_path / "full_stack.db"),
    )
    monkeypatch.setattr(settings, "persistence_flush_interval_seconds", 60)
    monkeypatch.setattr(settings, "persistence_retention_days", 0)


@pytest.fixture(autouse=True)
def fake_registry(monkeypatch):
    """Point every ClientRegistry at FakeClients, no real network."""
    from app.services import client_registry

    holder = {}

    def fake_get(self, provider_name):
        return holder[provider_name]

    monkeypatch.setattr(client_registry.ClientRegistry, "get", fake_get)
    return holder


def build_relay(monkeypatch, fake_registry, providers, clients=None):
    """Fresh Relay with the given providers/clients, wired into the API."""
    relay = Relay()

    for provider in providers:
        relay.provider_manager.register(provider)

    for name, client in (clients or {}).items():
        fake_registry[name] = client

    monkeypatch.setattr(app.api.chat, "relay", relay)
    monkeypatch.setattr(app.api.decision, "relay", relay)
    monkeypatch.setattr(app.api.diagnostics, "relay", relay)
    monkeypatch.setattr(app.api.health, "relay", relay)
    monkeypatch.setattr(app.api.providers, "relay", relay)
    return relay


@pytest.fixture
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Activation: the full stack constructs and runs together
# ---------------------------------------------------------------------------


class TestActivation:
    def test_full_stack_constructs_and_chats_ok(self, monkeypatch, fake_registry, tmp_path):
        enable_profile(monkeypatch, tmp_path)
        provider = make_provider("A", ["a1"], priority=1)
        fake_registry["A"] = FakeClient()
        fake_registry["A"].set_outcomes("a1", ["hello"])
        relay = build_relay(monkeypatch, fake_registry, [provider])

        assert relay.telemetry is not None
        assert relay.quality_store is not None
        assert relay.decision_engine.enabled
        assert relay.state_store is not None
        assert relay.state_flusher is not None

        result = relay.chat("hello")

        assert result["success"] is True
        assert result["response"] == "hello"
        assert relay.decision_engine.stats()["decisions"] >= 1

        stats = relay.telemetry.get("A", "a1")
        assert stats is not None
        assert stats.request_count >= 1
        assert stats.success_count >= 1

    def test_legacy_control_all_off_preserves_input_order(
        self, monkeypatch, fake_registry, tmp_path
    ):
        disable_builtin_providers(monkeypatch)
        monkeypatch.setattr(settings, "telemetry_enabled", False)
        monkeypatch.setattr(settings, "adaptive_routing_enabled", False)
        monkeypatch.setattr(settings, "quality_feedback_enabled", False)
        monkeypatch.setattr(settings, "health_feedback_enabled", False)
        monkeypatch.setattr(settings, "health_aware_routing", False)
        monkeypatch.setattr(settings, "decision_engine_enabled", False)
        monkeypatch.setattr(settings, "persistence_enabled", False)

        provider_a = make_provider("A", ["a1"], priority=1)
        provider_b = make_provider("B", ["b1"], priority=1)
        relay = build_relay(
            monkeypatch, fake_registry, [provider_a, provider_b]
        )

        assert relay.state_store is None

        candidates = relay.candidate_builder.build([provider_b, provider_a])
        assert [c[0].name for c in candidates] == ["B", "A"]


# ---------------------------------------------------------------------------
# The learning loop: chat -> telemetry -> adaptive reordering
# ---------------------------------------------------------------------------


class TestLearningLoop:
    def test_real_chat_drives_telemetry_and_health_learning(
        self, monkeypatch, fake_registry, tmp_path
    ):
        enable_profile(monkeypatch, tmp_path)
        monkeypatch.setattr(settings, "adaptive_learning_rate", 1.0)
        monkeypatch.setattr(
            settings, "health_feedback_model_unknown_degraded_threshold", 1
        )

        provider_a = make_provider("A", ["a1"], priority=10)
        provider_b = make_provider("B", ["b1"], priority=1)
        fake_registry["A"] = FakeClient()
        fake_registry["A"].set_outcomes("a1", [ProviderError("boom")])
        fake_registry["B"] = FakeClient()
        fake_registry["B"].set_outcomes("b1", ["ok"])
        relay = build_relay(
            monkeypatch, fake_registry, [provider_a, provider_b]
        )

        # A fails and B fails over, so one real chat drives telemetry and
        # health feedback for both providers.
        result = relay.chat("hello")

        assert result["success"] is True
        assert result["provider"] == "B"

        a_stats = relay.telemetry.get("A", "a1")
        b_stats = relay.telemetry.get("B", "b1")
        assert a_stats is not None and a_stats.ewma_success == 0.0
        assert b_stats is not None and b_stats.ewma_success == 1.0

        # The failed attempt drove health learning: A is now degraded.
        learned_a = relay.health_store.learned("A")
        assert learned_a is not None
        assert "a1" in learned_a.degraded_models

        # Health-aware routing now avoids A entirely on the next chat.
        result = relay.chat("again")
        assert result["provider"] == "B"

    def test_adaptive_reorders_within_band_when_confident(
        self, monkeypatch, fake_registry, tmp_path
    ):
        enable_profile(monkeypatch, tmp_path)
        monkeypatch.setattr(settings, "adaptive_min_samples", 1)
        monkeypatch.setattr(settings, "adaptive_learning_rate", 1.0)

        provider_a = make_provider("A", ["a1"], priority=1)
        provider_b = make_provider("B", ["b1"], priority=1)
        relay = build_relay(
            monkeypatch, fake_registry, [provider_a, provider_b]
        )

        # A: 5 successes then 5 failures -> EWMA reliability 0.0.
        for _ in range(5):
            relay.telemetry.record_attempt("A", "a1", True, latency_ms=50)
        for _ in range(5):
            relay.telemetry.record_attempt("A", "a1", False, latency_ms=50)
        # B: 5 failures then 5 successes -> EWMA reliability 1.0.
        for _ in range(5):
            relay.telemetry.record_attempt("B", "b1", False, latency_ms=50)
        for _ in range(5):
            relay.telemetry.record_attempt("B", "b1", True, latency_ms=50)

        # Cumulative signals are identical (5/10 successes, same latency),
        # so the flip is driven purely by the confident adaptive EWMA.
        candidates = relay.candidate_builder.build([provider_a, provider_b])
        assert [c[0].name for c in candidates] == ["B", "A"]

    def test_adaptive_neutral_below_min_samples(
        self, monkeypatch, fake_registry, tmp_path
    ):
        enable_profile(monkeypatch, tmp_path)
        # Default min_samples (10) is not met by the seeded observations.
        monkeypatch.setattr(settings, "adaptive_learning_rate", 1.0)

        provider_a = make_provider("A", ["a1"], priority=1)
        provider_b = make_provider("B", ["b1"], priority=1)
        relay = build_relay(
            monkeypatch, fake_registry, [provider_a, provider_b]
        )

        # A: fail, success, success -> 2/3 successes, EWMA reliability 1.0.
        relay.telemetry.record_attempt("A", "a1", False, latency_ms=50)
        relay.telemetry.record_attempt("A", "a1", True, latency_ms=50)
        relay.telemetry.record_attempt("A", "a1", True, latency_ms=50)
        # B: success, success, fail -> 2/3 successes, EWMA reliability 0.0.
        relay.telemetry.record_attempt("B", "b1", True, latency_ms=50)
        relay.telemetry.record_attempt("B", "b1", True, latency_ms=50)
        relay.telemetry.record_attempt("B", "b1", False, latency_ms=50)

        # Providers are given B first so the EWMA ordering (A better)
        # differs from the input order, but below min_samples adaptive
        # stays neutral and the input order is preserved.
        candidates = relay.candidate_builder.build([provider_b, provider_a])
        assert [c[0].name for c in candidates] == ["B", "A"]

    def test_health_band_primary_with_all_signals_on(
        self, monkeypatch, fake_registry, tmp_path
    ):
        enable_profile(monkeypatch, tmp_path)
        monkeypatch.setattr(settings, "adaptive_min_samples", 1)
        monkeypatch.setattr(settings, "adaptive_learning_rate", 1.0)

        provider_a = make_provider("A", ["a1"], priority=1)
        provider_b = make_provider("B", ["b1"], priority=1)
        fake_registry["A"] = FakeClient()
        fake_registry["A"].set_outcomes("a1", ["ok"])
        relay = build_relay(
            monkeypatch, fake_registry, [provider_a, provider_b]
        )

        # Three timeouts degrade B's learned health state.
        for _ in range(3):
            relay.health_store.record_failure("B", "b1", "timeout")
        assert "b1" in relay.health_store.learned("B").degraded_models

        # Health is primary: degraded B is excluded from the candidate
        # pool entirely, leaving healthy A as the only routed option.
        candidates = relay.candidate_builder.build([provider_b, provider_a])
        assert [c[0].name for c in candidates] == ["A"]

        result = relay.chat("hello")
        assert result["provider"] == "A"


# ---------------------------------------------------------------------------
# Quality scoring
# ---------------------------------------------------------------------------


class TestQualityScoring:
    def test_quality_shifts_ranking_when_confident(
        self, monkeypatch, fake_registry, tmp_path
    ):
        enable_profile(monkeypatch, tmp_path)
        monkeypatch.setattr(settings, "quality_feedback_min_samples", 1)

        provider_a = make_provider("A", ["a1"], priority=1)
        provider_b = make_provider("B", ["b1"], priority=1)
        relay = build_relay(
            monkeypatch, fake_registry, [provider_a, provider_b]
        )

        relay.quality_store.record("A", "a1", 5)
        relay.quality_store.record("B", "b1", 1)

        assert (
            relay.quality_store.quality_signal("A", "a1").score
            > relay.quality_store.quality_signal("B", "b1").score
        )

        candidates = relay.candidate_builder.build([provider_b, provider_a])
        assert [c[0].name for c in candidates] == ["A", "B"]

    def test_quality_neutral_below_min_samples(
        self, monkeypatch, fake_registry, tmp_path
    ):
        enable_profile(monkeypatch, tmp_path)
        monkeypatch.setattr(settings, "quality_feedback_min_samples", 5)

        provider_a = make_provider("A", ["a1"], priority=1)
        provider_b = make_provider("B", ["b1"], priority=1)
        relay = build_relay(
            monkeypatch, fake_registry, [provider_a, provider_b]
        )

        relay.quality_store.record("A", "a1", 5)
        relay.quality_store.record("B", "b1", 1)

        candidates = relay.candidate_builder.build([provider_b, provider_a])
        assert [c[0].name for c in candidates] == ["B", "A"]


# ---------------------------------------------------------------------------
# Persistence: full-stack state survives a restart
# ---------------------------------------------------------------------------


class TestPersistenceRestart:
    def test_full_stack_survives_restart(self, monkeypatch, fake_registry, tmp_path):
        enable_profile(monkeypatch, tmp_path)

        provider_a = make_provider("A", ["a1"], priority=1)
        provider_b = make_provider("B", ["b1"], priority=1)
        providers = [provider_a, provider_b]

        relay1 = build_relay(monkeypatch, fake_registry, providers)

        relay1.telemetry.record_attempt("A", "a1", True, latency_ms=10)
        relay1.telemetry.record_attempt("A", "a1", True, latency_ms=10)
        relay1.telemetry.record_attempt("B", "b1", False, latency_ms=90)
        for _ in range(3):
            relay1.health_store.record_failure("B", "b1", "timeout")
        relay1.quality_store.record("A", "a1", 5)
        relay1.decision_engine.decide(providers)
        relay1.state_flusher.flush()

        relay2 = build_relay(monkeypatch, fake_registry, providers)

        a_stats = relay2.telemetry.get("A", "a1")
        b_stats = relay2.telemetry.get("B", "b1")
        assert a_stats is not None and a_stats.request_count == 2
        assert b_stats is not None and b_stats.failure_count == 1

        assert "b1" in relay2.health_store.learned("B").degraded_models

        signal = relay2.quality_store.quality_signal("A", "a1")
        assert signal is not None and signal.sample_count == 1

        assert relay2.decision_engine.stats()["decisions"] == 1

        # Learned state still drives routing on the restarted relay:
        # degraded B is excluded from the candidate pool entirely.
        candidates = relay2.candidate_builder.build([provider_b, provider_a])
        assert [c[0].name for c in candidates] == ["A"]


# ---------------------------------------------------------------------------
# Diagnostics reflect the active intelligence stack
# ---------------------------------------------------------------------------


class TestDiagnosticsActive:
    def test_diagnostics_reports_intelligence_active(
        self, monkeypatch, fake_registry, client, tmp_path
    ):
        enable_profile(monkeypatch, tmp_path)
        provider = make_provider("A", ["a1"], priority=1)
        fake_registry["A"] = FakeClient()
        fake_registry["A"].set_outcomes("a1", ["hello"])
        relay = build_relay(monkeypatch, fake_registry, [provider])

        chat_resp = client.post("/chat", json={"message": "hello"})
        assert chat_resp.status_code == 200

        resp = client.get("/diagnostics")
        assert resp.status_code == 200
        payload = resp.json()

        assert payload["adaptive"]["config"]["enabled"] is True
        assert payload["quality"]["enabled"] is True
        assert payload["persistence"]["enabled"] is True
        assert payload["scoring"]["decision_engine"]["enabled"] is True
        assert payload["scoring"]["weights"]["adaptive_reliability"] == 1.0
        assert payload["scoring"]["ranking"]

        assert not contains_never_captured(payload)
