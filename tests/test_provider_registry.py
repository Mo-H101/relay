"""
Provider registry integrity (P1).

The registry is the single source of truth the wizard, key validation,
config persistence, and (from P4) runtime wiring read from. These tests
guard its invariants so adding a provider stays a registry entry.

P4.3.4 (areas 1-2) adds registry/capability-matrix coherence checks for
the RUNTIME_READY set and ClientRegistry resolution.
"""

from app.core.config import settings
from app.providers.factory import build_runtime_provider
from app.providers.registry import PROVIDER_MENU, PROVIDER_REGISTRY, RUNTIME_READY
from app.services.client_registry import ClientRegistry

import pytest

from tests.conformance_helpers import (
    PROVIDER_CAPABILITIES,
    cap,
    endpoint_pattern,
)

EXPECTED_IDS = ["nvidia", "openai", "anthropic", "gemini", "lmstudio", "ollama"]

MATRIX_KEYS = {
    "wire",
    "auth",
    "tools",
    "stream_usage",
    "check_model",
    "gen_params",
    "discovery_normalize",
    "tool_finish_reason",
    "chat_forwarded_verbatim",
    "discovery_endpoint",
    "retry_after",
}


def test_six_providers_registered():
    assert set(PROVIDER_REGISTRY) == set(EXPECTED_IDS)


def test_menu_order_matches_product_spec():
    assert [defn.id for defn in PROVIDER_MENU] == EXPECTED_IDS


def test_provider_ids_unique():
    ids = [defn.id for defn in PROVIDER_MENU]
    assert len(ids) == len(set(ids))


def test_base_urls_are_http():
    for defn in PROVIDER_REGISTRY.values():
        assert defn.base_url_default.startswith(("http://", "https://"))


def test_env_naming_conventions():
    for defn in PROVIDER_REGISTRY.values():
        assert defn.enabled_env.endswith("_ENABLED")
        if defn.priority_env:
            assert defn.priority_env.endswith("_MODEL_PRIORITY")


def test_cloud_requires_key_and_key_env():
    for defn in PROVIDER_REGISTRY.values():
        if defn.kind == "cloud":
            assert defn.requires_api_key
            assert defn.key_env
            assert defn.key_attr
        else:
            assert not defn.requires_api_key


def test_ollama_is_keyless():
    defn = PROVIDER_REGISTRY["ollama"]
    assert defn.kind == "local"
    assert defn.key_env is None
    assert defn.key_attr is None


def test_build_provider():
    defn = PROVIDER_REGISTRY["openai"]
    provider = defn.build_provider(api_key="sk-test")

    assert provider.name == "OpenAI"
    assert provider.api_key == "sk-test"
    assert provider.base_url == defn.base_url_default
    assert provider.enabled is True


def test_build_provider_without_key():
    defn = PROVIDER_REGISTRY["ollama"]
    provider = defn.build_provider()

    assert provider.name == "Ollama"
    assert provider.has_api_key() is False


def test_every_client_exposes_setup_methods():
    for defn in PROVIDER_REGISTRY.values():
        client = defn.client()
        assert callable(getattr(client, "list_models"))
        assert callable(getattr(client, "probe_model"))
        assert callable(getattr(client, "key_check"))


# ---------------------------------------------------------------------------
# P4.3.4 area 1: registry and capability-matrix coherence
# ---------------------------------------------------------------------------


def test_runtime_ready_is_registered_and_covered_by_matrix():
    assert RUNTIME_READY.issubset(set(PROVIDER_REGISTRY))
    for provider_id in sorted(RUNTIME_READY):
        assert provider_id in PROVIDER_CAPABILITIES
        assert set(PROVIDER_CAPABILITIES[provider_id]) == MATRIX_KEYS


def test_discovery_endpoint_matches_health_endpoint():
    for provider_id in sorted(RUNTIME_READY):
        defn = PROVIDER_REGISTRY[provider_id]
        assert cap(provider_id, "discovery_endpoint") == defn.health_endpoint
        assert endpoint_pattern(provider_id)["discovery"] == defn.health_endpoint


def test_runtime_priorities_are_positive_and_unique():
    priorities = [PROVIDER_REGISTRY[i].runtime_priority for i in sorted(RUNTIME_READY)]
    assert all(p > 0 for p in priorities)
    assert len(priorities) == len(set(priorities))


@pytest.mark.parametrize("provider_id", sorted(RUNTIME_READY))
def test_runtime_provider_build_round_trips(provider_id, monkeypatch):
    defn = PROVIDER_REGISTRY[provider_id]
    monkeypatch.setattr(
        defn.client_class,
        "list_models",
        lambda self, provider: ["gpt-test"],
    )
    monkeypatch.setattr(settings, defn.enabled_attr, True)
    if defn.key_attr:
        monkeypatch.setattr(settings, defn.key_attr, "sk-test")

    provider = build_runtime_provider(defn)

    assert provider.id == defn.id
    assert provider.name == defn.provider_name
    assert provider.base_url == defn.base_url_default
    assert provider.health_endpoint == defn.health_endpoint
    assert provider.enabled is True
    assert "gpt-test" in provider.models


# ---------------------------------------------------------------------------
# P4.3.4 area 2: ClientRegistry resolution for every runtime provider
# ---------------------------------------------------------------------------


def test_client_registry_resolves_every_runtime_provider():
    registry = ClientRegistry()
    for provider_id in sorted(RUNTIME_READY):
        defn = PROVIDER_REGISTRY[provider_id]
        by_id = registry.get(provider_id)
        by_name = registry.get(defn.provider_name)
        assert by_id is not None
        assert by_name is by_id


def test_client_registry_resolves_by_legacy_provider_name():
    registry = ClientRegistry()
    for defn in PROVIDER_REGISTRY.values():
        assert registry.get(defn.provider_name) is not None
