"""
Real-server integration tests for the LM Studio provider.

These tests run only when an LM Studio instance is reachable; they are
skipped by default so CI never requires one. Opt in by setting
LMSTUDIO_REAL_BASE_URL to the base URL of a running LM Studio server
(e.g. http://localhost:1234/v1). The server must have at least one
instruct model loaded that can complete a short prompt.

The fake-server suite (test_lmstudio_integration.py) covers the same
behavior offline; this file is the ground-truth check against a real
OpenAI-compatible endpoint.
"""

import os

import pytest

from app.core.config import settings
from app.core.relay import Relay
from app.providers.lmstudio import create_provider
from app.services.explanation import ExplanationService

REAL_BASE_URL = os.getenv("LMSTUDIO_REAL_BASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not REAL_BASE_URL,
    reason="LMSTUDIO_REAL_BASE_URL not set; real LM Studio required.",
)


def patch_real_settings(monkeypatch):
    monkeypatch.setattr(settings, "lmstudio_enabled", True)
    monkeypatch.setattr(settings, "lmstudio_base_url", REAL_BASE_URL)
    monkeypatch.setattr(settings, "lmstudio_api_key", "")
    monkeypatch.setattr(settings, "lmstudio_priority", 1)
    monkeypatch.setattr(settings, "lmstudio_model_priority", [])
    monkeypatch.setattr(settings, "nvidia_enabled", False)
    monkeypatch.setattr(settings, "openai_enabled", False)
    monkeypatch.setattr(settings, "telemetry_enabled", True)


@pytest.fixture
def real_relay(monkeypatch):
    patch_real_settings(monkeypatch)
    return Relay()


class TestRealDiscovery:
    def test_discovery_lists_loaded_models(self, monkeypatch):
        patch_real_settings(monkeypatch)

        provider = create_provider()

        assert provider.name == "LM Studio"
        assert provider.models
        assert all(isinstance(model, str) and model for model in provider.models)

    def test_models_are_chat_capable(self, monkeypatch):
        patch_real_settings(monkeypatch)

        provider = create_provider()

        from app.services.capabilities import detect_capability

        assert all(
            detect_capability(model) is not None
            for model in provider.models
        )


class TestRealChat:
    def test_chat_succeeds(self, real_relay):
        result = real_relay.chat("Say OK.")

        assert result["success"] is True
        assert result["provider"] == "LM Studio"
        assert result["model"] in real_relay.provider_manager.get(
            "LM Studio"
        ).models
        assert result["response"]

    def test_health_report_has_lmstudio(self, real_relay):
        report = real_relay.health()

        lm = next(
            p for p in report["providers"] if p["name"] == "LM Studio"
        )
        assert "status" in lm
        assert "connectivity" in lm
        assert "healthy_models" in lm

    def test_decision_explain_selects_lmstudio(self, real_relay):
        providers = real_relay.provider_manager.ranked()

        assert providers

        ranked = real_relay.candidate_builder.ranked_candidates(providers)
        explanation = ExplanationService().explain(ranked, task=None)

        assert explanation["selected"]["provider"] == "LM Studio"
        assert explanation["candidates"]
        assert explanation["candidates"][0]["reasons"]
