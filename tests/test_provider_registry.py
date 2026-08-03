"""
Provider registry integrity (P1).

The registry is the single source of truth the wizard, key validation,
config persistence, and (from P4) runtime wiring read from. These tests
guard its invariants so adding a provider stays a registry entry.
"""

from app.providers.registry import PROVIDER_MENU, PROVIDER_REGISTRY

EXPECTED_IDS = ["nvidia", "openai", "anthropic", "gemini", "lmstudio", "ollama"]


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
