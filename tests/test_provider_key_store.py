"""
Unit tests for the OS-keyring-backed ProviderKeyStore (P5 Phase 1).

Uses a fake in-memory backend installed via ``keyring.set_keyring`` so no
real OS credential store is touched, plus a fail-backend override test for
the unavailable-keyring failure behavior.
"""

import keyring
import pytest

from app.services.provider_key_store import (
    SERVICE_NAME,
    ProviderKeyStore,
    _configured_backend,
    provider_key_store,
)


class FakeKeyring(keyring.backend.KeyringBackend):
    """
    In-memory keyring backend recording ``(service, username)`` keys.
    """

    @classmethod
    def priority(cls):
        return 10

    def __init__(self):
        self._data = {}

    def get_password(self, service, username):
        return self._data.get((service, username))

    def set_password(self, service, username, password):
        self._data[(service, username)] = password

    def delete_password(self, service, username):
        try:
            del self._data[(service, username)]
        except KeyError:
            raise keyring.errors.PasswordDeleteError(username)


@pytest.fixture
def fake_keyring():
    backend = FakeKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(keyring.backends.fail.Keyring())


def test_set_get_roundtrip(fake_keyring):
    store = ProviderKeyStore()
    store.set("nvidia", "nv-key")
    assert store.get("nvidia") == "nv-key"


def test_get_missing_returns_empty(fake_keyring):
    store = ProviderKeyStore()
    assert store.get("openai") == ""


def test_remove_deletes_entry(fake_keyring):
    store = ProviderKeyStore()
    store.set("nvidia", "nv-key")
    store.remove("nvidia")
    assert store.get("nvidia") == ""


def test_remove_missing_is_idempotent(fake_keyring):
    store = ProviderKeyStore()
    store.remove("nvidia")  # must not raise


def test_uses_relay_service_and_provider_username(fake_keyring):
    store = ProviderKeyStore()
    store.set("nvidia", "nv-key")
    assert (SERVICE_NAME, "nvidia") in fake_keyring._data


def test_provider_ids_are_independent(fake_keyring):
    store = ProviderKeyStore()
    store.set("nvidia", "nv-key")
    store.set("openai", "oa-key")
    assert store.get("nvidia") == "nv-key"
    assert store.get("openai") == "oa-key"


def test_custom_service_name(fake_keyring):
    store = ProviderKeyStore(service="custom")
    store.set("nvidia", "nv-key")
    assert ("custom", "nvidia") in fake_keyring._data
    assert ("relay", "nvidia") not in fake_keyring._data


def test_override_backend_env_set_raises(monkeypatch):
    monkeypatch.setenv("RELAY_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    store = ProviderKeyStore()

    with pytest.raises(Exception):
        store.set("nvidia", "nv-key")


def test_override_backend_env_get_returns_empty(monkeypatch):
    monkeypatch.setenv("RELAY_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    store = ProviderKeyStore()
    assert store.get("nvidia") == ""


def test_get_failure_records_diagnostics(monkeypatch):
    monkeypatch.setenv("RELAY_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    store = ProviderKeyStore()

    assert store.get("nvidia") == ""  # recovery behavior preserved
    health = store.diagnostics()

    assert health["ok"] is False
    assert health["error"] is not None
    assert "nvidia" in health["error"]
    assert "Keyring" in health["error"]
    assert health["error_age_ms"] is not None


def test_get_failure_logs_warning(monkeypatch, caplog):
    monkeypatch.setenv("RELAY_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    store = ProviderKeyStore()

    with caplog.at_level("WARNING", logger="relay"):
        store.get("nvidia")

    assert any(
        record.levelname == "WARNING"
        and "keyring read" in record.getMessage()
        and "nvidia" in record.getMessage()
        for record in caplog.records
    )


def test_get_success_clears_diagnostics(monkeypatch):
    monkeypatch.setenv("RELAY_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    store = ProviderKeyStore()
    assert store.get("nvidia") == ""
    assert store.diagnostics()["ok"] is False

    monkeypatch.setenv("RELAY_KEYRING_BACKEND", "")
    monkeypatch.setattr(
        "app.services.provider_key_store.keyring",
        _FakeBackendModule(),
    )

    assert store.get("nvidia") == ""
    assert store.diagnostics()["ok"] is True
    assert store.last_error is None


class _FakeBackendModule:
    """Minimal keyring-module stand-in for the success-path test."""

    def get_password(self, service, username):
        return None

    def set_keyring(self, backend):
        pass


def test_configured_backend_none_by_default(monkeypatch):
    monkeypatch.delenv("RELAY_KEYRING_BACKEND", raising=False)
    assert _configured_backend() is None


def test_configured_backend_parses_override(monkeypatch):
    monkeypatch.setenv(
        "RELAY_KEYRING_BACKEND", "keyring.backends.fail.Keyring"
    )
    assert isinstance(_configured_backend(), keyring.backends.fail.Keyring)


def test_configured_backend_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("RELAY_KEYRING_BACKEND", "no.such.Module.Class")
    assert _configured_backend() is None


def test_default_instance_uses_fake_backend(fake_keyring):
    assert provider_key_store.get("nvidia") == ""
    provider_key_store.set("nvidia", "nv-key")
    assert provider_key_store.get("nvidia") == "nv-key"
