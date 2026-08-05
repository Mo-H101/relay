"""
Tests for ``relay migrate`` (P6.1).

Covers the full migration matrix: dry-run no-op, full run with import +
integrity + backup set + manifest, idempotent re-run, missing optional
sources, digest-change re-import, row-count mismatch abort-and-restore,
the ``--yes`` guard, rollback, ``--data-dir``, raw key material never
printed, and ``.env`` provider keys never imported.
"""

import argparse
import json
import sqlite3

import pytest

import app.cli.migrate as migrate_module
from app.services import platform_store

# Legacy schema DDL (verbatim source DDL, matching the platform history).
_KEY_DDL = """
CREATE TABLE api_keys (
    id TEXT PRIMARY KEY,
    key_hash BLOB NOT NULL,
    key_salt BLOB NOT NULL,
    kdf TEXT NOT NULL,
    label TEXT NOT NULL,
    scopes TEXT NOT NULL,
    expires_at REAL,
    created_at REAL NOT NULL,
    last_used_at REAL,
    revoked_at REAL
)
"""

_STATE_DDL = [
    """
    CREATE TABLE learned_state (
        provider TEXT PRIMARY KEY,
        provider_status TEXT,
        provider_status_remaining_seconds REAL,
        model_marks TEXT NOT NULL,
        model_counts TEXT NOT NULL,
        provider_counts TEXT NOT NULL,
        provider_status_expires_wall REAL
    )
    """,
    """
    CREATE TABLE telemetry (
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        request_count INTEGER NOT NULL,
        success_count INTEGER NOT NULL,
        failure_count INTEGER NOT NULL,
        total_latency_ms INTEGER NOT NULL,
        ewma_success REAL,
        ewma_latency_ms REAL,
        last_updated_wall REAL,
        PRIMARY KEY (provider, model)
    )
    """,
    """
    CREATE TABLE telemetry_failures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        failure_type TEXT NOT NULL,
        ts REAL NOT NULL
    )
    """,
    """
    CREATE TABLE quality_aggregates (
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        sample_count INTEGER NOT NULL,
        positive_count INTEGER NOT NULL,
        negative_count INTEGER NOT NULL,
        ewma_score REAL,
        categories TEXT NOT NULL,
        last_updated_wall REAL,
        PRIMARY KEY (provider, model)
    )
    """,
    """
    CREATE TABLE decision_stats (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        decisions INTEGER NOT NULL,
        candidates INTEGER NOT NULL,
        selected TEXT NOT NULL,
        by_band TEXT NOT NULL,
        last_updated_wall REAL NOT NULL
    )
    """,
]

_ENV_PROVIDER_KEY = "sk-super-secret-provider-key"


def _build_legacy(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)

    # Legacy relay_keys.db (schema v1) with one key row.
    kconn = sqlite3.connect(data_dir / "relay_keys.db")
    kconn.execute(_KEY_DDL)
    kconn.execute("PRAGMA user_version = 1")
    kconn.execute(
        "INSERT INTO api_keys ("
        " id, key_hash, key_salt, kdf, label, scopes, expires_at,"
        " created_at, last_used_at, revoked_at)"
        " VALUES ('kid-1', x'00', x'01', 'scrypt|16384|8|1', 'opencode',"
        " '[\"chat\",\"v1\"]', NULL, 100.0, NULL, NULL)"
    )
    kconn.commit()
    kconn.close()

    # Legacy relay_state.db (schema v3) with one row per state table.
    sconn = sqlite3.connect(data_dir / "relay_state.db")

    for ddl in _STATE_DDL:
        sconn.execute(ddl)

    sconn.execute("PRAGMA user_version = 3")
    sconn.execute(
        "INSERT INTO learned_state ("
        " provider, provider_status, provider_status_remaining_seconds,"
        " model_marks, model_counts, provider_counts,"
        " provider_status_expires_wall)"
        " VALUES ('LM Studio', 'degraded', 30.0, '{\"qwen-7b\": {\"degraded\": "
        "1}}', '{}', '{\"timeout\": 1}', 1000.0)"
    )
    sconn.execute(
        "INSERT INTO telemetry ("
        " provider, model, request_count, success_count, failure_count,"
        " total_latency_ms, ewma_success, ewma_latency_ms, last_updated_wall)"
        " VALUES ('LM Studio', 'qwen-7b', 4, 3, 1, 400, 0.5, 100.0, 1000.0)"
    )
    sconn.execute(
        "INSERT INTO telemetry_failures (provider, model, failure_type, ts)"
        " VALUES ('LM Studio', 'qwen-7b', 'timeout', 900.0)"
    )
    sconn.execute(
        "INSERT INTO quality_aggregates ("
        " provider, model, sample_count, positive_count, negative_count,"
        " ewma_score, categories, last_updated_wall)"
        " VALUES ('LM Studio', 'qwen-7b', 3, 2, 1, 0.75, '{}', 1000.0)"
    )
    sconn.execute(
        "INSERT INTO decision_stats ("
        " id, decisions, candidates, selected, by_band, last_updated_wall)"
        " VALUES (1, 5, 10, '{}', '{}', 1000.0)"
    )
    sconn.commit()
    sconn.close()

    # availability.json with an overloaded model (maps to degraded).
    (data_dir / "availability.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "generated_at": 500.0,
                "providers": {
                    "nvidia": {
                        "generated_at": 500.0,
                        "models": [
                            {
                                "model": "meta/llama-3.3-70b-instruct",
                                "status": "overloaded",
                                "latency_ms": 12.0,
                                "status_code": 200,
                                "error": None,
                                "probed_at": 490.0,
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    # .env with a provider key (backed up, never imported).
    (data_dir / ".env").write_text(
        f"OPENAI_API_KEY={_ENV_PROVIDER_KEY}\n", encoding="utf-8"
    )


def _args(**kwargs):
    defaults = dict(dry_run=False, yes=False, rollback=None, data_dir=None)
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _parser():
    parser = argparse.ArgumentParser(prog="relay migrate")
    migrate_module.add_migrate_flags(parser)
    return parser


def _run(args):
    migrate_module._run_migrate(args, _parser())


def _manifest(data_dir):
    path = data_dir / "migration-manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


class TestDryRun:
    def test_dry_run_changes_nothing(self, tmp_path, capsys):
        data_dir = tmp_path / "data"
        _build_legacy(data_dir)

        _run(_args(dry_run=True, data_dir=str(data_dir)))
        out = capsys.readouterr().out

        assert "Target:" in out
        assert "api_keys=1" in out
        assert not (data_dir / "platform.db").exists()
        assert not (data_dir / "backups").exists()
        assert _manifest(data_dir) is None

    def test_dry_run_with_no_sources(self, tmp_path, capsys):
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        _run(_args(dry_run=True, data_dir=str(data_dir)))
        assert "nothing to migrate" in capsys.readouterr().out


class TestRun:
    def test_full_run_imports_and_verifies(self, tmp_path, capsys):
        data_dir = tmp_path / "data"
        _build_legacy(data_dir)

        _run(_args(yes=True, data_dir=str(data_dir)))

        out = capsys.readouterr().out
        assert "Migrated into" in out

        platform_path = data_dir / "platform.db"
        assert platform_path.exists()

        conn = sqlite3.connect(platform_path)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok"

        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == platform_store.SCHEMA_VERSION

        counts = {
            table: conn.execute(
                f"SELECT count(*) FROM {table}"
            ).fetchone()[0]
            for table in (
                "api_keys",
                "learned_state",
                "telemetry",
                "telemetry_failures",
                "quality_aggregates",
                "decision_stats",
                "model_status",
            )
        }
        status = conn.execute(
            "SELECT status FROM model_status WHERE provider = 'nvidia'"
        ).fetchone()[0]
        conn.close()

        assert counts == {
            "api_keys": 1,
            "learned_state": 1,
            "telemetry": 1,
            "telemetry_failures": 1,
            "quality_aggregates": 1,
            "decision_stats": 1,
            "model_status": 1,
        }
        assert status == "degraded"

        # Backup set contains every source (env included, never moved).
        backups = list((data_dir / "backups").glob("*"))
        assert len(backups) == 1
        names = {p.name for p in backups[0].iterdir()}
        assert {
            "relay_keys.db",
            "relay_state.db",
            "availability.json",
            ".env",
        } <= names

        # Legacy sources remain on disk, untouched.
        assert (data_dir / "relay_keys.db").exists()
        assert (data_dir / "relay_state.db").exists()

    def test_manifest_records_sources_and_state(self, tmp_path):
        data_dir = tmp_path / "data"
        _build_legacy(data_dir)

        _run(_args(yes=True, data_dir=str(data_dir)))

        manifest = _manifest(data_dir)
        assert manifest is not None
        assert manifest["status"] == "ok"
        assert manifest["platform_schema_version"] == platform_store.SCHEMA_VERSION
        assert "migrated_at" in manifest

        sources = manifest["sources"]
        assert len(sources) == 4
        assert sources[str(data_dir / "relay_keys.db")]["rows"] == {
            "api_keys": 1
        }
        assert sources[str(data_dir / "relay_state.db")]["rows"]["telemetry"] == 1
        assert sources[str(data_dir / "availability.json")]["rows"] == {
            "model_status": 1
        }
        assert sources[str(data_dir / ".env")]["rows"] is None

        for entry in sources.values():
            assert len(entry["sha256"]) == 64

    def test_idempotent_rerun(self, tmp_path, capsys):
        data_dir = tmp_path / "data"
        _build_legacy(data_dir)

        _run(_args(yes=True, data_dir=str(data_dir)))
        capsys.readouterr()

        _run(_args(yes=True, data_dir=str(data_dir)))
        out = capsys.readouterr().out

        assert "Already migrated" in out

        # No second backup was created.
        backups = list((data_dir / "backups").glob("*"))
        assert len(backups) == 1

    def test_digest_change_requires_reimport(self, tmp_path, capsys):
        data_dir = tmp_path / "data"
        _build_legacy(data_dir)

        _run(_args(yes=True, data_dir=str(data_dir)))
        capsys.readouterr()

        # Touching a source changes its digest -> re-import path.
        keys_db = data_dir / "relay_keys.db"
        keys_db.write_bytes(keys_db.read_bytes() + b"touched")

        _run(_args(yes=True, data_dir=str(data_dir)))
        out = capsys.readouterr().out
        assert "Migrated into" in out

        # Manifest now reflects the changed source; a second run is inert.
        _run(_args(yes=True, data_dir=str(data_dir)))
        assert "Already migrated" in capsys.readouterr().out

    def test_missing_optional_sources(self, tmp_path, capsys):
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Only availability.json + .env exist (no DBs).
        (data_dir / "availability.json").write_text(
            json.dumps({"schema": 1, "generated_at": None, "providers": {}}),
            encoding="utf-8",
        )
        (data_dir / ".env").write_text("RELAY_PORT=8000\n", encoding="utf-8")

        _run(_args(yes=True, data_dir=str(data_dir)))
        capsys.readouterr()

        assert (data_dir / "platform.db").exists()

        conn = sqlite3.connect(data_dir / "platform.db")
        counts = {
            table: conn.execute(
                f"SELECT count(*) FROM {table}"
            ).fetchone()[0]
            for table in (
                "api_keys",
                "learned_state",
                "telemetry",
                "telemetry_failures",
                "quality_aggregates",
                "decision_stats",
                "model_status",
            )
        }
        conn.close()

        assert counts == {
            "api_keys": 0,
            "learned_state": 0,
            "telemetry": 0,
            "telemetry_failures": 0,
            "quality_aggregates": 0,
            "decision_stats": 0,
            "model_status": 0,
        }

        manifest = _manifest(data_dir)
        assert manifest["status"] == "ok"
        assert set(manifest["sources"]) == {
            str(data_dir / "availability.json"),
            str(data_dir / ".env"),
        }


class TestSafety:
    def test_yes_guard_non_interactive(self, tmp_path, capsys):
        data_dir = tmp_path / "data"
        _build_legacy(data_dir)

        with pytest.raises(SystemExit) as exc:
            _run(_args(data_dir=str(data_dir)))

        assert exc.value.code == 1
        assert not (data_dir / "platform.db").exists()
        assert "pass --yes" in capsys.readouterr().err

    def test_row_count_mismatch_aborts_and_restores(self, tmp_path, monkeypatch, capsys):
        data_dir = tmp_path / "data"
        _build_legacy(data_dir)

        real = migrate_module._import_state_tables

        def inflated(platform, state_db):
            counts = real(platform, state_db)
            counts["telemetry"] = counts["telemetry"] + 999
            return counts

        monkeypatch.setattr(migrate_module, "_import_state_tables", inflated)

        with pytest.raises(SystemExit) as exc:
            _run(_args(yes=True, data_dir=str(data_dir)))

        assert exc.value.code == 1
        assert not (data_dir / "platform.db").exists()
        assert (data_dir / "relay_keys.db").exists()
        assert (data_dir / "relay_state.db").exists()

        manifest = _manifest(data_dir)
        assert manifest is not None
        assert manifest["status"] == "failed"

        backups = list((data_dir / "backups").glob("*"))
        assert len(backups) == 1

        err = capsys.readouterr().err
        assert "row count mismatch" in err
        assert "restored sources" in err

    def test_env_provider_keys_never_imported(self, tmp_path, capsys):
        data_dir = tmp_path / "data"
        _build_legacy(data_dir)

        _run(_args(yes=True, data_dir=str(data_dir)))
        capsys.readouterr()

        raw = (data_dir / "platform.db").read_bytes()
        assert _ENV_PROVIDER_KEY.encode("utf-8") not in raw

    def test_raw_key_material_never_printed(self, tmp_path, capsys):
        data_dir = tmp_path / "data"
        _build_legacy(data_dir)

        _run(_args(dry_run=True, data_dir=str(data_dir)))
        dry_out = capsys.readouterr().out

        _run(_args(yes=True, data_dir=str(data_dir)))
        run_out = capsys.readouterr().out
        run_err = capsys.readouterr().err

        combined = dry_out + run_out + run_err
        assert _ENV_PROVIDER_KEY not in combined
        assert "rl_" not in combined


class TestRollback:
    def test_rollback_last_restores_and_removes(self, tmp_path, capsys):
        data_dir = tmp_path / "data"
        _build_legacy(data_dir)

        _run(_args(yes=True, data_dir=str(data_dir)))
        capsys.readouterr()

        assert (data_dir / "platform.db").exists()

        _run(_args(yes=True, rollback="last", data_dir=str(data_dir)))

        assert not (data_dir / "platform.db").exists()
        assert (data_dir / "relay_keys.db").exists()
        assert (data_dir / "relay_state.db").exists()
        assert _manifest(data_dir) is None

        # After rollback the runtime guard re-engages: opening the default
        # path raises again until a re-migration.
        import app.services.platform_store as platform_store_module

        old = platform_store_module.state_dir
        platform_store_module.state_dir = data_dir
        try:
            with pytest.raises(platform_store.PlatformStoreError):
                platform_store.open_connection(str(platform_store.default_path()))
        finally:
            platform_store_module.state_dir = old

        out = capsys.readouterr().out
        assert "Restored sources" in out
        assert "Removed" in out

    def test_rollback_unknown_backup_fails(self, tmp_path, capsys):
        data_dir = tmp_path / "data"
        _build_legacy(data_dir)

        with pytest.raises(SystemExit) as exc:
            _run(_args(yes=True, rollback="does-not-exist", data_dir=str(data_dir)))

        assert exc.value.code == 1
        assert "Backup not found" in capsys.readouterr().err


class TestDataDir:
    def test_data_dir_override_layout(self, tmp_path):
        data_dir = tmp_path / "custom-data"
        _build_legacy(data_dir)

        _run(_args(yes=True, data_dir=str(data_dir)))

        assert (data_dir / "platform.db").exists()
        assert (data_dir / "migration-manifest.json").exists()
        assert (data_dir / "backups").exists()
