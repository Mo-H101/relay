"""
CLI migration tests: ``relay provider keys migrate`` (and the
``relay keys provider migrate`` alias).

Covers env->keyring moves, idempotent re-runs, dry-run safety, the
write-all-then-cleanup invariant (a keyring failure never removes a .env
key), conflict handling, provider filtering, non-interactive confirmation,
and the never-print guarantee.

A fake in-memory keyring backend is installed via ``keyring.set_keyring``
so no real OS credential store is touched; the active ``.env`` is a temp
file patched through ``config_store.env_file``.
"""

import sys

import keyring
import pytest

from app.services import config_store
from app.setup.key_validation import mask_key


class FakeKeyring(keyring.backend.KeyringBackend):
    """
    In-memory keyring backend recording ``(service, username)`` keys.
    """

    @classmethod
    def priority(cls):
        return 10

    def __init__(self):
        self._data = {}

    def get_password(self, service, username):
        return self._data.get((service, username))

    def set_password(self, service, username, password):
        self._data[(service, username)] = password

    def delete_password(self, service, username):
        try:
            del self._data[(service, username)]
        except KeyError:
            raise keyring.errors.PasswordDeleteError(username)


_PROVIDER_KEY_ENVS = (
    "NVIDIA_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "LMSTUDIO_API_KEY",
)


@pytest.fixture(autouse=True)
def _clear_provider_env(monkeypatch):
    """Never let the repo's own .env leak into get_env's process fallback."""
    for var in _PROVIDER_KEY_ENVS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def fake_keyring():
    backend = FakeKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(keyring.backends.fail.Keyring())


@pytest.fixture
def env_file(monkeypatch, tmp_path):
    path = tmp_path / ".env"
    monkeypatch.setattr(config_store, "env_file", path)
    return path


@pytest.fixture
def keyring_on(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_keyring_enabled", True)


@pytest.fixture
def run_cli(capsys):
    from app.cli import main

    def _run(argv):
        main(argv)
        out, err = capsys.readouterr()
        return out, err

    return _run


def _set_env(env_file, key, value):
    config_store.set_env(key, value)
    assert key in env_file.read_text(encoding="utf-8")


# ------------------------------------------------------------ env -> keyring

def test_migrate_moves_env_key_into_keyring(fake_keyring, env_file, keyring_on, run_cli):
    _set_env(env_file, "NVIDIA_API_KEY", "nv-key-1234")

    out, _ = run_cli(["provider", "keys", "migrate", "--yes"])

    assert "nvidia" in out
    assert "migrated" in out
    assert fake_keyring.get_password("relay", "nvidia") == "nv-key-1234"
    assert "NVIDIA_API_KEY" not in env_file.read_text(encoding="utf-8")


def test_migrate_handles_all_cloud_providers(fake_keyring, env_file, keyring_on, run_cli):
    _set_env(env_file, "NVIDIA_API_KEY", "nv-key")
    _set_env(env_file, "OPENAI_API_KEY", "sk-key")

    out, _ = run_cli(["provider", "keys", "migrate", "--yes"])

    assert fake_keyring.get_password("relay", "nvidia") == "nv-key"
    assert fake_keyring.get_password("relay", "openai") == "sk-key"
    assert "NVIDIA_API_KEY" not in env_file.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" not in env_file.read_text(encoding="utf-8")


def test_migrate_alias_keys_provider_dispatches(
    fake_keyring, env_file, keyring_on, run_cli
):
    _set_env(env_file, "NVIDIA_API_KEY", "nv-key-5678")

    out, _ = run_cli(["keys", "provider", "migrate", "--yes"])

    assert "nvidia" in out
    assert fake_keyring.get_password("relay", "nvidia") == "nv-key-5678"
    assert "NVIDIA_API_KEY" not in env_file.read_text(encoding="utf-8")


def test_migrate_provider_filter_restricts_run(
    fake_keyring, env_file, keyring_on, run_cli
):
    _set_env(env_file, "NVIDIA_API_KEY", "nv-key")
    _set_env(env_file, "OPENAI_API_KEY", "sk-key")

    out, _ = run_cli(
        ["provider", "keys", "migrate", "--provider", "openai", "--yes"]
    )

    assert fake_keyring.get_password("relay", "openai") == "sk-key"
    assert fake_keyring.get_password("relay", "nvidia") is None
    assert "OPENAI_API_KEY" not in env_file.read_text(encoding="utf-8")
    assert "NVIDIA_API_KEY" in env_file.read_text(encoding="utf-8")


# --------------------------------------------------------------- dry run

def test_migrate_dry_run_mutates_nothing(fake_keyring, env_file, keyring_on, run_cli):
    _set_env(env_file, "NVIDIA_API_KEY", "nv-key-9999")

    out, _ = run_cli(["provider", "keys", "migrate", "--dry-run", "--yes"])

    assert "migrate" in out
    assert "nvidia" in out
    assert fake_keyring.get_password("relay", "nvidia") is None
    assert "NVIDIA_API_KEY" in env_file.read_text(encoding="utf-8")
    assert "nv-key-9999" not in out


# ------------------------------------------------- P6.2 audit events

def test_provider_key_mutations_record_audit_events(
    fake_keyring, env_file, keyring_on, run_cli, isolated_event_log
):
    run_cli(["provider", "keys", "set", "openai", "sk-audit-1234", "--yes"])

    set_events = isolated_event_log.query(action="provider_key.set")
    assert len(set_events) == 1
    assert set_events[0]["target"] == "openai"
    assert set_events[0]["outcome"] == "ok"

    run_cli(["provider", "keys", "remove", "openai", "--yes"])

    remove_events = isolated_event_log.query(action="provider_key.remove")
    assert len(remove_events) == 1
    assert remove_events[0]["target"] == "openai"
    assert remove_events[0]["outcome"] == "ok"

    # The raw key never appears in any event row.
    assert "sk-audit-1234" not in repr(isolated_event_log.query())


def test_provider_key_migrate_records_audit_events(
    fake_keyring, env_file, keyring_on, run_cli, isolated_event_log
):
    _set_env(env_file, "NVIDIA_API_KEY", "nv-key-audit")

    run_cli(["provider", "keys", "migrate", "--yes"])

    migrate_events = isolated_event_log.query(action="provider_key.migrate")
    assert len(migrate_events) == 1
    assert migrate_events[0]["target"] == "nvidia"
    assert migrate_events[0]["detail"] == {
        "source": "env",
        "destination": "keyring",
    }
    assert "nv-key-audit" not in repr(migrate_events)


# ---------------------------------------------------- idempotent / conflict

def test_migrate_rerun_is_noop(fake_keyring, env_file, keyring_on, run_cli):
    _set_env(env_file, "NVIDIA_API_KEY", "nv-key-1234")
    run_cli(["provider", "keys", "migrate", "--yes"])

    out, _ = run_cli(["provider", "keys", "migrate", "--yes"])

    assert "Nothing to migrate" in out
    assert fake_keyring.get_password("relay", "nvidia") == "nv-key-1234"
    assert "NVIDIA_API_KEY" not in env_file.read_text(encoding="utf-8")


def test_migrate_already_present_skips(fake_keyring, env_file, keyring_on, run_cli):
    _set_env(env_file, "NVIDIA_API_KEY", "same-key")
    fake_keyring.set_password("relay", "nvidia", "same-key")

    out, _ = run_cli(["provider", "keys", "migrate", "--yes"])

    assert "Nothing to migrate" in out
    assert "NVIDIA_API_KEY" in env_file.read_text(encoding="utf-8")


def test_migrate_conflict_skipped_without_force(
    fake_keyring, env_file, keyring_on, run_cli
):
    _set_env(env_file, "NVIDIA_API_KEY", "env-key-1234")
    fake_keyring.set_password("relay", "nvidia", "keyring-old-9999")

    with pytest.raises(SystemExit) as exc:
        run_cli(["provider", "keys", "migrate", "--yes"])
    assert exc.value.code == 1

    assert fake_keyring.get_password("relay", "nvidia") == "keyring-old-9999"
    assert "NVIDIA_API_KEY" in env_file.read_text(encoding="utf-8")


def test_migrate_conflict_reports_masked_tails_only(
    fake_keyring, env_file, keyring_on, run_cli, capsys
):
    _set_env(env_file, "NVIDIA_API_KEY", "env-key-1234")
    fake_keyring.set_password("relay", "nvidia", "keyring-old-9999")

    with pytest.raises(SystemExit):
        run_cli(["provider", "keys", "migrate", "--yes"])
    out, err = capsys.readouterr()

    assert mask_key("env-key-1234") in err
    assert mask_key("keyring-old-9999") in err
    assert "env-key-1234" not in out
    assert "env-key-1234" not in err


def test_migrate_conflict_force_overwrites(
    fake_keyring, env_file, keyring_on, run_cli
):
    _set_env(env_file, "NVIDIA_API_KEY", "env-key-1234")
    fake_keyring.set_password("relay", "nvidia", "keyring-old-9999")

    out, _ = run_cli(["provider", "keys", "migrate", "--force", "--yes"])

    assert fake_keyring.get_password("relay", "nvidia") == "env-key-1234"
    assert "NVIDIA_API_KEY" not in env_file.read_text(encoding="utf-8")


# ----------------------------------------------------- keyring write failure

def test_migrate_write_failure_aborts_before_env_removal(
    env_file, keyring_on, run_cli, monkeypatch
):
    monkeypatch.setenv("RELAY_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    _set_env(env_file, "NVIDIA_API_KEY", "nv-key-1234")

    with pytest.raises(SystemExit) as exc:
        run_cli(["provider", "keys", "migrate", "--yes"])
    assert exc.value.code == 1

    assert "NVIDIA_API_KEY" in env_file.read_text(encoding="utf-8")


# ----------------------------------------------------------- confirmation

def test_migrate_refuses_noninteractive_without_yes(
    fake_keyring, env_file, keyring_on, run_cli
):
    _set_env(env_file, "NVIDIA_API_KEY", "nv-key-1234")

    with pytest.raises(SystemExit) as exc:
        run_cli(["provider", "keys", "migrate"])
    assert exc.value.code == 1

    assert fake_keyring.get_password("relay", "nvidia") is None
    assert "NVIDIA_API_KEY" in env_file.read_text(encoding="utf-8")


# ------------------------------------------------------- never-print rules

def test_migrate_never_prints_secrets(
    fake_keyring, env_file, keyring_on, run_cli, capsys
):
    _set_env(env_file, "NVIDIA_API_KEY", "nvapi-super-secret-1234567890")

    run_cli(["provider", "keys", "migrate", "--yes"])
    out, err = capsys.readouterr()

    assert "nvapi-super-secret-1234567890" not in out
    assert "nvapi-super-secret-1234567890" not in err


# --------------------------------------------------------------- warnings

def test_migrate_warns_when_keyring_disabled(
    fake_keyring, env_file, run_cli
):
    _set_env(env_file, "NVIDIA_API_KEY", "nv-key-1234")

    out, err = run_cli(["provider", "keys", "migrate", "--yes"])

    assert "RELAY_KEYRING is not true" in err
    assert fake_keyring.get_password("relay", "nvidia") == "nv-key-1234"


# --------------------------------------------------------------- selection

def test_migrate_keyless_provider_noop(fake_keyring, env_file, keyring_on, run_cli):
    out, _ = run_cli(["provider", "keys", "migrate", "--provider", "ollama", "--yes"])

    assert "No cloud providers" in out


def test_migrate_empty_env_noop(fake_keyring, env_file, keyring_on, run_cli):
    out, _ = run_cli(["provider", "keys", "migrate", "--yes"])

    assert "Nothing to migrate" in out


def test_migrate_unknown_provider_exits_2(keyring_on, run_cli):
    with pytest.raises(SystemExit) as exc:
        run_cli(["provider", "keys", "migrate", "--provider", "nope"])
    assert exc.value.code == 2
