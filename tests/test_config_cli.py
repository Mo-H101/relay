"""
Tests for ``relay config``: ``show``, ``validate``, ``diff`` (P7.1,
read-only) and ``set``, ``unset``, ``reload`` (P7.2).

These exercise the CLI through ``main(argv)`` + ``capsys`` exactly like the
other CLI test modules, including the masking/redaction guarantees: no raw
secret value may ever reach stdout or stderr, and ``validate`` reports
field names only. The P7.2 write tests run against a hermetic temp ``.env``
and restore the settings singleton plus the process environment afterwards.
"""

import json
import os
import sys
from pathlib import Path

import pytest

from app.cli import main
from app.core.config import settings
from app.core.config_spec import (
    SPECS,
    INFO,
    LIVE,
    RESTART,
    parse_value,
    render_value,
)
from app.services import config_store

SECRETS = {
    spec.env
    for spec in SPECS
    if spec.secret and spec.env is not None
}


@pytest.fixture
def mutation_env(monkeypatch, tmp_path):
    """
    Hermetic .env for the P7.2 write commands: point the single writer
    (``config_store.env_file``) and the reload path (``app.core.config
    .env_file``) at a temp file, and restore the settings singleton plus
    the process environment afterwards so no test leaks state.
    """
    from app.core import config as config_module

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


def _write_env(tmp_path: Path, pairs: dict[str, str], name: str = "test.env") -> Path:
    path = tmp_path / name
    path.write_text("".join(f"{key}={value}\n" for key, value in pairs.items()))
    return path


def _run_config(capsys, argv: list[str]):
    main(["config", *argv])
    return capsys.readouterr()


# --------------------------------------------------------------------- show

def test_show_lists_every_env_backed_setting(capsys):
    out, err = _run_config(capsys, ["show"])

    env_backed = {
        spec.env for spec in SPECS if spec.env is not None and spec.cli_visible
    }

    for env in env_backed:
        assert env in out

    assert err == ""


def test_show_masks_secret_values(capsys, tmp_path):
    out, _ = _run_config(capsys, ["show"])

    secret_envs = sorted(SECRETS)

    for env in secret_envs:
        assert env in out

    # The literal secret markers must never appear in raw form.
    for token in ("sk-", "gsk_", "AIza", "nvapi-"):
        assert token not in out


def test_show_json_is_valid_and_masks(capsys):
    out, _ = _run_config(capsys, ["show", "--json"])
    rows = json.loads(out)

    assert isinstance(rows, list) and rows

    envs = {row["env"] for row in rows}

    for spec in SPECS:
        if spec.env is None or not spec.cli_visible:
            continue
        assert spec.env in envs

    for row in rows:
        assert row["effect"] in (LIVE, RESTART, INFO)
        assert isinstance(row["value"], str)
        assert isinstance(row["default"], str)


def test_show_json_marks_secret_and_masks_value(capsys):
    out, _ = _run_config(capsys, ["show", "--json"])
    rows = json.loads(out)

    secrets = [row for row in rows if row["secret"]]

    assert secrets

    for row in secrets:
        assert row["value"] == "(unset)" or "*" in row["value"]

    for token in ("sk-", "gsk_", "AIza", "nvapi-"):
        assert token not in out


# ------------------------------------------------------------------ validate

def test_validate_reports_all_invalid_values(capsys, tmp_path):
    invalid = {
        "REQUEST_TIMEOUT": "abc",
        "SCORING_PRIORITY_DENOM": "0",
        "RELAY_PORT": "oops",
    }
    good = {
        "MAX_RETRIES": "3",
        "HEALTH_FRESHNESS_EXPONENT": "0.5",
        "LOG_LEVEL": "DEBUG",
    }
    target = _write_env(tmp_path, {**invalid, **good})

    with pytest.raises(SystemExit) as exc:
        main(["config", "validate", "--env-file", str(target)])

    out, err = capsys.readouterr()

    assert exc.value.code == 2

    for env in invalid:
        assert f"Invalid value for {env}" in err
        assert "abc" not in err
        assert "oops" not in err

    for env in good:
        assert env not in err


def test_validate_reports_unknown_keys(capsys, tmp_path):
    target = _write_env(tmp_path, {"MAX_RETRIES": "3", "NOT_A_REAL_KEY": "x"})

    main(["config", "validate", "--env-file", str(target)])

    out, err = capsys.readouterr()

    assert "Config OK" in out
    assert "unknown key(s)" in err
    assert "NOT_A_REAL_KEY" in err


def test_validate_ok_exits_zero(capsys, tmp_path):
    target = _write_env(tmp_path, {"MAX_RETRIES": "3", "LOG_LEVEL": "DEBUG"})

    main(["config", "validate", "--env-file", str(target)])

    out, err = capsys.readouterr()

    assert "Config OK" in out
    assert err == ""


def test_validate_ok_reports_unknown_keys_to_stderr(capsys, tmp_path):
    target = _write_env(
        tmp_path,
        {"MAX_RETRIES": "3", "LOG_LEVEL": "DEBUG", "ALSO_UNKNOWN": "1"},
    )

    main(["config", "validate", "--env-file", str(target)])

    out, err = capsys.readouterr()

    assert "Config OK" in out
    assert "ALSO_UNKNOWN" in err


def test_validate_missing_file_exits_one(capsys, tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["config", "validate", "--env-file", str(tmp_path / "nope.env")])

    out, err = capsys.readouterr()

    assert exc.value.code == 1
    assert "not found" in err


def test_validate_masks_secret_values(capsys, tmp_path):
    target = _write_env(
        tmp_path,
        {"MAX_RETRIES": "3", "OPENAI_API_KEY": "sk-secret-value-abc"},
    )

    main(["config", "validate", "--env-file", str(target)])

    out, err = capsys.readouterr()

    assert "sk-secret-value-abc" not in out + err


# ---------------------------------------------------------------------- diff

def test_diff_file_process_reports_changed(capsys, tmp_path):
    target = _write_env(
        tmp_path,
        {"MAX_RETRIES": "99", "LOG_LEVEL": "DEBUG"},
    )

    main(["config", "diff", str(target)])

    out, err = capsys.readouterr()

    assert "diff" in out
    assert "CHANGED" in out
    assert "MAX_RETRIES" in out
    assert "99" in out
    assert err == ""


def test_diff_file_process_unchanged(capsys, tmp_path):
    pairs = {}

    for spec in SPECS:
        if spec.env is None or spec.secret or spec.type == "url":
            continue

        value = getattr(settings, spec.attr, None)

        if value is None:
            continue

        rendered = render_value(spec, value)

        if parse_value(spec, rendered) != value:
            continue

        pairs[spec.env] = rendered

    target = _write_env(tmp_path, pairs)

    main(["config", "diff", str(target)])

    out, err = capsys.readouterr()

    assert "UNCHANGED" in out
    assert not any(line == "CHANGED" for line in out.splitlines())


def test_diff_file_file_reports_changed(capsys, tmp_path):
    path_a = _write_env(tmp_path, {"MAX_RETRIES": "3", "LOG_LEVEL": "DEBUG"}, name="a.env")
    path_b = _write_env(tmp_path, {"MAX_RETRIES": "9", "LOG_LEVEL": "DEBUG"}, name="b.env")

    main(["config", "diff", str(path_a), str(path_b)])

    out, err = capsys.readouterr()

    assert "CHANGED" in out
    assert "MAX_RETRIES" in out
    assert "3" in out and "9" in out
    assert "LOG_LEVEL" in out.split("CHANGED")[1] or "UNCHANGED" in out


def test_diff_file_file_missing(capsys, tmp_path):
    path_a = _write_env(tmp_path, {"MAX_RETRIES": "3"}, name="a.env")
    path_b = _write_env(tmp_path, {"MAX_RETRIES": "3", "LOG_LEVEL": "DEBUG"}, name="b.env")

    main(["config", "diff", str(path_a), str(path_b)])

    out, err = capsys.readouterr()

    assert "MISSING" in out
    assert "LOG_LEVEL" in out


def test_diff_masks_secrets(capsys, tmp_path):
    target = _write_env(
        tmp_path,
        {"MAX_RETRIES": "3", "OPENAI_API_KEY": "sk-verysecret-xyz"},
    )

    main(["config", "diff", str(target)])

    out, err = capsys.readouterr()

    assert "sk-verysecret-xyz" not in out + err
    assert "OPENAI_API_KEY" in out


def test_diff_missing_file_exits_one(capsys, tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["config", "diff", str(tmp_path / "nope.env")])

    out, err = capsys.readouterr()

    assert exc.value.code == 1
    assert "not found" in err


def test_diff_rejects_two_many_files(capsys, tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "config",
                "diff",
                str(tmp_path / "a.env"),
                str(tmp_path / "b.env"),
                str(tmp_path / "c.env"),
            ]
        )

    out, err = capsys.readouterr()

    assert exc.value.code == 2
    assert "at most two env files" in err


# ------------------------------------------------------------------- plumbing

def test_no_subcommand_prints_help(capsys):
    main(["config"])

    out, err = capsys.readouterr()

    assert "show" in out
    assert "validate" in out
    assert "diff" in out
    assert "set" in out
    assert "unset" in out
    assert "reload" in out


# --------------------------------------------------- set (P7.2)

def test_set_valid_exits_zero_and_writes(mutation_env, capsys):
    main(["config", "set", "MAX_RETRIES", "3", "--yes", "--no-reload"])

    out, err = capsys.readouterr()

    assert "MAX_RETRIES saved" in out
    assert err == ""
    assert "MAX_RETRIES='3'" in mutation_env.read_text(encoding="utf-8")


def test_set_live_field_applies_in_process(mutation_env, capsys):
    main(["config", "set", "MAX_RETRIES", "9", "--yes"])

    out, err = capsys.readouterr()

    assert "Applied in-process." in out
    assert settings.max_retries == 9


def test_set_secret_never_echoed(mutation_env, capsys):
    main(
        ["config", "set", "OPENAI_API_KEY", "sk-topsecret-xyz", "--yes", "--no-reload"]
    )

    out, err = capsys.readouterr()

    assert "sk-topsecret-xyz" not in out + err
    assert "OPENAI_API_KEY" in out
    assert "sk-topsecret-xyz" in mutation_env.read_text(encoding="utf-8")


def test_set_secret_from_stdin(mutation_env, capsys, monkeypatch):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO("sk-stdin-secret\n"))
    main(["config", "set", "OPENAI_API_KEY", "-", "--yes", "--no-reload"])

    out, err = capsys.readouterr()

    assert "sk-stdin-secret" not in out + err
    assert "sk-stdin-secret" in mutation_env.read_text(encoding="utf-8")


def test_set_invalid_exits_two_with_redacted_error(mutation_env, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["config", "set", "REQUEST_TIMEOUT", "abc", "--yes"])

    out, err = capsys.readouterr()

    assert exc.value.code == 2
    assert "Invalid value for REQUEST_TIMEOUT" in err
    assert "abc" not in err
    assert not mutation_env.exists()


def test_set_unknown_env_exits_two(mutation_env, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["config", "set", "NOT_A_SETTING", "x", "--yes"])

    out, err = capsys.readouterr()

    assert exc.value.code == 2
    assert "Unknown setting 'NOT_A_SETTING'" in err
    assert not mutation_env.exists()


def test_set_noninteractive_without_yes_refused(mutation_env, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["config", "set", "MAX_RETRIES", "3"])

    out, err = capsys.readouterr()

    assert exc.value.code == 1
    assert "pass --yes to confirm" in err
    assert not mutation_env.exists()


def test_set_dry_run_writes_nothing(mutation_env, capsys):
    main(["config", "set", "MAX_RETRIES", "9", "--yes", "--dry-run"])

    out, err = capsys.readouterr()

    assert "MAX_RETRIES" in out
    assert "dry run" in out
    assert not mutation_env.exists()


def test_set_dry_run_secret_preview_masked(mutation_env, capsys):
    main(["config", "set", "OPENAI_API_KEY", "sk-drysecret", "--yes", "--dry-run"])

    out, err = capsys.readouterr()

    assert "sk-drysecret" not in out + err
    assert not mutation_env.exists()


def test_set_restart_field_reports_restart_required(mutation_env, capsys):
    main(["config", "set", "RELAY_PORT", "9000", "--yes", "--no-reload"])

    out, err = capsys.readouterr()

    assert "Restart required" in out
    assert "RELAY_PORT='9000'" in mutation_env.read_text(encoding="utf-8")


def test_set_json_report_has_no_values(mutation_env, capsys):
    main(["config", "set", "MAX_RETRIES", "3", "--yes", "--no-reload", "--json"])

    out, _ = capsys.readouterr()
    payload = json.loads(out)

    assert payload["saved"] is True
    assert payload["env"] == "MAX_RETRIES"
    assert set(payload) == {
        "saved", "env", "effect", "reloaded", "applied", "restored"
    }


# ------------------------------------------------- unset (P7.2)

def test_unset_removes_key(mutation_env, capsys):
    main(["config", "set", "MAX_RETRIES", "9", "--yes", "--no-reload"])
    capsys.readouterr()

    main(["config", "unset", "MAX_RETRIES", "--yes", "--no-reload"])

    out, err = capsys.readouterr()

    assert "MAX_RETRIES saved" in out
    assert "MAX_RETRIES" not in mutation_env.read_text(encoding="utf-8")


def test_unset_absent_key_is_idempotent(mutation_env, capsys):
    main(["config", "unset", "MAX_RETRIES", "--yes", "--no-reload"])

    out, err = capsys.readouterr()

    assert "MAX_RETRIES saved" in out
    assert err == ""


def test_unset_restores_default_in_process(mutation_env, capsys):
    main(["config", "set", "MAX_RETRIES", "9", "--yes"])
    capsys.readouterr()

    main(["config", "unset", "MAX_RETRIES", "--yes"])

    out, err = capsys.readouterr()

    assert "Applied in-process." in out
    assert settings.max_retries == 1


# ------------------------------------------------- reload (P7.2)

def test_reload_reports_applied_and_unchanged(mutation_env, capsys):
    mutation_env.write_text("MAX_RETRIES=9\n", encoding="utf-8")

    main(["config", "reload"])

    out, err = capsys.readouterr()

    assert "Configuration reloaded." in out
    assert "applied" in out
    assert "max_retries" in out
    assert settings.max_retries == 9
    assert err == ""


def test_reload_dry_run_mutates_nothing(mutation_env, capsys):
    mutation_env.write_text("MAX_RETRIES=9\n", encoding="utf-8")
    before = settings.max_retries

    main(["config", "reload", "--dry-run"])

    out, err = capsys.readouterr()

    assert "Dry run" in out
    assert "max_retries" in out
    assert settings.max_retries == before


def test_reload_invalid_file_exits_one_redacted(mutation_env, capsys):
    mutation_env.write_text("REQUEST_TIMEOUT=abc\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        main(["config", "reload"])

    out, err = capsys.readouterr()

    assert exc.value.code == 1
    assert "Invalid value for REQUEST_TIMEOUT" in err
    assert "abc" not in err


# ----------------------------------------------------------- audit (P7.2)

def test_config_set_emits_audit_event(mutation_env, capsys, isolated_event_log):
    main(["config", "set", "MAX_RETRIES", "3", "--yes", "--no-reload"])
    capsys.readouterr()

    events = isolated_event_log.query(action="config.set")

    assert len(events) == 1
    assert events[0]["target"] == "MAX_RETRIES"
    assert events[0]["outcome"] == "ok"
    assert events[0]["actor"] == "cli"
    assert events[0]["detail"] == {"reloaded": False, "restored": False}


def test_config_set_failed_emits_failed_audit(
    mutation_env, capsys, isolated_event_log
):
    with pytest.raises(SystemExit) as exc:
        main(["config", "set", "REQUEST_TIMEOUT", "abc", "--yes"])

    assert exc.value.code == 2
    capsys.readouterr()

    events = isolated_event_log.query(action="config.set")

    assert len(events) == 1
    assert events[0]["target"] == "REQUEST_TIMEOUT"
    assert events[0]["outcome"] == "failed"
    assert events[0]["detail"] == {"reloaded": False, "restored": False}


def test_config_unset_emits_audit_event(mutation_env, capsys, isolated_event_log):
    main(["config", "unset", "MAX_RETRIES", "--yes", "--no-reload"])
    capsys.readouterr()

    events = isolated_event_log.query(action="config.unset")

    assert len(events) == 1
    assert events[0]["target"] == "MAX_RETRIES"
    assert events[0]["outcome"] == "ok"
    assert events[0]["detail"] == {"reloaded": False, "restored": False}


def test_config_reload_emits_audit_event(mutation_env, capsys, isolated_event_log):
    mutation_env.write_text("MAX_RETRIES=9\n", encoding="utf-8")

    main(["config", "reload"])
    capsys.readouterr()

    events = isolated_event_log.query(action="config.reload")

    assert len(events) == 1
    assert events[0]["outcome"] == "ok"
    # MAX_RETRIES=9 differs from the running default, so it must be applied.
    assert events[0]["detail"]["applied"] >= 1
    assert isinstance(events[0]["detail"]["unchanged"], int)


def test_config_set_reload_failure_emits_failed_audit(
    mutation_env, capsys, isolated_event_log
):
    mutation_env.write_text("REQUEST_TIMEOUT=abc\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        main(["config", "set", "MAX_RETRIES", "3", "--yes"])

    assert exc.value.code == 1
    capsys.readouterr()

    events = isolated_event_log.query(action="config.set")

    assert len(events) == 1
    assert events[0]["outcome"] == "failed"
    assert events[0]["detail"] == {"reloaded": False, "restored": True}
