"""
CLI tests for the P5 Phase 3 key-management commands.

Invokes ``app.cli.main(argv=[...])`` (same pattern as ``test_packaging.py``)
with a temp-path KeyStore injected through ``app.cli.keys._store``, and a
monkeypatched ``config_store.env_file`` / keyring backend for the provider
commands.
"""

import io
import json
import sys

import pytest

from app.services import config_store
from app.services.key_store import KeyStore


@pytest.fixture
def store(monkeypatch, tmp_path):
    instance = KeyStore(tmp_path / "relay_keys.db")
    monkeypatch.setattr("app.cli.keys._store", lambda: instance)
    yield instance
    instance.close()


@pytest.fixture
def run_cli(capsys):
    from app.cli import main

    def _run(argv):
        main(argv)
        out, err = capsys.readouterr()
        return out, err

    return _run


@pytest.fixture
def no_keyring(monkeypatch, tmp_path):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_keyring_enabled", False)
    monkeypatch.setattr(config_store, "env_file", tmp_path / ".env")


def _extract_added_key(out):
    return next(
        line for line in out.splitlines() if line.startswith("API Key: ")
    ).split(": ", 1)[1]


# ----------------------------------------------------------- CLI parsing

def test_unknown_subcommand_exits_2(run_cli):
    with pytest.raises(SystemExit) as exc:
        run_cli(["keys", "bogus"])
    assert exc.value.code == 2


def test_keys_add_requires_label(store, run_cli):
    with pytest.raises(SystemExit) as exc:
        run_cli(["keys", "add"])
    assert exc.value.code == 2


def test_keys_add_requires_positive_expiry(store, run_cli):
    with pytest.raises(SystemExit) as exc:
        run_cli(["keys", "add", "--label", "x", "--expires-days", "0"])
    assert exc.value.code == 2


def test_provider_keys_unknown_provider(no_keyring, run_cli):
    with pytest.raises(SystemExit) as exc:
        run_cli(["provider", "keys", "set", "nope", "sk-x"])
    assert exc.value.code == 1


def test_provider_keys_rejects_keyless_provider(no_keyring, run_cli):
    with pytest.raises(SystemExit) as exc:
        run_cli(["provider", "keys", "set", "ollama", "sk-x"])
    assert exc.value.code == 2


def test_provider_keys_remove_rejects_keyless_provider(no_keyring, run_cli):
    with pytest.raises(SystemExit) as exc:
        run_cli(["provider", "keys", "remove", "ollama"])
    assert exc.value.code == 2


# ---------------------------------------------------------- add/remove/list

def test_keys_add_prints_raw_once_and_verifies(store, run_cli):
    out, _ = run_cli(["keys", "add", "--label", "opencode"])
    raw = _extract_added_key(out)

    assert out.count(raw) == 1
    assert out.count("rl_") == 1
    assert "Shown once" in out

    meta = store.verify(raw)
    assert meta is not None
    assert meta["label"] == "opencode"


def test_keys_add_scopes_and_expiry(store, run_cli):
    out, _ = run_cli(
        ["keys", "add", "--label", "ci", "--scopes", "chat,v1",
         "--expires-days", "30"]
    )
    raw = _extract_added_key(out)
    assert "chat,v1" in out

    meta = store.verify(raw)
    assert meta["scopes"] == ["chat", "v1"]
    assert meta["expires_at"] is not None


def test_keys_list_never_shows_raw(store, run_cli):
    _, raw = store.create("direct", scopes=["chat"])
    out, _ = run_cli(["keys", "list"])

    assert "direct" in out
    assert raw not in out
    assert "rl_" not in out


def test_keys_remove_requires_yes_noninteractive(store, run_cli):
    key_id, raw = store.create("opencode")

    with pytest.raises(SystemExit) as exc:
        run_cli(["keys", "remove", key_id])
    assert exc.value.code == 1
    assert store.verify(raw) is not None


def test_keys_remove_yes_revokes(store, run_cli):
    key_id, raw = store.create("opencode")
    out, _ = run_cli(["keys", "remove", key_id, "--yes"])

    assert "Revoked" in out
    assert store.verify(raw) is None
    assert store.list()[0]["revoked_at"] is not None


def test_keys_remove_accepts_shortened_id(store, run_cli):
    key_id, raw = store.create("opencode")
    out, _ = run_cli(["keys", "remove", key_id[:8], "--yes"])
    assert "Revoked" in out
    assert store.verify(raw) is None


def test_keys_remove_unknown_id(store, run_cli):
    with pytest.raises(SystemExit) as exc:
        run_cli(["keys", "remove", "deadbeef", "--yes"])
    assert exc.value.code == 1


# ------------------------------------------------------------- test outcomes

def test_keys_test_outcomes(store, run_cli):
    _, raw = store.create("active", scopes=["chat"])
    out, _ = run_cli(["keys", "test", raw])
    assert out.strip() == "ok active"

    out, _ = run_cli(["keys", "test", "rl_" + "a" * 43])
    assert out.strip() == "invalid"


def test_keys_test_expired_and_revoked(store, run_cli):
    _, expired_raw = store.create("old", expires_at=0)
    out, _ = run_cli(["keys", "test", expired_raw])
    assert out.strip() == "expired"

    revoked_id, revoked_raw = store.create("gone")
    store.revoke(revoked_id)
    out, _ = run_cli(["keys", "test", revoked_raw])
    assert out.strip() == "revoked"


def test_keys_test_stdin(store, run_cli, monkeypatch):
    _, raw = store.create("via-stdin")
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw + "\n"))
    out, _ = run_cli(["keys", "test", "-"])
    assert out.strip() == "ok via-stdin"


def test_keys_test_never_echoes_key(store, run_cli):
    out, _ = run_cli(["keys", "test", "rl_" + "b" * 43])
    assert "rl_" not in out


# ------------------------------------------------------------------- --json

def test_keys_list_json_has_full_id_no_raw(store, run_cli):
    key_id, raw = store.create("json-key")
    out, _ = run_cli(["keys", "list", "--json"])

    payload = json.loads(out)
    assert len(payload) == 1
    assert payload[0]["id"] == key_id
    assert payload[0]["label"] == "json-key"
    assert raw not in out


def test_keys_add_json_includes_raw(store, run_cli):
    out, _ = run_cli(["keys", "add", "--label", "machine", "--json"])

    payload = json.loads(out)
    raw = payload["api_key"]
    assert raw.startswith("rl_")
    assert out.count(raw) == 1
    assert store.verify(raw)["label"] == "machine"


# ---------------------------------------------------- provider keys (env)

def test_provider_keys_set_writes_env(no_keyring, run_cli, tmp_path):
    out, _ = run_cli(["provider", "keys", "set", "nvidia", "sk-env-1234"])
    assert "Stored key for nvidia" in out

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "NVIDIA_API_KEY" in env_text
    assert "sk-env-1234" in env_text


def test_provider_keys_remove_clears_env(no_keyring, run_cli, tmp_path):
    run_cli(["provider", "keys", "set", "nvidia", "sk-env-1234"])
    out, _ = run_cli(["provider", "keys", "remove", "nvidia"])
    assert "Removed key for nvidia" in out

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "sk-env-1234" not in env_text


def test_provider_keys_set_stdin(no_keyring, run_cli, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "stdin", io.StringIO("sk-stdin-5678\n"))
    out, _ = run_cli(["provider", "keys", "set", "nvidia", "-"])
    assert "Stored key for nvidia" in out

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "sk-stdin-5678" in env_text


# ------------------------------------------------- provider keys (keyring)

class _FakeKeyring:
    def __init__(self):
        self.data = {}

    def get(self, provider_id):
        return self.data.get(provider_id, "")

    def set(self, provider_id, value):
        self.data[provider_id] = value

    def remove(self, provider_id):
        self.data.pop(provider_id, None)


def _enable_keyring(monkeypatch):
    from app.core.config import settings

    fake = _FakeKeyring()
    monkeypatch.setattr(settings, "relay_keyring_enabled", True)
    monkeypatch.setattr(config_store, "provider_key_store", fake)

    import app.providers.factory as factory

    monkeypatch.setattr(factory, "provider_key_store", fake)
    return fake


def test_provider_keys_keyring_lifecycle(monkeypatch, run_cli, tmp_path):
    fake = _enable_keyring(monkeypatch)

    out, _ = run_cli(["provider", "keys", "set", "openai", "sk-keyring-9876"])
    assert "Stored key for openai" in out
    assert fake.get("openai") == "sk-keyring-9876"

    out, _ = run_cli(["provider", "keys", "remove", "openai"])
    assert "Removed key for openai" in out
    assert fake.get("openai") == ""

    out, _ = run_cli(["provider", "keys", "remove", "openai"])
    assert "Removed key for openai" in out


# ------------------------------------------------- provider keys (masking)

def test_provider_keys_list_masks_values(no_keyring, run_cli, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "nvidia_api_key", "sk-abcdef123456")
    out, _ = run_cli(["provider", "keys", "list"])

    assert "nvidia" in out
    assert "sk-abcdef123456" not in out
    assert "********3456" in out


def test_provider_keys_list_masks_short_keys(no_keyring, run_cli, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "nvidia_api_key", "abc")
    out, _ = run_cli(["provider", "keys", "list"])

    assert "abc" not in out
    assert "***" in out


def test_provider_keys_list_json_no_raw(no_keyring, run_cli, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "nvidia_api_key", "sk-abcdef123456")
    out, _ = run_cli(["provider", "keys", "list", "--json"])

    payload = json.loads(out)
    nvidia = next(row for row in payload if row["id"] == "nvidia")
    assert nvidia["has_key"] is True
    assert nvidia["key"] == "********3456"
    assert "sk-abcdef123456" not in out
