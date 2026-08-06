"""
Tests for ``relay config`` (P7.1, read-only): ``show``, ``validate``,
``diff``.

These exercise the CLI through ``main(argv)`` + ``capsys`` exactly like the
other CLI test modules, including the masking/redaction guarantees: no raw
secret value may ever reach stdout or stderr, and ``validate`` reports
field names only.
"""

import json
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

SECRETS = {
    spec.env
    for spec in SPECS
    if spec.secret and spec.env is not None
}


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
