"""
Phase P0 — packaging and first-run foundation tests.

Covers:
- package version and metadata
- CLI entry point (console script + `python -m app.cli`)
- setup-state marker (clean install, configured, incomplete, corrupt)
- first-run dispatch (setup vs. serve)
- package build smoke (wheel + installed `relay` console script)

The build smoke test runs by default when setuptools/wheel are installed
and can be disabled with RUN_PACKAGING_SMOKE=0.
"""

import json
import os
import re
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _version():
    from app import __version__
    return __version__


# ------------------------------------------------------------------ metadata

def test_version_is_pep440():
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[.-].+)?", _version())


def test_pyproject_declares_relay_console_script():
    with open(PROJECT_ROOT / "pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)
    assert data["project"]["name"] == "relay"
    assert data["project"]["scripts"]["relay"] == "app.cli:main"
    assert ">=3.10" in data["project"]["requires-python"]
    assert data["build-system"]["build-backend"].startswith("setuptools")


def test_entry_point_is_importable():
    import app.cli
    assert callable(app.cli.main)


def test_windows_installer_exposes_relay_on_user_path():
    text = (PROJECT_ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "SetEnvironmentVariable" in text
    assert '"User"' in text
    assert "Scripts" in text


def test_posix_installer_exposes_relay_on_path():
    text = (PROJECT_ROOT / "install.sh").read_text(encoding="utf-8")
    assert "PATH" in text
    assert ".local/bin" in text
    assert "export PATH" in text


def test_module_cli_help_lists_setup():
    proc = subprocess.run(
        [sys.executable, "-m", "app.cli", "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "setup" in (proc.stdout + proc.stderr)


def test_module_cli_help_lists_tui_and_serve():
    proc = subprocess.run(
        [sys.executable, "-m", "app.cli", "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    out = proc.stdout + proc.stderr
    assert "tui" in out
    assert "serve" in out
    assert "setup" in out


def test_module_cli_version_matches_package():
    proc = subprocess.run(
        [sys.executable, "-m", "app.cli", "--version"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert _version() in (proc.stdout + proc.stderr)


# ------------------------------------------------------------- first-run state

def test_clean_install_state_is_not_configured(monkeypatch, tmp_path):
    from app.services import setup_state
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / "missing")
    assert setup_state.read_setup_state() == "not_configured"


def test_configured_state_roundtrip(monkeypatch, tmp_path):
    from app.services import setup_state
    state_dir = tmp_path / ".relay"
    monkeypatch.setattr(setup_state, "state_dir", state_dir)
    setup_state.write_setup_state("configured")
    assert setup_state.read_setup_state() == "configured"
    assert (state_dir / "state.json").exists()


def test_incomplete_state_detected(monkeypatch, tmp_path):
    from app.services import setup_state
    state_dir = tmp_path / ".relay"
    monkeypatch.setattr(setup_state, "state_dir", state_dir)
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({"schema": 1, "setup_state": "incomplete"}),
        encoding="utf-8",
    )
    assert setup_state.read_setup_state() == "incomplete"


def test_corrupt_state_file_is_incomplete(monkeypatch, tmp_path):
    from app.services import setup_state
    state_dir = tmp_path / ".relay"
    monkeypatch.setattr(setup_state, "state_dir", state_dir)
    state_dir.mkdir()
    (state_dir / "state.json").write_text("{not json", encoding="utf-8")
    assert setup_state.read_setup_state() == "incomplete"


def test_env_file_presence_alone_is_not_configured(monkeypatch, tmp_path):
    """
    A user can have a .env without a completed setup; the state marker is
    authoritative for first-run detection.
    """
    import app.cli as cli
    from app.services import setup_state
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")

    calls = []
    monkeypatch.setattr(cli, "_cmd_setup", lambda args: calls.append(True))
    cli.main([])
    assert calls == [True]


# ------------------------------------------------------------- first-run hooks

def _patch_provider_state(monkeypatch, configured):
    from app.core.config import settings
    monkeypatch.setattr(settings, "nvidia_enabled", configured)
    monkeypatch.setattr(settings, "openai_enabled", False)
    monkeypatch.setattr(settings, "lmstudio_enabled", False)
    monkeypatch.setattr(settings, "nvidia_api_key", "test-key" if configured else "")
    monkeypatch.setattr(settings, "openai_api_key", "")


def test_unconfigured_first_run_launches_setup(monkeypatch, tmp_path):
    import app.cli as cli
    from app.services import setup_state
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    _patch_provider_state(monkeypatch, configured=False)

    setup_calls = []
    serve_calls = []
    monkeypatch.setattr(cli, "_cmd_setup", lambda args: setup_calls.append(True))
    monkeypatch.setattr(cli, "_cmd_serve", lambda: serve_calls.append(True))

    cli.main([])
    assert setup_calls == [True]
    assert serve_calls == []


def test_configured_execution_path_launches_tui(monkeypatch, tmp_path):
    import app.cli as cli
    from app.services import setup_state
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    setup_state.write_setup_state("configured")
    _patch_provider_state(monkeypatch, configured=True)

    setup_calls = []
    tui_calls = []
    monkeypatch.setattr(cli, "_cmd_setup", lambda args: setup_calls.append(True))
    monkeypatch.setattr(cli, "_cmd_tui", lambda: tui_calls.append(True))

    cli.main([])
    assert tui_calls == [True]
    assert setup_calls == []


def test_serve_subcommand_dispatches_to_server(monkeypatch, tmp_path):
    import app.cli as cli
    from app.services import setup_state
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")

    serve_calls = []
    monkeypatch.setattr(cli, "_cmd_serve", lambda: serve_calls.append(True))

    cli.main(["serve"])
    assert serve_calls == [True]


def test_tui_subcommand_dispatches_to_tui(monkeypatch, tmp_path):
    import app.cli as cli
    from app.services import setup_state
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")

    tui_calls = []
    monkeypatch.setattr(cli, "_cmd_tui", lambda: tui_calls.append(True))

    cli.main(["tui"])
    assert tui_calls == [True]


def test_incomplete_state_reruns_setup(monkeypatch, tmp_path):
    import app.cli as cli
    from app.services import setup_state
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    setup_state.write_setup_state("incomplete")
    _patch_provider_state(monkeypatch, configured=False)

    setup_calls = []
    monkeypatch.setattr(cli, "_cmd_setup", lambda args: setup_calls.append(True))
    cli.main([])
    assert setup_calls == [True]


def test_configured_marker_but_no_provider_reruns_setup(monkeypatch, tmp_path):
    import app.cli as cli
    from app.services import setup_state
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    setup_state.write_setup_state("configured")
    _patch_provider_state(monkeypatch, configured=False)

    setup_calls = []
    serve_calls = []
    monkeypatch.setattr(cli, "_cmd_setup", lambda args: setup_calls.append(True))
    monkeypatch.setattr(cli, "_cmd_serve", lambda: serve_calls.append(True))

    cli.main([])
    assert setup_calls == [True]
    assert serve_calls == []


# -------------------------------------------------------------- package build

@pytest.mark.skipif(
    os.environ.get("RUN_PACKAGING_SMOKE") == "0",
    reason="disabled via RUN_PACKAGING_SMOKE=0",
)
def test_package_build_and_console_script(tmp_path):
    pytest.importorskip("setuptools")
    pytest.importorskip("wheel")

    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()

    proc = subprocess.run(
        [
            sys.executable, "-m", "pip", "wheel",
            "--no-build-isolation", "--no-deps",
            "-w", str(wheel_dir), ".",
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr

    wheels = sorted(wheel_dir.glob("relay-*.whl"))
    assert wheels, "no wheel produced"

    with zipfile.ZipFile(wheels[-1]) as zf:
        names = zf.namelist()
        assert any(name.startswith("app/") for name in names), (
            "app package missing from wheel"
        )
        entry_points = [n for n in names if n.endswith("entry_points.txt")]
        assert entry_points, "entry_points.txt missing"
        ep_text = zf.read(entry_points[0]).decode()
        assert "relay = app.cli:main" in ep_text

    venv_dir = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(venv_dir)],
        check=True,
        capture_output=True,
    )
    venv_python = venv_dir / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--no-deps", str(wheels[-1])],
        check=True,
        capture_output=True,
    )
    relay_exe = venv_dir / (
        "Scripts/relay.exe" if os.name == "nt" else "bin/relay"
    )
    assert relay_exe.exists(), "relay console script missing"

    help_proc = subprocess.run(
        [str(relay_exe), "--help"], capture_output=True, text=True
    )
    assert help_proc.returncode == 0
    assert "usage" in (help_proc.stdout + help_proc.stderr).lower()

    version_proc = subprocess.run(
        [str(relay_exe), "--version"], capture_output=True, text=True
    )
    assert version_proc.returncode == 0
    assert _version() in (version_proc.stdout + version_proc.stderr)
