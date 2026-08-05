"""
P6.2 durable security-event log tests.

Covers write/read round-trips, bounded reads, retention pruning,
best-effort hot-path semantics (a store failure never raises and bumps
``relay_events_failed_total``), the synchronous admin path (failure
surfaces), redaction of raw-key-shaped detail before insert, and the
privacy contract of the ``events`` schema (no prompt/response/raw bytes).
"""

import json
import sqlite3
import time

import pytest

import app.services.platform_store as platform_store
from app.services.event_log import EVENT_ACTIONS, EventLog
from app.services.metrics import relay_metrics


@pytest.fixture(autouse=True)
def reset_metrics():
    relay_metrics.reset()
    yield
    relay_metrics.reset()


@pytest.fixture
def log(tmp_path):
    instance = EventLog(str(tmp_path / "platform.db"))
    yield instance
    instance.close()


# ------------------------------------------------------------ write / read

def test_emit_and_query_roundtrip(log):
    assert log.emit("key.create", actor="cli", target="k-1", outcome="ok")
    assert log.emit(
        "key.rotate",
        actor="cli",
        target="k-1",
        detail={"new_key_id": "k-2"},
    )
    assert log.emit("auth.failure", outcome="denied", detail={"reason": "forbidden"})

    rows = log.query()
    assert len(rows) == 3

    newest = rows[0]
    assert newest["action"] == "auth.failure"
    assert newest["outcome"] == "denied"
    assert newest["detail"] == {"reason": "forbidden"}
    assert newest["actor"] == "system"

    rotated = next(row for row in rows if row["action"] == "key.rotate")
    assert rotated["detail"] == {"new_key_id": "k-2"}
    assert rotated["target"] == "k-1"


def test_query_newest_first_and_bounded(log):
    for index in range(10):
        log.emit("key.create", actor="cli", target=f"k-{index}")

    rows = log.query(limit=3)
    assert len(rows) == 3
    assert rows[0]["target"] == "k-9"

    # Limits are clamped to the module ceiling, never dumped unbounded.
    many = log.query(limit=10 ** 6)
    assert len(many) <= 500


def test_query_filters(log):
    log.emit("key.create", target="a")
    log.emit("auth.success", outcome="ok")
    log.emit("auth.failure", outcome="failed")

    only_auth = log.query(action="auth.failure")
    assert len(only_auth) == 1
    assert only_auth[0]["outcome"] == "failed"

    only_failed = log.query(outcome="failed")
    assert len(only_failed) == 1
    assert only_failed[0]["action"] == "auth.failure"


def test_count(log):
    assert log.count() == 0
    log.emit("key.create", target="a")
    log.emit("key.create", target="b")
    assert log.count() == 2


def test_event_actions_vocabulary_bounded():
    assert "key.rotate" in EVENT_ACTIONS
    assert "key.prune" in EVENT_ACTIONS
    assert "config.reload" in EVENT_ACTIONS
    assert "migrate.run" in EVENT_ACTIONS
    assert "provider_key.migrate" in EVENT_ACTIONS
    # The log is security-only; no free-form buckets.
    assert all("." in action for action in EVENT_ACTIONS)


# ------------------------------------------------------ retention pruning

def test_prune_retention_removes_old_rows(log, tmp_path):
    log.emit("key.create", actor="cli", target="old")
    log.emit("key.create", actor="cli", target="recent")

    # Rewrite the first row's timestamp to be far in the past.
    path = str(tmp_path / "platform.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE events SET ts = ? WHERE target = 'old'",
        (time.time() - 40 * 86400,),
    )
    conn.commit()
    conn.close()

    assert log.prune_retention(30) == 1
    rows = log.query()
    assert [row["target"] for row in rows] == ["recent"]


def test_prune_retention_disabled_for_non_positive_days(log):
    log.emit("key.create", target="a")
    assert log.prune_retention(0) == 0
    assert log.prune_retention(-7) == 0
    assert log.count() == 1


# ----------------------------------------------------- best-effort hot path

def test_emit_never_raises_when_store_unavailable(monkeypatch, tmp_path):
    def _broken(path):
        raise RuntimeError("db gone")

    monkeypatch.setattr(platform_store, "open_connection", _broken)

    log = EventLog(str(tmp_path / "platform.db"))
    try:
        assert log.emit("auth.success") is False
    finally:
        log.close()

    assert relay_metrics.events_failed.value() == 1
    assert relay_metrics.events_written.value() == 0


def test_emit_raise_on_error_surfaces_admin_failure(monkeypatch, tmp_path):
    def _broken(path):
        raise RuntimeError("db gone")

    monkeypatch.setattr(platform_store, "open_connection", _broken)

    log = EventLog(str(tmp_path / "platform.db"))
    try:
        # ``_ensure_open`` converts the store-open failure to an OSError so
        # admin paths surface it as a 500.
        with pytest.raises(OSError):
            log.emit("key.revoke", actor="cli", raise_on_error=True)
    finally:
        log.close()

    assert relay_metrics.events_failed.value() == 1


def test_emit_counts_written(log):
    assert log.emit("key.create", target="a")
    assert log.emit("auth.failure", outcome="failed")
    assert relay_metrics.events_written.value() == 2


# ------------------------------------------------------- redaction / privacy

def test_detail_redacted_before_insert(log):
    raw = "rl_" + "a" * 43
    log.emit(
        "key.create",
        actor="cli",
        target="k-1",
        detail={
            "label": "plain label",
            "token": raw,
            "note": f"key={raw}",
        },
    )

    rows = log.query()
    assert rows[0]["detail"]["label"] == "plain label"
    assert raw not in repr(rows[0]["detail"])

    # The raw key never reaches the database bytes either.
    db_text = sqlite3.connect(log.path).execute(
        "SELECT detail FROM events"
    ).fetchone()[0]
    assert raw not in db_text
    assert json.loads(db_text)["label"] == "plain label"


def test_events_schema_is_security_metadata_only(log, tmp_path):
    conn = sqlite3.connect(log.path)
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(events)")
    }
    conn.close()

    assert {"id", "ts", "actor", "action", "target", "outcome", "detail"} == columns
    assert not {"prompt", "response", "message"} & columns


def test_close_is_idempotent(log):
    log.close()
    log.close()


# -------------------------------------------------------------- CLI tail


@pytest.fixture
def run_cli(capsys):
    from app.cli import main

    def _run(argv):
        main(argv)
        out, err = capsys.readouterr()
        return out, err

    return _run


def test_cli_events_tails_newest_first(monkeypatch, run_cli, isolated_event_log):
    isolated_event_log.emit("key.create", actor="cli", target="k-1")
    isolated_event_log.emit("auth.failure", actor="k-1", outcome="failed")
    out, _ = run_cli(["events"])

    assert "key.create" in out
    assert "auth.failure" in out
    lines = [line for line in out.splitlines() if "  " in line]
    assert lines[0].split()[2] == "auth.failure"  # newest first
    assert lines[1].split()[2] == "key.create"


def test_cli_events_filters_and_json(monkeypatch, run_cli, isolated_event_log):
    isolated_event_log.emit("key.create", actor="cli", target="k-1")
    isolated_event_log.emit("auth.failure", actor="k-1", outcome="failed")

    out, _ = run_cli(["events", "--action", "key.create", "--json"])
    payload = json.loads(out)
    assert len(payload) == 1
    assert payload[0]["action"] == "key.create"

    out, _ = run_cli(["events", "--outcome", "denied"])
    assert "No events." in out


def test_cli_events_invalid_outcome_is_error(monkeypatch, run_cli):
    with pytest.raises(SystemExit) as exc:
        run_cli(["events", "--outcome", "bogus"])
    assert exc.value.code == 2


def test_cli_events_log_outage_is_clean_error(monkeypatch, capsys):
    from app.cli import main
    from app.services import event_log as event_log_module

    def _broken():
        raise RuntimeError("db gone")

    monkeypatch.setattr(event_log_module, "event_log", _broken)

    with pytest.raises(SystemExit) as exc:
        main(["events"])
    assert exc.value.code == 1

    _, err = capsys.readouterr()
    assert "could not read the event log" in err
    assert "db gone" not in err
