"""
Tests for the shared platform database owner (P6.1).

Covers fresh open at the combined schema version, migration idempotency,
WAL/busy timeout, POSIX permissions, corrupt backup-and-reopen, the D6
legacy-unmigrated guard, concurrent opens of the same file, and rejection
of newer-than-supported schemas.
"""

import os
import stat
import sqlite3
import threading

import pytest

import app.services.platform_store as platform_store
from app.services.platform_store import PlatformStoreError

_FULL_TABLE_SET = {
    "api_keys",
    "learned_state",
    "telemetry",
    "telemetry_failures",
    "quality_aggregates",
    "decision_stats",
    "model_status",
    "events",
    "request_log",
    "conversations",
    "conversation_turns",
    "summaries",
    "compaction_records",
    "project_state",
    "resume_replays",
}


class TestOpenAndSchema:
    def test_fresh_open_has_full_table_set(self, tmp_path):
        conn = platform_store.open_connection(str(tmp_path / "fresh.db"))
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        conn.close()

        assert version == platform_store.SCHEMA_VERSION
        assert _FULL_TABLE_SET <= tables

    def test_reopen_is_idempotent(self, tmp_path):
        path = str(tmp_path / "stable.db")
        first = platform_store.open_connection(path)
        first.close()

        second = platform_store.open_connection(path)
        version = second.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in second.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        second.close()

        assert version == platform_store.SCHEMA_VERSION
        assert _FULL_TABLE_SET <= tables

    def test_migration_from_scratch_history(self, tmp_path):
        # A fresh, empty file starts at user_version 0 and runs the full
        # history (v1 -> v8), including api_keys, events, request_log, the
        # project-continuity tables, and the resume_replays tracker.
        path = str(tmp_path / "history.db")

        platform_conn = platform_store.open_connection(path)
        version = platform_conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in platform_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        platform_conn.close()

        assert version == platform_store.SCHEMA_VERSION == 8
        assert _FULL_TABLE_SET <= tables

    def test_v4_to_v6_additive_upgrade(self, tmp_path):
        # Build an explicit v4 database (migrations 1..4 only), write a
        # sentinel key, then let open_connection advance it to the current
        # schema. The upgrade is additive: existing rows survive
        # byte-identical.
        path = str(tmp_path / "v4.db")
        conn = sqlite3.connect(path)

        for target in range(1, 5):
            for statement in platform_store.MIGRATIONS[target]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {target}")

        conn.execute(
            "INSERT INTO api_keys (id, key_hash, key_salt, kdf, label,"
            " scopes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "sentinel-key",
                b"\x01" * 32,
                b"\x02" * 16,
                "argon2",
                "kept",
                '["chat"]',
                1234.0,
            ),
        )
        conn.commit()
        conn.close()

        opened = platform_store.open_connection(path)
        version = opened.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in opened.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        label = opened.execute(
            "SELECT label FROM api_keys WHERE id = 'sentinel-key'"
        ).fetchone()[0]
        opened.close()

        assert version == platform_store.SCHEMA_VERSION
        assert "events" in tables
        assert "request_log" in tables
        assert label == "kept"

    def test_newer_schema_version_rejected(self, tmp_path):
        path = tmp_path / "newer.db"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()

        with pytest.raises(PlatformStoreError):
            platform_store.open_connection(str(path))

    def test_wal_and_busy_timeout(self, tmp_path):
        conn = platform_store.open_connection(str(tmp_path / "t.db"))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        conn.close()

        assert mode.lower() == "wal"
        assert timeout == 5000


class TestPermissionsAndCorruption:
    def test_file_permissions_user_only(self, tmp_path):
        if os.name == "nt":
            pytest.skip("POSIX permission check")

        path = tmp_path / "perm.db"
        conn = platform_store.open_connection(str(path))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.close()

        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_sidecar_permissions_user_only(self, tmp_path):
        if os.name == "nt":
            pytest.skip("POSIX permission check")

        path = tmp_path / "sidecar.db"
        conn = platform_store.open_connection(str(path))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        conn.close()

        for suffix in ("", "-wal", "-shm"):
            candidate = tmp_path / f"sidecar.db{suffix}"

            if not candidate.exists():
                continue

            mode = stat.S_IMODE(os.stat(candidate).st_mode)
            assert mode == 0o600, suffix

    def test_corrupt_backup_and_reopen(self, tmp_path):
        path = tmp_path / "corrupt.db"
        path.write_bytes(b"this is not a sqlite database at all")

        conn = platform_store.open_connection(str(path))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.close()

        backups = list(tmp_path.glob("corrupt.db.corrupt-*.bak"))
        assert len(backups) == 1
        assert backups[0].read_bytes().startswith(b"this is not a sqlite")

        conn = platform_store.open_connection(str(path))
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == platform_store.SCHEMA_VERSION

    def test_corrupt_backup_permissions_user_only(self, tmp_path):
        if os.name == "nt":
            pytest.skip("POSIX permission check")

        path = tmp_path / "perm-corrupt.db"
        path.write_bytes(b"this is not a sqlite database at all")

        conn = platform_store.open_connection(str(path))
        conn.close()

        backups = list(tmp_path.glob("perm-corrupt.db.corrupt-*.bak"))
        assert len(backups) == 1
        mode = stat.S_IMODE(os.stat(backups[0]).st_mode)
        assert mode == 0o600


class TestConcurrency:
    def test_concurrent_opens_of_same_file(self, tmp_path):
        path = str(tmp_path / "shared.db")
        errors = []

        def worker():
            try:
                conn = platform_store.open_connection(path)
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
                conn.close()
            except Exception as exc:  # noqa: BLE001 - collected for assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()

        assert errors == []

        conn = platform_store.open_connection(path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == platform_store.SCHEMA_VERSION


class TestLegacyGuard:
    def _patch_layout(self, monkeypatch, tmp_path, with_legacy_keys=True):
        monkeypatch.delenv("RELAY_DATA_DIR", raising=False)
        monkeypatch.delenv("PERSISTENCE_PATH", raising=False)

        state = tmp_path / "state"
        state.mkdir(exist_ok=True)

        if with_legacy_keys:
            (state / "relay_keys.db").write_bytes(b"legacy")

        monkeypatch.setattr(platform_store, "state_dir", state)
        return state

    def test_guard_blocks_fresh_platform_db(self, tmp_path, monkeypatch):
        state = self._patch_layout(monkeypatch, tmp_path)

        assert not (state / "platform.db").exists()

        with pytest.raises(PlatformStoreError, match="relay migrate"):
            platform_store.open_connection(str(platform_store.default_path()))

        # Nothing was created.
        assert not (state / "platform.db").exists()

    def test_guard_inert_when_no_legacy_sources(self, tmp_path, monkeypatch):
        self._patch_layout(monkeypatch, tmp_path, with_legacy_keys=False)

        conn = platform_store.open_connection(str(platform_store.default_path()))
        conn.close()

        assert (tmp_path / "state" / "platform.db").exists()

    def test_guard_bypassed_with_relay_data_dir_override(
        self, tmp_path, monkeypatch
    ):
        self._patch_layout(monkeypatch, tmp_path)
        monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path / "override"))

        conn = platform_store.open_connection(str(platform_store.default_path()))
        conn.close()

        assert (tmp_path / "state" / "platform.db").exists()

    def test_guard_bypassed_with_explicit_non_default_path(
        self, tmp_path, monkeypatch
    ):
        self._patch_layout(monkeypatch, tmp_path)

        other = tmp_path / "other"
        other.mkdir()

        conn = platform_store.open_connection(str(other / "platform.db"))
        conn.close()

        assert (other / "platform.db").exists()
        assert not (tmp_path / "state" / "platform.db").exists()

    def test_guard_inert_when_target_already_exists(self, tmp_path, monkeypatch):
        state = self._patch_layout(monkeypatch, tmp_path)
        (state / "platform.db").write_bytes(b"migrated")

        conn = platform_store.open_connection(str(platform_store.default_path()))
        conn.close()
