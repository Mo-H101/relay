"""
Configuration screen tests (Feature A of P2d).

Covers: form shape (no secrets), live-reload vs restart-required vs
informational classification, the Save -> Validate -> Apply -> Confirm
flow, and rollback to previous values when reload validation or apply
fails.
"""

import pytest

from app.services import config_store
from app.ui.data import ServiceFacade
from tests.ui_fakes import FakeRelay, FakeStore, FakeReloader


def _facade(store=None, reloader=None) -> ServiceFacade:
    return ServiceFacade(
        relay_instance=FakeRelay(),
        store=store or FakeStore(),
        reloader=reloader or FakeReloader(),
    )


def test_config_form_exposes_no_secret_fields():
    fields = _facade().config_form()
    assert fields
    envs = [f.env for f in fields]
    assert "RELAY_API_KEY" not in envs
    assert not any("SECRET" in env or "KEY" in env for env in envs)
    assert not any(
        "key" in f.label.lower() or "secret" in f.label.lower() for f in fields
    )


def test_config_form_classification():
    fields = {f.env: f for f in _facade().config_form()}
    assert fields["TASK_CODING"].editable
    assert fields["TASK_CODING"].reloadable
    assert not fields["TASK_CODING"].restart_required
    assert fields["TASK_CODING"].kind == "csv"

    assert fields["RELAY_HOST"].restart_required
    assert not fields["RELAY_HOST"].editable

    assert fields["RETRY_BACKOFF_BASE_SECONDS"].editable
    assert fields["RETRY_BACKOFF_BASE_SECONDS"].reloadable
    assert not fields["RETRY_BACKOFF_BASE_SECONDS"].restart_required


def test_restart_required_fields_are_read_only():
    fields = {f.env: f for f in _facade().config_form()}
    for env in (
        "RELAY_HOST",
        "RELAY_PORT",
        "PERSISTENCE_ENABLED",
        "PERSISTENCE_PATH",
        "PERSISTENCE_FLUSH_INTERVAL_SECONDS",
        "LOG_LEVEL",
        "LOG_FILE",
        "LMSTUDIO_BASE_URL",
    ):
        assert fields[env].restart_required, env
        assert not fields[env].editable, env
        assert not fields[env].reloadable, env


def test_live_reload_fields_are_editable():
    fields = {f.env: f for f in _facade().config_form()}
    for env in (
        "TASK_ROUTING_ENABLED",
        "CROSS_PROVIDER_MODEL_SELECTION",
        "TASK_VISION",
        "MAX_RETRIES",
        "RETRY_HONOR_RETRY_AFTER",
        "REQUEST_TIMEOUT_BUDGET_SECONDS",
    ):
        assert fields[env].editable, env
        assert fields[env].reloadable, env
        assert not fields[env].restart_required, env


def test_save_config_live_reload_success(monkeypatch, tmp_path):
    monkeypatch.setattr(config_store, "env_file", tmp_path / ".env")
    calls = []

    def fake_reload(relay, **kwargs):
        calls.append(kwargs)
        return {
            "reloaded": True,
            "dry_run": kwargs.get("dry_run", False),
            "applied": ["task_coding"],
            "unchanged": ["max_retries"],
            "failures": [],
        }

    facade = _facade(store=config_store, reloader=fake_reload)

    report = facade.save_config({"TASK_CODING": "nvidia:model-x"})

    assert report["saved"] is True
    assert report["applied"] == ["task_coding"]
    assert calls[0]["dry_run"] is True
    assert not calls[1].get("dry_run")
    assert calls[0].get("dotenv_path") == str(tmp_path / ".env")
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "nvidia:model-x" in text


def test_save_config_validation_failure_restores_previous_values(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config_store, "env_file", tmp_path / ".env")
    config_store.set_env("TASK_CODING", "old-model")

    def failing(relay, **kwargs):
        return {
            "reloaded": False,
            "dry_run": True,
            "applied": [],
            "unchanged": [],
            "failures": [],
            "error_kind": "validation",
            "error": "Invalid value for TASK_CODING",
        }

    facade = _facade(store=config_store, reloader=failing)

    report = facade.save_config({"TASK_CODING": "!!!"})

    assert report["saved"] is False
    assert "TASK_CODING" in report["error"]
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "old-model" in text
    assert "!!!" not in text


def test_save_config_apply_failure_restores_previous_values(monkeypatch, tmp_path):
    monkeypatch.setattr(config_store, "env_file", tmp_path / ".env")
    config_store.set_env("MAX_RETRIES", "1")

    def apply_fails(relay, **kwargs):
        if kwargs.get("dry_run"):
            return {
                "reloaded": True,
                "dry_run": True,
                "applied": ["max_retries"],
                "unchanged": [],
                "failures": [],
            }
        return {
            "reloaded": False,
            "dry_run": False,
            "applied": [],
            "unchanged": [],
            "failures": [],
            "error_kind": "apply",
            "error": "boom",
        }

    facade = _facade(store=config_store, reloader=apply_fails)

    report = facade.save_config({"MAX_RETRIES": "5"})

    assert report["saved"] is False
    assert "boom" in report["error"]
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "1" in text
    assert "5" not in text


def test_save_config_restart_required_rejected_without_writing():
    for env in ("RELAY_HOST", "PERSISTENCE_PATH", "LMSTUDIO_BASE_URL", "LOG_FILE"):
        facade = _facade()
        report = facade.save_config({env: "changed"})
        assert report["saved"] is False
        assert "Restart" in report["error"]
        assert not facade._store.env_writes


def test_save_config_unknown_or_secret_field_rejected():
    # Secret envs never appear in the form and are rejected on save; the
    # legacy informational field (DEFAULT_PROVIDER) was retired in P6.3.
    facade = _facade()
    report = facade.save_config({"RELAY_API_KEY": "a-secret"})
    assert report["saved"] is False
    assert "RELAY_API_KEY" in report["error"]


def test_save_config_no_changes_is_noop():
    facade = _facade()
    report = facade.save_config({})
    assert report["saved"] is False
    assert "No changes" in report["error"]


def test_save_config_reports_unchanged_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(config_store, "env_file", tmp_path / ".env")
    config_store.set_env("MAX_RETRIES", "1")

    def fake_reload(relay, **kwargs):
        return {
            "reloaded": True,
            "dry_run": kwargs.get("dry_run", False),
            "applied": [],
            "unchanged": ["max_retries"],
            "failures": [],
        }

    report = _facade(store=config_store, reloader=fake_reload).save_config(
        {"MAX_RETRIES": "1"}
    )

    assert report["saved"] is True
    assert report["unchanged"] == ["max_retries"]
    assert not report["applied"]


def test_restart_required_fields_are_listed():
    facade = _facade()
    restart = facade.config_restart_required_fields()
    assert "RELAY_HOST" in restart
    assert "LMSTUDIO_BASE_URL" in restart
    assert "TASK_CODING" not in restart
    assert "RELAY_API_KEY" not in restart


@pytest.mark.asyncio
async def test_configuration_screen_smoke():
    from app.ui.app import RelayApp

    app = RelayApp(facade=_facade(), start_server=False)

    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        await pilot.press("5")
        await pilot.pause()
        screen = app.screen
        assert screen.query_one("#cfg-TASK_CODING") is not None
        assert screen.query_one("#cfg-RELAY_HOST") is not None
        assert screen.query_one("#cfg-RELAY_HOST").disabled is True
        assert screen.query_one("#cfg-TASK_CODING").disabled is False
        await pilot.press("q")
        await pilot.pause()
