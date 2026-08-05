"""
P6.2 key prune tests (D4).

Covers ``KeyStore.prune`` semantics (terminal-only, grace-window aware,
counts), the ``relay keys prune`` CLI (dry-run default, ``--yes``,
``--older-than-days``, ``--json``), active keys never touched, and the
``key.prune`` audit event.
"""

import json
import sqlite3
import time

import pytest

from app.services.key_store import KeyStore


@pytest.fixture
def store(tmp_path):
    instance = KeyStore(tmp_path / "relay_keys.db")
    yield instance
    instance.close()


def _rewrite_revoked_at(store, key_id, ts):
    conn = sqlite3.connect(store.path)
    conn.execute(
        "UPDATE api_keys SET revoked_at = ? WHERE id = ?", (ts, key_id)
    )
    conn.commit()
    conn.close()


# ------------------------------------------------------------ store

def test_prune_removes_only_terminal_rows_before_cutoff(store):
    now = time.time()

    old_revoked_id, _ = store.create("old-revoked")
    store.revoke(old_revoked_id)
    _rewrite_revoked_at(store, old_revoked_id, now - 100 * 86400)

    recent_revoked_id, _ = store.create("recent-revoked")
    store.revoke(recent_revoked_id)

    old_expired_id, _ = store.create("old-expired", expires_at=now - 100 * 86400)
    recent_expired_id, _ = store.create(
        "recent-expired", expires_at=now - 1 * 86400
    )
    active_id, _ = store.create("active")

    removed, scanned = store.prune(now - 30 * 86400)

    assert removed == 2
    assert scanned == 5

    remaining = {entry["id"] for entry in store.list()}
    assert remaining == {recent_revoked_id, recent_expired_id, active_id}


def test_prune_never_touches_active_keys(store):
    # An active key (no revoke, future expiry) survives even when it has
    # existed long before the cutoff.
    key_id, raw = store.create("active", expires_at=time.time() + 86400)

    removed, _ = store.prune(time.time() - 30 * 86400)
    assert removed == 0
    assert store.verify(raw) is not None
    assert store.get_by_id(key_id) is not None


def test_prune_cutoff_at_boundary(store):
    now = time.time()
    _, raw = store.create("boundary", expires_at=now - 30 * 86400)

    # Equal to the cutoff is removed (the predicate uses <= for expiry).
    removed, _ = store.prune(now - 30 * 86400)
    assert removed == 1
    assert store.verify(raw) is None


def test_prune_empty_store(tmp_path):
    store = KeyStore(tmp_path / "relay_keys.db")
    try:
        assert store.prune(time.time()) == (0, 0)
    finally:
        store.close()


# ------------------------------------------------------------------- CLI

@pytest.fixture
def run_cli(capsys):
    from app.cli import main

    def _run(argv):
        main(argv)
        out, err = capsys.readouterr()
        return out, err

    return _run


@pytest.fixture
def cli_store(monkeypatch, tmp_path):
    instance = KeyStore(tmp_path / "relay_keys.db")
    monkeypatch.setattr("app.cli.keys._store", lambda: instance)
    yield instance
    instance.close()


def _add_old_revoked(store):
    now = time.time()
    key_id, _ = store.create("stale")
    store.revoke(key_id)
    _rewrite_revoked_at(store, key_id, now - 100 * 86400)
    return key_id


def test_cli_prune_dry_run_by_default(cli_store, run_cli):
    key_id = _add_old_revoked(cli_store)
    out, _ = run_cli(["keys", "prune"])

    assert "1 terminal key(s)" in out
    assert "Dry run: nothing changed" in out
    assert cli_store.get_by_id(key_id) is not None


def test_cli_prune_yes_deletes(cli_store, run_cli):
    key_id = _add_old_revoked(cli_store)
    out, _ = run_cli(["keys", "prune", "--yes"])

    assert "Removed 1 terminal key(s)" in out
    assert cli_store.get_by_id(key_id) is None


def test_cli_prune_older_than_days_keeps_recent(cli_store, run_cli):
    now = time.time()
    old_id = _add_old_revoked(cli_store)
    recent_id, _ = cli_store.create("recent")
    cli_store.revoke(recent_id)
    _rewrite_revoked_at(cli_store, recent_id, now - 1 * 86400)

    out, _ = run_cli(["keys", "prune", "--older-than-days", "30", "--yes"])

    assert "Removed 1 terminal key(s)" in out
    assert cli_store.get_by_id(old_id) is None
    assert cli_store.get_by_id(recent_id) is not None


def test_cli_prune_active_never_listed(cli_store, run_cli):
    _, raw = cli_store.create("active")
    out, _ = run_cli(["keys", "prune"])

    assert "No terminal keys to prune" in out
    assert cli_store.verify(raw) is not None


def test_cli_prune_json_dry_run(cli_store, run_cli):
    _add_old_revoked(cli_store)
    out, _ = run_cli(["keys", "prune", "--json"])

    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert len(payload["candidates"]) == 1
    assert payload["candidates"][0]["label"] == "stale"


def test_cli_prune_json_with_yes(cli_store, run_cli):
    key_id = _add_old_revoked(cli_store)
    out, _ = run_cli(["keys", "prune", "--json", "--yes"])

    payload = json.loads(out)
    assert payload["dry_run"] is False
    assert len(payload["candidates"]) == 1
    assert cli_store.get_by_id(key_id) is None


def test_cli_prune_records_event(cli_store, run_cli, isolated_event_log):
    _add_old_revoked(cli_store)
    run_cli(["keys", "prune", "--yes"])

    events = isolated_event_log.query(action="key.prune")
    assert len(events) == 1
    assert events[0]["outcome"] == "ok"
    assert events[0]["actor"] == "cli"
    assert events[0]["detail"]["removed"] == 1


# ----------------------------------------------------- failure modes

def test_prune_locked_store_errors_cleanly(cli_store, run_cli):
    # A broken/locked store surfaces as a short error (exit 1), never a
    # traceback and never a partial delete.
    key_id = _add_old_revoked(cli_store)

    def _locked():
        raise RuntimeError("database is locked")

    cli_store.list = _locked

    with pytest.raises(SystemExit) as exc:
        run_cli(["keys", "prune", "--yes"])
    assert exc.value.code == 1

    # Nothing was deleted.
    assert cli_store.get_by_id(key_id) is not None
