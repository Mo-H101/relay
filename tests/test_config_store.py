"""
Config store (P1): single-writer .env persistence, no key echo.
"""

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


def test_get_env_reads_process_environment(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    assert config_store.get_env("FOO") == "bar"


def test_get_env_missing_returns_default():
    assert config_store.get_env("RELAY_NO_SUCH_VAR_XYZ", "dflt") == "dflt"


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
