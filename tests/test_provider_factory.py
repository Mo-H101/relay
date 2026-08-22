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
from app.providers import factory as factory_module
from app.providers import registry as registry_module
from app.providers.base import MAX_PROVIDER_MODELS, Provider
from app.providers.factory import build_runtime_provider
from app.providers.lmstudio import create_provider as create_lmstudio_provider
from app.providers.nvidia import create_provider as create_nvidia_provider
from app.providers.openai import create_provider as create_openai_provider
from app.providers.registry import PROVIDER_REGISTRY, RUNTIME_READY


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


def test_runtime_registry_path_is_independent_of_shims():
    """
    The registry/factory runtime path resolves providers straight to the
    ``*_client`` modules and never references the legacy shim facades
    (``app.providers.nvidia|openai|lmstudio``), so those shims are removable
    without touching runtime wiring. This guards the deprecation note added
    to each shim in P6.3.
    """
    import inspect

    runtime_source = (
        inspect.getsource(registry_module)
        + inspect.getsource(factory_module)
        + inspect.getsource(Provider)
    )

    for shim in (
        "providers.nvidia import",
        "providers.openai import",
        "providers.lmstudio import",
    ):
        assert shim not in runtime_source

    for pid, expected in (
        ("nvidia", "app.providers.nvidia_client"),
        ("openai", "app.providers.openai_client"),
        ("lmstudio", "app.providers.lmstudio_client"),
    ):
        defn = PROVIDER_REGISTRY[pid]
        assert defn.client_class.__module__ == expected
        provider = defn.build_provider(base_url="http://127.0.0.1:1")
        assert isinstance(provider, Provider)
        assert provider.id == pid
        assert provider.identity() == pid


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


def test_factory_bounds_and_deduplicates_discovered_catalog(monkeypatch):
    from app.providers.nvidia_client import NvidiaClient

    monkeypatch.setattr(settings, "nvidia_api_key", "sk-test")
    oversized = ["duplicate", "duplicate", None, 42, "x" * 1025]
    oversized.extend(f"model-{index}" for index in range(MAX_PROVIDER_MODELS + 5))
    monkeypatch.setattr(
        NvidiaClient,
        "list_models",
        lambda self, provider: oversized,
    )

    provider = build_runtime_provider(PROVIDER_REGISTRY["nvidia"])

    assert len(provider.models) == MAX_PROVIDER_MODELS
    assert provider.models[0] == "duplicate"
    assert len(set(provider.models)) == len(provider.models)
    assert all(isinstance(model, str) for model in provider.models)


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


@pytest.mark.parametrize("provider_id", sorted(RUNTIME_READY))
def test_runtime_provider_exposes_proxy_request_kwargs_method(provider_id):
    from app.providers.openai_compat_client import proxy_request_kwargs

    defn = PROVIDER_REGISTRY[provider_id]
    client = defn.client()
    provider = defn.build_provider(
        api_key="sk-test", base_url=defn.base_url_default
    )
    url = "https://provider.invalid/v1/chat/completions"

    assert hasattr(client, "proxy_request_kwargs")
    assert callable(client.proxy_request_kwargs)
    assert (
        client.proxy_request_kwargs(provider, url)
        == proxy_request_kwargs(provider, url)
    )


@pytest.mark.parametrize("provider_id", sorted(RUNTIME_READY))
def test_runtime_provider_exposes_connectivity_probe(provider_id, monkeypatch):
    defn = PROVIDER_REGISTRY[provider_id]
    client = defn.client()
    probe = client.connectivity_probe

    assert callable(probe)

    class _Resp:
        status_code = 200

    import sys

    client_module = sys.modules[type(client).connectivity_probe.__module__]
    monkeypatch.setattr(
        client_module,
        "bounded_get",
        lambda *args, **kwargs: _Resp(),
    )

    provider = defn.build_provider(
        api_key="sk-test", base_url=defn.base_url_default
    )

    ok, details, latency = probe(provider)

    assert ok is True
    assert details == "HTTP 200"
    assert isinstance(latency, int)


class _StaticKeyring:
    """
    Stub keyring returning a fixed value; used to keep keyring-path tests
    hermetic and off the real OS credential store.
    """

    def __init__(self, value):
        self.value = value

    def get(self, provider_id):
        return self.value


class TestProviderKeyResolution:
    def test_resolve_disabled_keyring_uses_env_only(self, monkeypatch):
        monkeypatch.setattr(settings, "relay_keyring_enabled", False)
        monkeypatch.setattr(settings, "nvidia_api_key", "env-secret")

        class _Stub:
            def get(self, provider_id):
                raise AssertionError("keyring consulted while disabled")

        monkeypatch.setattr(factory_module, "provider_key_store", _Stub())

        resolved = factory_module.resolve_provider_key(
            PROVIDER_REGISTRY["nvidia"]
        )

        assert resolved == "env-secret"

    def test_resolve_keyring_entry_wins_over_env(self, monkeypatch):
        monkeypatch.setattr(settings, "relay_keyring_enabled", True)
        monkeypatch.setattr(settings, "nvidia_api_key", "env-secret")

        def get(self, provider_id):
            assert provider_id == "nvidia"
            return "keyring-secret"

        monkeypatch.setattr(
            factory_module,
            "provider_key_store",
            _StaticKeyring("keyring-secret"),
        )

        resolved = factory_module.resolve_provider_key(
            PROVIDER_REGISTRY["nvidia"]
        )

        assert resolved == "keyring-secret"

    def test_resolve_keyring_absent_falls_back_to_env(self, monkeypatch):
        monkeypatch.setattr(settings, "relay_keyring_enabled", True)
        monkeypatch.setattr(settings, "nvidia_api_key", "env-secret")
        monkeypatch.setattr(factory_module, "provider_key_store", _StaticKeyring(""))

        resolved = factory_module.resolve_provider_key(
            PROVIDER_REGISTRY["nvidia"]
        )

        assert resolved == "env-secret"

    def test_resolve_keyring_unavailable_falls_back_to_env(self, monkeypatch):
        monkeypatch.setattr(settings, "relay_keyring_enabled", True)
        monkeypatch.setattr(settings, "nvidia_api_key", "env-secret")

        class _Stub:
            def get(self, provider_id):
                raise RuntimeError("keyring unavailable")

        monkeypatch.setattr(factory_module, "provider_key_store", _Stub())

        resolved = factory_module.resolve_provider_key(
            PROVIDER_REGISTRY["nvidia"]
        )

        assert resolved == "env-secret"

    def test_resolve_keyless_provider_returns_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "relay_keyring_enabled", True)

        class _Stub:
            def get(self, provider_id):
                raise AssertionError("keyless provider must not touch keyring")

        monkeypatch.setattr(factory_module, "provider_key_store", _Stub())

        assert (
            factory_module.resolve_provider_key(PROVIDER_REGISTRY["ollama"]) == ""
        )

    def test_build_runtime_provider_uses_resolved_key(self, monkeypatch):
        from app.providers.nvidia_client import NvidiaClient

        monkeypatch.setattr(settings, "relay_keyring_enabled", True)
        monkeypatch.setattr(settings, "nvidia_api_key", "env-secret")
        monkeypatch.setattr(
            factory_module, "provider_key_store", _StaticKeyring("keyring-secret")
        )
        monkeypatch.setattr(
            NvidiaClient,
            "list_models",
            lambda self, provider: ["a", "b"],
        )

        provider = build_runtime_provider(PROVIDER_REGISTRY["nvidia"])

        assert provider.api_key == "keyring-secret"
        assert provider.models == ["a", "b"]

    def test_build_local_provider_uses_keyring_entry(self, monkeypatch):
        from app.providers.lmstudio_client import LMStudioClient

        monkeypatch.setattr(settings, "relay_keyring_enabled", True)
        monkeypatch.setattr(settings, "lmstudio_api_key", "")
        monkeypatch.setattr(
            factory_module, "provider_key_store", _StaticKeyring("lmstudio-vault")
        )
        monkeypatch.setattr(
            LMStudioClient,
            "list_models",
            lambda self, provider: ["llama-1"],
        )

        provider = build_runtime_provider(PROVIDER_REGISTRY["lmstudio"])

        assert provider.api_key == "lmstudio-vault"
        assert provider.models == ["llama-1"]
