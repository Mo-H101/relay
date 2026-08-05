"""
Config store (P1): single-writer .env persistence, no key echo.
"""

import pytest

from app.core.config import settings
from app.providers.registry import PROVIDER_REGISTRY
from app.services import config_store


def _patch_env(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(config_store, "env_file", env_file)
    return env_file


def test_set_env_writes_quoted_value(monkeypatch, tmp_path):
    env_file = _patch_env(monkeypatch, tmp_path)

    config_store.set_env("FOO", "bar")

    assert "'bar'" in env_file.read_text(encoding="utf-8")


def test_env_file_user_only_after_write(monkeypatch, tmp_path):
    import os
    import stat

    if os.name == "nt":
        pytest.skip("POSIX permission check")

    env_file = _patch_env(monkeypatch, tmp_path)

    config_store.set_env("NVIDIA_API_KEY", "sk-secret")

    mode = stat.S_IMODE(os.stat(env_file).st_mode)
    assert mode == 0o600


def test_get_env_reads_process_environment(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    assert config_store.get_env("FOO") == "bar"


def test_get_env_missing_returns_default():
    assert config_store.get_env("RELAY_NO_SUCH_VAR_XYZ", "dflt") == "dflt"


def test_unset_env_removes_key(monkeypatch, tmp_path):
    env_file = _patch_env(monkeypatch, tmp_path)
    config_store.set_env("FOO", "bar")

    config_store.unset_env("FOO")

    text = env_file.read_text(encoding="utf-8")
    assert "FOO" not in text
    assert config_store.get_env("FOO", "dflt") == "dflt"


def test_unset_env_missing_key_is_noop(monkeypatch, tmp_path):
    env_file = _patch_env(monkeypatch, tmp_path)
    config_store.set_env("FOO", "bar")

    config_store.unset_env("NOT_PRESENT_XYZ")

    assert "FOO" in env_file.read_text(encoding="utf-8")


def test_set_provider_config_writes_all_fields(monkeypatch, tmp_path):
    env_file = _patch_env(monkeypatch, tmp_path)
    defn = PROVIDER_REGISTRY["openai"]

    config_store.set_provider_config(
        defn,
        enabled=True,
        api_key="sk-secret",
        priority_models=["gpt-4o", "gpt-4o-mini"],
    )

    text = env_file.read_text(encoding="utf-8")
    assert f"{defn.enabled_env}='true'" in text
    assert f"{defn.key_env}='sk-secret'" in text
    assert f"{defn.priority_env}='gpt-4o,gpt-4o-mini'" in text


def test_set_provider_config_none_leaves_unchanged(monkeypatch, tmp_path):
    env_file = _patch_env(monkeypatch, tmp_path)
    defn = PROVIDER_REGISTRY["openai"]

    config_store.set_provider_config(defn, enabled=True)

    text = env_file.read_text(encoding="utf-8")
    assert f"{defn.enabled_env}='true'" in text
    assert defn.key_env not in text
    assert defn.priority_env not in text


def test_set_provider_config_empty_string_clears(monkeypatch, tmp_path):
    env_file = _patch_env(monkeypatch, tmp_path)
    defn = PROVIDER_REGISTRY["openai"]

    config_store.set_provider_config(defn, api_key="", priority_models=[])

    text = env_file.read_text(encoding="utf-8")
    assert f"{defn.key_env}=''" in text


def test_keyless_provider_ignores_key_arg(monkeypatch, tmp_path):
    env_file = _patch_env(monkeypatch, tmp_path)
    defn = PROVIDER_REGISTRY["ollama"]

    config_store.set_provider_config(defn, enabled=True, api_key="nope")

    assert defn.key_env is None
    text = env_file.read_text(encoding="utf-8")
    assert f"{defn.enabled_env}='true'" in text


def test_wizard_never_echoes_keys_via_store(monkeypatch, tmp_path, capsys):
    _patch_env(monkeypatch, tmp_path)

    config_store.set_provider_config(
        PROVIDER_REGISTRY["nvidia"],
        enabled=True,
        api_key="super-secret-key-1234",
    )

    out = capsys.readouterr()
    assert "super-secret-key-1234" not in out.out
    assert "super-secret-key-1234" not in out.err


class _KeyringSpy:
    def __init__(self):
        self.set_calls = []
        self.remove_calls = []

    def set(self, provider_id, value):
        self.set_calls.append((provider_id, value))

    def remove(self, provider_id):
        self.remove_calls.append(provider_id)


def test_keyring_enabled_writes_key_to_keyring_not_env(monkeypatch, tmp_path):
    env_file = _patch_env(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "relay_keyring_enabled", True)
    spy = _KeyringSpy()
    monkeypatch.setattr(config_store, "provider_key_store", spy)
    defn = PROVIDER_REGISTRY["openai"]

    config_store.set_provider_config(
        defn,
        enabled=True,
        api_key="sk-secret",
        priority_models=["gpt-4o"],
    )

    assert spy.set_calls == [("openai", "sk-secret")]
    assert spy.remove_calls == []

    text = env_file.read_text(encoding="utf-8")
    assert defn.key_env not in text
    assert f"{defn.enabled_env}='true'" in text
    assert f"{defn.priority_env}='gpt-4o'" in text


def test_keyring_enabled_empty_string_removes_key(monkeypatch, tmp_path):
    env_file = _patch_env(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "relay_keyring_enabled", True)
    spy = _KeyringSpy()
    monkeypatch.setattr(config_store, "provider_key_store", spy)
    defn = PROVIDER_REGISTRY["openai"]

    config_store.set_provider_config(defn, api_key="")

    assert spy.set_calls == []
    assert spy.remove_calls == ["openai"]

    # Keyring-enabled key writes never create or touch the .env file.
    assert not env_file.exists()


def test_keyring_enabled_keyless_provider_untouched(monkeypatch, tmp_path):
    env_file = _patch_env(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "relay_keyring_enabled", True)

    class _ExplodingSpy:
        def set(self, provider_id, value):
            raise AssertionError("keyless provider must not touch keyring")

        def remove(self, provider_id):
            raise AssertionError("keyless provider must not touch keyring")

    monkeypatch.setattr(config_store, "provider_key_store", _ExplodingSpy())
    defn = PROVIDER_REGISTRY["ollama"]

    config_store.set_provider_config(defn, enabled=True, api_key="nope")

    text = env_file.read_text(encoding="utf-8")
    assert f"{defn.enabled_env}='true'" in text


def test_keyring_disabled_writes_env_and_never_keyring(monkeypatch, tmp_path):
    env_file = _patch_env(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "relay_keyring_enabled", False)

    class _ExplodingSpy:
        def set(self, provider_id, value):
            raise AssertionError("keyring consulted while disabled")

        def remove(self, provider_id):
            raise AssertionError("keyring consulted while disabled")

    monkeypatch.setattr(config_store, "provider_key_store", _ExplodingSpy())
    defn = PROVIDER_REGISTRY["openai"]

    config_store.set_provider_config(
        defn, enabled=True, api_key="sk-secret"
    )

    text = env_file.read_text(encoding="utf-8")
    assert f"{defn.key_env}='sk-secret'" in text
