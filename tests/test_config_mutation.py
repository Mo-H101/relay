"""
Service tests for P7.2 controlled configuration mutation.

Hermetic: the single writer (``config_store.env_file``) and the reload path
(``app.core.config.env_file``) both point at a temp ``.env``, the settings
singleton and the process environment are restored after every test, and
no test imports or exercises ``app.core.relay``.
"""

import os

import pytest

from app.core import config as config_module
from app.core.config import settings
from app.core.config_spec import SPEC_BY_ENV
from app.services import config_mutation, config_store
from app.services.config_mutation import (
    ConfigMutationError,
    ConfigUsageError,
    reload_settings_report,
    set_setting,
    unset_setting,
)


@pytest.fixture
def env_file(monkeypatch, tmp_path):
    """
    Hermetic .env: point the single writer and the reload path at a temp
    file, and restore the settings singleton plus the process environment
    afterwards so no test leaks state into the rest of the session.
    """
    path = tmp_path / ".env"
    monkeypatch.setattr(config_store, "env_file", path)
    monkeypatch.setattr(config_module, "env_file", path)

    before_env = dict(os.environ)
    before_settings = dict(settings.__dict__)

    yield path

    for key in list(os.environ):
        if key not in before_env:
            os.environ.pop(key, None)
        else:
            os.environ[key] = before_env[key]

    settings.__dict__.clear()
    settings.__dict__.update(before_settings)


def _write(path, pairs):
    path.write_text("".join(f"{key}={value}\n" for key, value in pairs.items()))


def _raising_reload(*args, **kwargs):
    raise ValueError("Invalid value for REQUEST_TIMEOUT: boom")


class _KeyringSpy:
    def __init__(self):
        self.set_calls = []
        self.remove_calls = []

    def get(self, provider_id):
        return ""

    def set(self, provider_id, value):
        self.set_calls.append((provider_id, value))

    def remove(self, provider_id):
        self.remove_calls.append(provider_id)


# ---------------------------------------------------------------------- set

def test_set_persists_valid_value(env_file):
    report = set_setting("REQUEST_TIMEOUT", "200", reload=False)

    assert report["saved"] is True
    assert report["env"] == "REQUEST_TIMEOUT"
    assert report["effect"] == "live"
    assert report["reloaded"] is False
    assert "REQUEST_TIMEOUT='200'" in env_file.read_text(encoding="utf-8")


def test_set_live_field_applies_in_process(env_file):
    report = set_setting("REQUEST_TIMEOUT", "200")

    assert report["reloaded"] is True
    assert report["applied"] is True
    assert settings.request_timeout == 200


def test_set_restart_field_reports_no_in_process_apply(env_file):
    report = set_setting("RELAY_PORT", "9000", reload=False)

    assert report["saved"] is True
    assert report["effect"] == "restart"
    assert report["reloaded"] is False
    assert report["applied"] is False
    assert "RELAY_PORT='9000'" in env_file.read_text(encoding="utf-8")


def test_set_invalid_value_rejected_without_writing(env_file):
    with pytest.raises(ConfigUsageError) as exc:
        set_setting("REQUEST_TIMEOUT", "abc")

    assert "Invalid value for REQUEST_TIMEOUT" in str(exc.value)
    assert "abc" not in str(exc.value)
    assert not env_file.exists()


def test_set_invalid_float_below_minimum_rejected(env_file):
    with pytest.raises(ConfigUsageError) as exc:
        set_setting("HEALTH_FRESHNESS_EXPONENT", "-1.0")

    assert "Invalid value for HEALTH_FRESHNESS_EXPONENT" in str(exc.value)
    assert not env_file.exists()


def test_set_unknown_env_refused(env_file):
    with pytest.raises(ConfigUsageError) as exc:
        set_setting("NOT_A_REAL_SETTING", "x")

    assert "Unknown setting 'NOT_A_REAL_SETTING'" in str(exc.value)
    assert not env_file.exists()


def test_set_informational_field_refused(env_file):
    with pytest.raises(ConfigUsageError) as exc:
        set_setting("relay_name", "x")

    assert "'relay_name' cannot be set." in str(exc.value)
    assert not env_file.exists()


def test_set_preserves_unrelated_lines(env_file):
    env_file.write_text("MAX_RETRIES=3\nCOMMENT=yes\n", encoding="utf-8")

    set_setting("REQUEST_TIMEOUT", "200", reload=False)

    text = env_file.read_text(encoding="utf-8")
    assert "COMMENT=yes" in text
    assert "MAX_RETRIES=3" in text
    assert "REQUEST_TIMEOUT='200'" in text


# --------------------------------------------------------------- secrets

def test_set_report_never_contains_secret(monkeypatch, env_file):
    monkeypatch.setattr(settings, "relay_keyring_enabled", False)

    report = set_setting("OPENAI_API_KEY", "sk-secret-value", reload=False)

    assert report["saved"] is True
    assert "sk-secret-value" not in str(report)


def test_set_secret_dry_run_preview_is_masked(env_file):
    report = set_setting("OPENAI_API_KEY", "sk-secret-value", dry_run=True)

    assert report["new"] != "sk-secret-value"
    assert "sk-secret-value" not in report["new"]
    assert not env_file.exists()


def test_set_provider_key_writes_env_when_keyring_disabled(monkeypatch, env_file):
    monkeypatch.setattr(settings, "relay_keyring_enabled", False)

    report = set_setting("OPENAI_API_KEY", "sk-new", reload=False)

    assert report["saved"] is True
    assert "OPENAI_API_KEY='sk-new'" in env_file.read_text(encoding="utf-8")


def test_set_provider_key_routes_through_keyring_when_enabled(
    monkeypatch, env_file
):
    monkeypatch.setattr(settings, "relay_keyring_enabled", True)
    spy = _KeyringSpy()
    monkeypatch.setattr(config_store, "provider_key_store", spy)

    report = set_setting("OPENAI_API_KEY", "sk-new", reload=False)

    assert report["saved"] is True
    assert spy.set_calls == [("openai", "sk-new")]
    assert spy.remove_calls == []
    assert not env_file.exists()


def test_unset_provider_key_routes_through_keyring_when_enabled(
    monkeypatch, env_file
):
    monkeypatch.setattr(settings, "relay_keyring_enabled", True)
    spy = _KeyringSpy()
    monkeypatch.setattr(config_store, "provider_key_store", spy)

    report = unset_setting("OPENAI_API_KEY", reload=False)

    assert report["saved"] is True
    assert spy.set_calls == []
    assert spy.remove_calls == ["openai"]
    assert not env_file.exists()


# ------------------------------------------------------------- dry run

def test_set_dry_run_does_not_write(env_file):
    env_file.write_text("MAX_RETRIES=3\n", encoding="utf-8")

    report = set_setting("MAX_RETRIES", "9", dry_run=True)

    assert report["saved"] is False
    assert report["dry_run"] is True
    assert report["would_reload"] is True
    text = env_file.read_text(encoding="utf-8")
    assert "MAX_RETRIES=3" in text
    assert "9" not in text


def test_set_dry_run_reports_would_not_reload_with_no_reload(env_file):
    report = set_setting("MAX_RETRIES", "9", reload=False, dry_run=True)

    assert report["would_reload"] is False


def test_unset_dry_run_does_not_write(env_file):
    config_store.set_env("MAX_RETRIES", "9")

    report = unset_setting("MAX_RETRIES", dry_run=True)

    assert report["saved"] is False
    assert "MAX_RETRIES='9'" in env_file.read_text(encoding="utf-8")


def test_set_dry_run_invalid_value_still_rejected(env_file):
    with pytest.raises(ConfigUsageError):
        set_setting("REQUEST_TIMEOUT", "abc", dry_run=True)

    assert not env_file.exists()


# --------------------------------------------------------------- rollback

def test_set_reload_failure_restores_previous_value(monkeypatch, env_file):
    config_store.set_env("REQUEST_TIMEOUT", "100")
    monkeypatch.setattr(config_mutation, "_apply_settings_reload", _raising_reload)

    with pytest.raises(ConfigMutationError) as exc:
        set_setting("REQUEST_TIMEOUT", "250")

    assert getattr(exc.value, "restored", False) is True
    assert "Invalid value for REQUEST_TIMEOUT" in str(exc.value)
    text = env_file.read_text(encoding="utf-8")
    assert "REQUEST_TIMEOUT='100'" in text
    assert "250" not in text


def test_set_reload_failure_restores_default_when_nothing_was_set(
    monkeypatch, env_file
):
    monkeypatch.setattr(config_mutation, "_apply_settings_reload", _raising_reload)

    with pytest.raises(ConfigMutationError):
        set_setting("REQUEST_TIMEOUT", "250")

    assert "REQUEST_TIMEOUT" not in env_file.read_text(encoding="utf-8")


def test_unset_reload_failure_restores_previous_value(monkeypatch, env_file):
    config_store.set_env("REQUEST_TIMEOUT", "100")
    monkeypatch.setattr(config_mutation, "_apply_settings_reload", _raising_reload)

    with pytest.raises(ConfigMutationError) as exc:
        unset_setting("REQUEST_TIMEOUT")

    assert getattr(exc.value, "restored", False) is True
    assert "REQUEST_TIMEOUT='100'" in env_file.read_text(encoding="utf-8")


def test_set_reload_failure_restores_settings_snapshot(monkeypatch, env_file):
    monkeypatch.setattr(config_mutation, "_apply_settings_reload", _raising_reload)
    before = dict(settings.__dict__)

    with pytest.raises(ConfigMutationError):
        set_setting("REQUEST_TIMEOUT", "250")

    assert settings.__dict__ == before


# ------------------------------------------------------------------- unset

def test_unset_removes_key(env_file):
    config_store.set_env("MAX_RETRIES", "9")

    report = unset_setting("MAX_RETRIES", reload=False)

    assert report["saved"] is True
    assert "MAX_RETRIES" not in env_file.read_text(encoding="utf-8")


def test_unset_absent_key_is_idempotent(env_file):
    report = unset_setting("MAX_RETRIES", reload=False)

    assert report["saved"] is True


def test_unset_restores_default_in_process(env_file):
    config_store.set_env("MAX_RETRIES", "9")

    report = unset_setting("MAX_RETRIES")

    assert report["reloaded"] is True
    assert settings.max_retries == 1


# ------------------------------------------------------------------ reload

def test_reload_report_lists_applied_and_unchanged(env_file):
    _write(env_file, {"MAX_RETRIES": "9"})

    report = reload_settings_report()

    assert report["reloaded"] is True
    assert "max_retries" in report["applied"]
    assert settings.max_retries == 9


def test_reload_report_dry_run_mutates_nothing(env_file):
    _write(env_file, {"MAX_RETRIES": "9"})
    before = settings.max_retries

    report = reload_settings_report(dry_run=True)

    assert report["dry_run"] is True
    assert "max_retries" in report["applied"]
    assert settings.max_retries == before


def test_reload_report_invalid_file_raises_redacted(env_file):
    _write(env_file, {"REQUEST_TIMEOUT": "abc"})

    with pytest.raises(ValueError) as exc:
        reload_settings_report()

    assert "Invalid value for REQUEST_TIMEOUT" in str(exc.value)
    assert "abc" not in str(exc.value)


def test_reload_report_invalid_file_dry_run_raises_redacted(env_file):
    _write(env_file, {"REQUEST_TIMEOUT": "abc"})

    with pytest.raises(ValueError) as exc:
        reload_settings_report(dry_run=True)

    assert "Invalid value for REQUEST_TIMEOUT" in str(exc.value)
    assert "abc" not in str(exc.value)


def test_reload_report_failed_reload_restores_settings(env_file):
    _write(env_file, {"REQUEST_TIMEOUT": "abc"})
    before = dict(settings.__dict__)

    with pytest.raises(ValueError):
        reload_settings_report()

    assert settings.__dict__ == before


def test_reload_path_never_imports_relay(env_file, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "app.core.relay":
            raise AssertionError("app.core.relay must not be imported")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)

    report = reload_settings_report(dry_run=True)

    assert "applied" in report
