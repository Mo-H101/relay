"""
Registry-driven runtime provider construction (P4.1).

The provider registry is the single source of runtime truth: every
runtime provider is built by ``build_runtime_provider`` from its
``ProviderDefinition``, and the per-provider factory modules are thin
wrappers around it. These tests guard that contract, the stable-id
resolution, and the priority reconciliation (D2: openai runtime priority
5, matching the live factory).
"""

import pytest

from app.core.config import settings
from app.providers.base import Provider
from app.providers.factory import build_runtime_provider
from app.providers.lmstudio import create_provider as create_lmstudio_provider
from app.providers.nvidia import create_provider as create_nvidia_provider
from app.providers.openai import create_provider as create_openai_provider
from app.providers.registry import PROVIDER_REGISTRY


@pytest.fixture(autouse=True)
def no_discovery_network(monkeypatch):
    """
    Keep tests hermetic: discovery must never hit the network. Tests that
    want discovery results patch a client's list_models explicitly.
    """
    from app.providers.lmstudio_client import LMStudioClient
    from app.providers.nvidia_client import NvidiaClient
    from app.providers.openai_client import OpenAIClient

    for client_class in (NvidiaClient, OpenAIClient, LMStudioClient):
        monkeypatch.setattr(
            client_class,
            "list_models",
            lambda self, provider: (_ for _ in ()).throw(
                AssertionError("unexpected discovery call")
            ),
        )


def test_factory_sets_stable_id():
    provider = build_runtime_provider(PROVIDER_REGISTRY["nvidia"])

    assert provider.id == "nvidia"
    assert provider.identity() == "nvidia"
    assert provider.name == "NVIDIA"


def test_wrappers_match_registry_factory():
    for wrapper, defn in (
        (create_nvidia_provider, PROVIDER_REGISTRY["nvidia"]),
        (create_openai_provider, PROVIDER_REGISTRY["openai"]),
        (create_lmstudio_provider, PROVIDER_REGISTRY["lmstudio"]),
    ):
        direct = build_runtime_provider(defn)
        wrapped = wrapper()

        assert wrapped.id == direct.id == defn.id
        assert wrapped.name == direct.name == defn.provider_name
        assert wrapped.base_url == direct.base_url
        assert wrapped.priority == direct.priority
        assert wrapped.requires_api_key == direct.requires_api_key
        assert wrapped.health_endpoint == direct.health_endpoint


def test_openai_priority_regression():
    defn = PROVIDER_REGISTRY["openai"]
    assert defn.runtime_priority == 5

    provider = build_runtime_provider(defn)
    assert provider.priority == 5


def test_cloud_provider_without_key_skips_discovery(monkeypatch):
    monkeypatch.setattr(settings, "nvidia_api_key", "")

    provider = build_runtime_provider(PROVIDER_REGISTRY["nvidia"])

    assert provider.models == []
    assert provider.priority_models == []


def test_cloud_provider_with_key_discovers_and_orders(monkeypatch):
    from app.providers.nvidia_client import NvidiaClient

    monkeypatch.setattr(settings, "nvidia_api_key", "sk-test")
    monkeypatch.setattr(
        settings,
        "nvidia_model_priority",
        ["b", "c"],
    )
    monkeypatch.setattr(
        NvidiaClient,
        "list_models",
        lambda self, provider: ["a", "b", "c"],
    )

    provider = build_runtime_provider(PROVIDER_REGISTRY["nvidia"])

    assert provider.models == ["b", "c", "a"]
    assert provider.priority_models == ["b", "c"]


def test_local_provider_discovers_without_key(monkeypatch):
    from app.providers.lmstudio_client import LMStudioClient

    monkeypatch.setattr(
        LMStudioClient,
        "list_models",
        lambda self, provider: ["llama-1", "llama-2"],
    )

    provider = build_runtime_provider(PROVIDER_REGISTRY["lmstudio"])

    assert provider.requires_api_key is False
    assert provider.models == ["llama-1", "llama-2"]


def test_lmstudio_priority_and_base_url_overrides(monkeypatch):
    from app.providers.lmstudio_client import LMStudioClient

    monkeypatch.setattr(settings, "lmstudio_priority", 3)
    monkeypatch.setattr(
        settings, "lmstudio_base_url", "http://127.0.0.1:9999/v1"
    )
    monkeypatch.setattr(
        LMStudioClient,
        "list_models",
        lambda self, provider: [],
    )

    provider = build_runtime_provider(PROVIDER_REGISTRY["lmstudio"])

    assert provider.priority == 3
    assert provider.base_url == "http://127.0.0.1:9999/v1"


def test_discovery_failure_yields_empty_models(monkeypatch):
    from app.providers.lmstudio_client import LMStudioClient

    def boom(self, provider):
        raise RuntimeError("offline")

    monkeypatch.setattr(LMStudioClient, "list_models", boom)

    provider = build_runtime_provider(PROVIDER_REGISTRY["lmstudio"])

    assert provider.models == []
    assert provider.priority_models == []


def test_identity_falls_back_to_name():
    legacy = Provider(name="NVIDIA", base_url="https://nvidia.invalid")

    assert legacy.id == ""
    assert legacy.identity() == "NVIDIA"

    modern = Provider(
        id="nvidia",
        name="NVIDIA",
        base_url="https://nvidia.invalid",
    )

    assert modern.identity() == "nvidia"
