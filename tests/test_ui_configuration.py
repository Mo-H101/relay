"""
Configuration screen tests (Feature A of P2d, P7.3).

Covers: the derived full-surface form (every setting, secret fields as
masked read-only rows), live vs restart-required vs informational
classification, the Save -> Validate -> Write -> Apply -> Confirm flow
through the P7.2 mutation layer, restart-only saves never being
live-applied, rollback to previous values when apply fails, and the
best-effort audit events for TUI writes.
"""

import pytest

from app.services import config_store
from app.setup.key_validation import mask_key
from app.ui.data import ServiceFacade
from tests.ui_fakes import FakeRelay, FakeReloader, FakeStore


def _facade(store=None, reloader=None) -> ServiceFacade:
    return ServiceFacade(
        relay_instance=FakeRelay(),
        store=store or FakeStore(),
        reloader=reloader or FakeReloader(),
    )


# --------------------------------------------------------------- form shape

def test_config_form_exposes_every_secret_as_masked_read_only_row():
    fields = {f.env: f for f in _facade().config_form()}
    for env in (
        "NVIDIA_API_KEY",
        "OPENAI_API_KEY",
        "RELAY_API_KEY",
    ):
        assert env in fields, env
        assert fields[env].secret, env
        assert not fields[env].editable, env


def test_config_form_secret_values_are_masked_never_raw():
    store = FakeStore()
    store._values["RELAY_API_KEY"] = "sk-super-secret-material"
    fields = {f.env: f for f in _facade(store=store).config_form()}
    value = fields["RELAY_API_KEY"].value
    assert value == mask_key("sk-super-secret-material")
    assert "super-secret-material" not in value


def test_config_form_classification():
    fields = {f.env: f for f in _facade().config_form()}
    assert fields["TASK_CODING"].editable
    assert fields["TASK_CODING"].reloadable
    assert not fields["TASK_CODING"].restart_required
    assert fields["TASK_CODING"].kind == "csv"

    assert fields["RELAY_HOST"].restart_required
    assert not fields["RELAY_HOST"].reloadable
    assert fields["RELAY_HOST"].editable

    assert fields["RETRY_BACKOFF_BASE_SECONDS"].editable
    assert fields["RETRY_BACKOFF_BASE_SECONDS"].reloadable
    assert not fields["RETRY_BACKOFF_BASE_SECONDS"].restart_required


def test_restart_required_fields_are_editable_but_never_live():
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
        assert fields[env].editable, env
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


# ------------------------------------------------------------------- save

def test_save_config_live_reload_success(monkeypatch, tmp_path):
    monkeypatch.setattr(config_store, "env_file", tmp_path / ".env")
    calls = []

    def fake_reload(relay, **kwargs):
        calls.append(kwargs)
        return {
            "reloaded": True,
            "dry_run": False,
            "applied": ["task_coding"],
            "unchanged": ["max_retries"],
            "failures": [],
        }

    facade = _facade(store=config_store, reloader=fake_reload)

    report = facade.save_config({"TASK_CODING": "nvidia:model-x"})

    assert report["saved"] is True
    assert report["applied"] == ["task_coding"]
    assert len(calls) == 1
    assert not calls[0].get("dry_run")
    assert calls[0].get("dotenv_path") == str(tmp_path / ".env")
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "nvidia:model-x" in text


def test_save_config_invalid_value_is_refused_with_zero_writes():
    facade = _facade()

    report = facade.save_config({"REQUEST_TIMEOUT": "not-an-int"})

    assert report["saved"] is False
    assert "Invalid value for 'REQUEST_TIMEOUT'" in report["error"]
    assert not facade._store.env_writes


def test_save_config_unknown_field_is_refused_with_zero_writes():
    facade = _facade()

    report = facade.save_config({"RELAY_NAME": "anything"})

    assert report["saved"] is False
    assert "RELAY_NAME" in report["error"]
    assert not facade._store.env_writes


def test_save_config_secret_field_is_refused_with_zero_writes():
    facade = _facade()

    report = facade.save_config({"RELAY_API_KEY": "a-secret"})

    assert report["saved"] is False
    assert "RELAY_API_KEY" in report["error"]
    assert not facade._store.env_writes


def test_save_config_apply_failure_restores_previous_values():
    store = FakeStore()
    store._values["MAX_RETRIES"] = "1"
    failing = FakeReloader(
        report={
            "reloaded": False,
            "dry_run": False,
            "applied": [],
            "unchanged": [],
            "failures": [],
            "error_kind": "apply",
            "error": "boom",
        }
    )

    facade = _facade(store=store, reloader=failing)

    report = facade.save_config({"MAX_RETRIES": "5"})

    assert report["saved"] is False
    assert "boom" in report["error"]
    assert store._values["MAX_RETRIES"] == "1"
    assert ("MAX_RETRIES", "5") in store.env_writes


def test_save_config_restart_only_save_is_written_never_applied():
    store = FakeStore()
    reloader = FakeReloader()
    facade = _facade(store=store, reloader=reloader)

    report = facade.save_config({"RELAY_HOST": "0.0.0.0"})

    assert report["saved"] is True
    assert report["applied"] is False
    assert report["restart_required"] == ["RELAY_HOST"]
    assert not reloader.calls
    assert ("RELAY_HOST", "0.0.0.0") in store.env_writes


def test_save_config_mixed_live_and_restart_applies_live_only():
    store = FakeStore()
    reloader = FakeReloader()
    facade = _facade(store=store, reloader=reloader)

    report = facade.save_config(
        {"TASK_CODING": "nvidia:model-x", "LOG_LEVEL": "DEBUG"}
    )

    assert report["saved"] is True
    assert len(reloader.calls) == 1
    assert report["restart_required"] == ["LOG_LEVEL"]
    assert ("LOG_LEVEL", "DEBUG") in store.env_writes
    assert ("TASK_CODING", "nvidia:model-x") in store.env_writes


def test_save_config_no_changes_is_noop():
    facade = _facade()
    report = facade.save_config({})
    assert report["saved"] is False
    assert "No changes" in report["error"]


def test_save_config_reports_unchanged_fields():
    reloader = FakeReloader(
        report={
            "reloaded": True,
            "dry_run": False,
            "applied": [],
            "unchanged": ["max_retries"],
            "failures": [],
        }
    )
    report = _facade(reloader=reloader).save_config({"MAX_RETRIES": "1"})
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


# ------------------------------------------------------------------- audit

def test_save_config_emits_audit_events(isolated_event_log):
    facade = _facade()

    report = facade.save_config({"TASK_CODING": "nvidia:model-x"})

    assert report["saved"] is True
    sets = isolated_event_log.query(action="config.set", limit=20)
    reloads = isolated_event_log.query(action="config.reload", limit=20)

    assert any(row["actor"] == "tui" and row["outcome"] == "ok" for row in sets)
    assert any(row["actor"] == "tui" and row["outcome"] == "ok" for row in reloads)


def test_save_config_audit_events_never_contain_values(isolated_event_log):
    facade = _facade()

    facade.save_config({"TASK_CODING": "nvidia:model-x"})

    for row in isolated_event_log.query(limit=50):
        detail = row["detail"]
        text = " ".join(map(str, detail.values()))
        assert "nvidia:model-x" not in text


def test_save_config_failure_emits_failed_audit(isolated_event_log):
    facade = _facade()

    report = facade.save_config({"REQUEST_TIMEOUT": "not-an-int"})

    assert report["saved"] is False
    failed = isolated_event_log.query(action="config.set", outcome="failed", limit=20)
    assert any(row["actor"] == "tui" for row in failed)


def test_restart_only_save_emits_set_but_no_reload_audit(isolated_event_log):
    facade = _facade()

    report = facade.save_config({"RELAY_HOST": "0.0.0.0"})

    assert report["saved"] is True
    assert isolated_event_log.query(action="config.set", outcome="ok", limit=20)
    assert not isolated_event_log.query(action="config.reload", limit=20)


# ------------------------------------------------------------------- smoke

@pytest.mark.asyncio
async def test_configuration_screen_smoke():
    from textual.widgets import Input, Static

    from app.ui.app import RelayApp

    app = RelayApp(facade=_facade(), start_server=False)

    async with app.run_test(
        headless=True, size=(120, 40), notifications=False
    ) as pilot:
        await pilot.press("5")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen.query_one("#cfg-TASK_CODING"), Input)
        assert screen.query_one("#cfg-TASK_CODING").disabled is False
        assert isinstance(screen.query_one("#cfg-RELAY_HOST"), Input)
        assert screen.query_one("#cfg-RELAY_HOST").disabled is False
        secret = screen.query_one("#cfg-RELAY_API_KEY")
        assert isinstance(secret, Static)
        assert "sk-" not in str(secret.render())
        await pilot.press("q")
        await pilot.pause()
