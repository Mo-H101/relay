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


def test_atomic_update_preserves_unrelated_lines(monkeypatch, tmp_path):
    env_file = _patch_env(monkeypatch, tmp_path)
    env_file.write_text("KEEP=1\nFOO=old\nCOMMENT=yes\n", encoding="utf-8")

    config_store.set_env("FOO", "new")

    text = env_file.read_text(encoding="utf-8")
    assert "KEEP=1" in text
    assert "COMMENT=yes" in text
    assert "FOO='new'" in text
    assert "FOO=old" not in text


def test_atomic_update_creates_file_when_missing(monkeypatch, tmp_path):
    env_file = _patch_env(monkeypatch, tmp_path)

    config_store.set_env("FOO", "bar")

    assert env_file.exists()
    assert "FOO='bar'" in env_file.read_text(encoding="utf-8")


def test_atomic_update_leaves_no_temp_files(monkeypatch, tmp_path):
    env_file = _patch_env(monkeypatch, tmp_path)
    config_store.set_env("A", "1")

    config_store.set_env("B", "2")
    config_store.unset_env("A")

    leftovers = [
        p for p in tmp_path.iterdir()
        if p.name != env_file.name and p.name != env_file.name + ".lock"
    ]
    assert leftovers == []


def test_atomic_update_preserves_0600_on_posix(monkeypatch, tmp_path):
    import os
    import stat

    if os.name == "nt":
        pytest.skip("POSIX permission check")

    env_file = _patch_env(monkeypatch, tmp_path)
    env_file.write_text("FOO=old\n", encoding="utf-8")
    os.chmod(env_file, 0o600)

    config_store.set_env("FOO", "new")

    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600


def test_atomic_update_tightens_loose_existing_file(monkeypatch, tmp_path):
    import os
    import stat

    if os.name == "nt":
        pytest.skip("POSIX permission check")

    env_file = _patch_env(monkeypatch, tmp_path)
    env_file.write_text("FOO=old\n", encoding="utf-8")
    os.chmod(env_file, 0o644)

    config_store.set_env("FOO", "new")

    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600


def test_atomic_update_recovers_from_aborted_replace(monkeypatch, tmp_path):
    import os

    env_file = _patch_env(monkeypatch, tmp_path)
    env_file.write_text("FOO=old\n", encoding="utf-8")

    def _boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        config_store.set_env("FOO", "new")

    # Original .env untouched, no temp file left behind.
    assert env_file.read_text(encoding="utf-8") == "FOO=old\n"
    leftovers = [
        p for p in tmp_path.iterdir()
        if p.name != env_file.name and p.name != env_file.name + ".lock"
    ]
    assert leftovers == []


def test_set_env_interleaved_updates_lose_nothing(monkeypatch, tmp_path):
    env_file = _patch_env(monkeypatch, tmp_path)
    env_file.write_text("", encoding="utf-8")

    from concurrent.futures import ThreadPoolExecutor

    def set_one(key):
        config_store.set_env(key, key.lower())

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(set_one, ["K1", "K2", "K3", "K4", "K5", "K6", "K7", "K8"]))

    text = env_file.read_text(encoding="utf-8")
    for key in ["K1", "K2", "K3", "K4", "K5", "K6", "K7", "K8"]:
        assert f"{key}='{key.lower()}'" in text
