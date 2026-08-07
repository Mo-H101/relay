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
import sysconfig
import tarfile
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
    assert re.fullmatch(
        r"\d+\.\d+\.\d+(?:[.-]?(?:a|b|rc|dev|post)\d+)?",
        _version(),
    )


def test_dist_artifacts_match_current_version():
    """
    Stale-dist guard: any built artifacts under ``dist/`` must carry the
    current package version. A leftover artifact from an older release
    (e.g. ``relay-0.1.0``) would otherwise risk being published or
    installed in place of the real build.
    """
    dist_dir = PROJECT_ROOT / "dist"
    if not dist_dir.is_dir():
        return

    version = _version()
    files = list(dist_dir.iterdir())
    assert files, "dist/ exists but is empty"

    for f in files:
        assert f.name.startswith(f"relay-{version}"), (
            f"stale artifact in dist/: {f.name} (current version {version})"
        )


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


def test_windows_installer_cmd_uses_process_local_policy_bypass():
    text = (PROJECT_ROOT / "install.cmd").read_text(encoding="utf-8")
    assert "ExecutionPolicy Bypass" in text
    assert "install.ps1" in text


def test_posix_installer_exposes_relay_on_path():
    text = (PROJECT_ROOT / "install.sh").read_text(encoding="utf-8")
    assert "PATH" in text
    assert ".local/bin" in text
    assert "export PATH" in text


# ----------------------------------------------------- installed config layout

def test_installed_layout_uses_stable_user_data_dir(monkeypatch, tmp_path):
    import app.core.config as config

    monkeypatch.delenv("RELAY_ENV_FILE", raising=False)
    monkeypatch.delenv("RELAY_STATE_DIR", raising=False)
    monkeypatch.setattr(config, "IS_SOURCE_CHECKOUT", False)
    monkeypatch.setattr(config, "_user_data_dir", lambda: tmp_path)

    assert config._resolve_env_file() == tmp_path / ".env"
    assert config._resolve_state_dir() == tmp_path
    assert config._resolve_persistence_path() == tmp_path / "platform.db"


def test_source_checkout_keeps_env_next_to_project(monkeypatch, tmp_path):
    import app.core.config as config

    monkeypatch.delenv("RELAY_ENV_FILE", raising=False)
    monkeypatch.delenv("RELAY_STATE_DIR", raising=False)
    assert config.IS_SOURCE_CHECKOUT is True

    monkeypatch.chdir(tmp_path)
    assert config._resolve_env_file() == config.PROJECT_ROOT / ".env"
    assert config._resolve_state_dir() == config.PROJECT_ROOT / ".relay"
    assert config._resolve_persistence_path() == config.PROJECT_ROOT / ".relay" / "platform.db"


def test_source_checkout_prefers_cwd_env_when_present(monkeypatch, tmp_path):
    import app.core.config as config

    monkeypatch.delenv("RELAY_ENV_FILE", raising=False)
    cwd_env = tmp_path / ".env"
    cwd_env.write_text("RELAY_PORT=7777\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert config._resolve_env_file() == cwd_env
    assert config._resolve_state_dir() == tmp_path / ".relay"


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

@pytest.fixture(scope="module")
def installed_env(tmp_path_factory):
    """
    Build the wheel and install it into an isolated venv, once per module.

    The venv inherits the host test environment's packages via a ``.pth``
    file: ``python -m venv --system-site-packages`` from inside a venv
    resolves against the *base* interpreter (which may lack optional deps
    such as ``rich`` or ``platformdirs``), so a plain isolated venv plus a
    pointer to the host site-packages is used instead.
    """
    pytest.importorskip("setuptools")
    pytest.importorskip("wheel")

    wheel_dir = tmp_path_factory.mktemp("wheel")
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

    venv_dir = tmp_path_factory.mktemp("venv")
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
        capture_output=True,
    )
    venv_python = venv_dir / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    _inherit_host_site_packages(venv_python)
    subprocess.run(
        [str(venv_python), "-m", "pip", "install",
         "--no-deps", "--ignore-installed", str(wheels[-1])],
        check=True,
        capture_output=True,
    )
    relay_exe = venv_dir / (
        "Scripts/relay.exe" if os.name == "nt" else "bin/relay"
    )
    assert relay_exe.exists(), "relay console script missing"

    return {
        "wheel": wheels[-1],
        "venv_python": venv_python,
        "relay_exe": relay_exe,
    }


def _inherit_host_site_packages(venv_python):
    """Make the isolated venv see the packages of the pytest interpreter."""
    host_purelib = sysconfig.get_paths()["purelib"]
    probe = subprocess.run(
        [str(venv_python), "-c",
         "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        check=True,
        capture_output=True,
        text=True,
    )
    venv_site = Path(probe.stdout.strip())
    (venv_site / "_relay_host.pth").write_text(
        f"{host_purelib}\n", encoding="utf-8"
    )


@pytest.mark.skipif(
    os.environ.get("RUN_PACKAGING_SMOKE") == "0",
    reason="disabled via RUN_PACKAGING_SMOKE=0",
)
def test_package_build_and_console_script(installed_env):
    with zipfile.ZipFile(installed_env["wheel"]) as zf:
        names = zf.namelist()
        assert any(name.startswith("app/") for name in names), (
            "app package missing from wheel"
        )
        entry_points = [n for n in names if n.endswith("entry_points.txt")]
        assert entry_points, "entry_points.txt missing"
        ep_text = zf.read(entry_points[0]).decode()
        assert "relay = app.cli:main" in ep_text

    relay_exe = installed_env["relay_exe"]

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


@pytest.mark.skipif(
    os.environ.get("RUN_PACKAGING_SMOKE") == "0",
    reason="disabled via RUN_PACKAGING_SMOKE=0",
)
def test_installed_cli_runs_from_arbitrary_cwd_with_stable_state(
    installed_env, tmp_path
):
    """
    Installed Relay must run from any directory (no venv activation) and
    keep config/state/data in a stable user-data dir, not the CWD.
    """
    relay_exe = installed_env["relay_exe"]
    venv_python = installed_env["venv_python"]

    data_dir = tmp_path / "relay-data"
    arbitrary_cwd = tmp_path / "somewhere-else"
    arbitrary_cwd.mkdir()

    env = dict(os.environ)
    env["RELAY_DATA_DIR"] = str(data_dir)

    ver = subprocess.run(
        [str(relay_exe), "--version"],
        cwd=str(arbitrary_cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    assert ver.returncode == 0
    assert _version() in (ver.stdout + ver.stderr)

    probe = subprocess.run(
        [
            str(venv_python), "-c",
            "from app.core import config; "
            "print(config.env_file); print(config.state_dir); "
            "print(config.settings.persistence_path)",
        ],
        cwd=str(arbitrary_cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr
    out = probe.stdout.splitlines()
    assert out[0] == str(data_dir / ".env")
    assert out[1] == str(data_dir)
    assert out[2] == str(data_dir / "platform.db")

    write = subprocess.run(
        [
            str(venv_python), "-c",
            "from app.services import setup_state; "
            "from app.setup import persistence; "
            "setup_state.write_setup_state('configured'); "
            "persistence.write_model_status('nvidia', [])",
        ],
        cwd=str(arbitrary_cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    assert write.returncode == 0, write.stderr
    assert (data_dir / "state.json").exists()
    assert (data_dir / "platform.db").exists()


def test_manifest_prunes_tests_and_build_artifacts():
    """
    MANIFEST.in must keep the sdist to the source package and standard
    metadata: the test suite, benchmarks, docs, and build artifacts are
    not needed to build or install Relay from an sdist.
    """
    manifest = (PROJECT_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    for entry in ("prune tests", "prune bench", "prune docs", "prune dist"):
        assert entry in manifest, entry


@pytest.mark.skipif(
    os.environ.get("RUN_PACKAGING_SMOKE") == "0",
    reason="disabled via RUN_PACKAGING_SMOKE=0",
)
def test_sdist_build_excludes_tests_and_bench(tmp_path_factory):
    pytest.importorskip("build")

    out_dir = tmp_path_factory.mktemp("sdist")
    proc = subprocess.run(
        [
            sys.executable, "-m", "build",
            "--sdist", "--no-isolation",
            "--outdir", str(out_dir), ".",
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr

    sdists = sorted(out_dir.glob("relay-*.tar.gz"))
    assert sdists, "no sdist produced"

    with tarfile.open(sdists[-1]) as tf:
        names = tf.getnames()

    assert not any("/tests/" in name for name in names)
    assert not any("/bench/" in name for name in names)
    assert not any("/docs/" in name for name in names)
    assert any(name.startswith("relay-") and "/app/" in name for name in names)  # source present
