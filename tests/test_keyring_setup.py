"""
P6.2 keyring-aware setup detection tests (G6 / D5).

A keyring-only install (post ``relay provider keys migrate``) must be
detected as configured: ``_has_usable_provider`` sees the keyring value
through ``resolve_provider_key``, and the wizard's existing-key check
offers the keyring key instead of prompting for a fresh one.
"""

import pytest

from app.core.config import settings
from app.providers.registry import PROVIDER_REGISTRY


class _FakeKeyring:
    """In-memory ``provider_key_store`` returning a fixed value per id."""

    def __init__(self, values):
        self._values = dict(values)

    def get(self, provider_id):
        return self._values.get(provider_id, "")


def _enable_keyring(monkeypatch, values):
    import app.providers.factory as factory

    monkeypatch.setattr(settings, "relay_keyring_enabled", True)
    monkeypatch.setattr(factory, "provider_key_store", _FakeKeyring(values))
    return factory.provider_key_store


def test_has_usable_provider_true_for_keyring_only(monkeypatch):
    monkeypatch.setattr(settings, "nvidia_enabled", True)
    monkeypatch.setattr(settings, "nvidia_api_key", "")
    _enable_keyring(monkeypatch, {"nvidia": "sk-keyring-vault"})

    from app.cli import _has_usable_provider

    assert _has_usable_provider() is True


def test_has_usable_provider_false_without_key(monkeypatch):
    monkeypatch.setattr(settings, "nvidia_enabled", True)
    monkeypatch.setattr(settings, "nvidia_api_key", "")
    _enable_keyring(monkeypatch, {})

    from app.cli import _has_usable_provider

    assert _has_usable_provider() is False


def test_config_configured_true_for_keyring_only(monkeypatch):
    monkeypatch.setattr(settings, "nvidia_enabled", True)
    monkeypatch.setattr(settings, "nvidia_api_key", "")
    _enable_keyring(monkeypatch, {"nvidia": "sk-keyring-vault"})

    import app.cli as cli_module

    monkeypatch.setattr(cli_module, "read_setup_state", lambda: "configured")

    assert cli_module._config_configured() is True


def test_config_configured_false_when_not_marked(monkeypatch):
    monkeypatch.setattr(settings, "nvidia_enabled", True)
    _enable_keyring(monkeypatch, {"nvidia": "sk-keyring-vault"})

    import app.cli as cli_module

    monkeypatch.setattr(cli_module, "read_setup_state", lambda: "incomplete")

    assert cli_module._config_configured() is False


def test_resolve_provider_key_prefers_keyring(monkeypatch):
    monkeypatch.setattr(settings, "nvidia_api_key", "env-secret")
    _enable_keyring(monkeypatch, {"nvidia": "keyring-secret"})

    from app.providers.factory import resolve_provider_key

    assert resolve_provider_key(PROVIDER_REGISTRY["nvidia"]) == "keyring-secret"


# ------------------------------------------------------------- wizard


def make_client(key_status=200, models=("m1", "m2", "m3")):
    models = list(models)

    class FakeClient:
        def key_check(self, provider):
            return key_status, "ok" if key_status == 200 else "invalid key"

        def list_models(self, provider):
            return list(models)

        def probe_model(self, provider, model):
            from app.providers.availability import AVAILABLE, ModelProbe

            return ModelProbe(True, 5, 200, "") if AVAILABLE else None

    return FakeClient


def make_defn(provider_id="openai", name="OpenAI"):
    from app.providers.registry import ProviderDefinition

    return ProviderDefinition(
        id=provider_id,
        display_name=name,
        provider_name=name,
        kind="cloud",
        requires_api_key=True,
        key_env="FAKE_KEY",
        enabled_env="FAKE_ENABLED",
        key_attr="fake_key",
        enabled_attr="fake_enabled",
        base_url_env=None,
        base_url_default="http://fake/v1",
        priority_env="FAKE_PRIORITY",
        health_endpoint="/models",
        client_class=make_client(),
        runtime_priority=1,
    )


class FakeStore:
    def __init__(self):
        self.writes = []

    def set_provider_config(self, defn, **kwargs):
        self.writes.append((defn.id, kwargs))

    def get_env(self, key, default=""):
        return ""


@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    from app.services import setup_state
    from app.services import platform_store
    from app.setup import persistence

    monkeypatch.setattr(setup_state, "state_dir", tmp_path)
    monkeypatch.setattr(persistence, "state_dir", tmp_path)
    monkeypatch.setattr(platform_store, "state_dir", tmp_path)
    return tmp_path


def test_wizard_offers_keyring_key_as_existing(isolated_state, monkeypatch):
    from app.setup.ui import ScriptedUI
    from app.setup.wizard import run_setup

    import app.services.provider_key_store as pks_module

    _enable_keyring(monkeypatch, {"openai": "sk-keyring-vault"})
    monkeypatch.setattr(
        pks_module, "provider_key_store", _FakeKeyring({"openai": "sk-keyring-vault"})
    )

    menu = [make_defn()]
    store = FakeStore()
    ui = ScriptedUI([1, "y", "n", "n", 2])

    result = run_setup(ui, menu=menu, store=store)

    assert result.usable
    assert result.state == "configured"
    assert store.writes == [
        ("openai", {"enabled": True, "api_key": "sk-keyring-vault"})
    ]
    # The first yes/no answer accepted the existing key (keep-it path was
    # taken) and the keyring value was never re-entered by the user.
    assert ui.answers[:2] == [1, "y"]
    assert "sk-keyring-vault" not in str(ui.answers[2:])
