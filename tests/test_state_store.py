"""
Tests for the persistent StateStore and the export/import methods on
HealthStore and TelemetryStore.
"""

import json
import sqlite3
import threading
import time

import pytest

import app.services.state_store as state_store_module
from app.services.decision_engine import DecisionStats
from app.services.health_store import HealthStore
from app.services.quality import QualityStore
from app.services.state_store import MIGRATIONS, StateStore, StateStoreError
from app.services.telemetry import TelemetryStore


class FakeClock:
    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t


def make_learned(store, provider="LM Studio", model="qwen-7b"):
    store.record_failure(provider, model, "timeout")
    store.record_failure(provider, model, "timeout")
    store.record_failure(provider, model, "rate_limit")


class TestStateStoreDatabase:
    def test_fresh_database_created_and_versioned(self, tmp_path):
        path = tmp_path / "fresh.db"
        store = StateStore(str(path))

        assert store.load_learned_state() == {}
        assert store.load_telemetry() == []
        store.close()

        conn = sqlite3.connect(path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == StateStore.SCHEMA_VERSION

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        conn.close()

        assert {"learned_state", "telemetry", "telemetry_failures"} <= tables

    def test_memory_database_works(self):
        store = StateStore()
        store.save_learned_state(
            {"P": {"provider_status": None, "model_marks": {}}}
        )
        assert store.load_learned_state() == {
            "P": {
                "provider_status": None,
                "provider_status_remaining_seconds": None,
                "provider_status_expires_wall": None,
                "model_marks": {},
                "model_counts": {},
                "provider_counts": {},
            }
        }
        store.close()

    def test_schema_migration_forward(self, tmp_path, monkeypatch):
        path = tmp_path / "migrate.db"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = 1")
        conn.execute("CREATE TABLE learned_state (provider TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()

        monkeypatch.setattr(StateStore, "SCHEMA_VERSION", 2)
        monkeypatch.setattr(
            state_store_module,
            "MIGRATIONS",
            {
                1: MIGRATIONS[1],
                2: [
                    "ALTER TABLE learned_state"
                    " ADD COLUMN provider_status TEXT"
                ],
            },
        )

        store = StateStore(str(path))
        store.close()

        conn = sqlite3.connect(path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        columns = [
            row[1] for row in conn.execute("PRAGMA table_info(learned_state)")
        ]
        conn.close()

        assert version == 2
        assert "provider_status" in columns

    def test_v2_migrates_to_v3_additively(self, tmp_path):
        path = tmp_path / "v2.db"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = 2")
        conn.execute(
            "CREATE TABLE learned_state ("
            " provider TEXT PRIMARY KEY, provider_status TEXT,"
            " provider_status_remaining_seconds REAL, model_marks TEXT NOT NULL,"
            " model_counts TEXT NOT NULL, provider_counts TEXT NOT NULL,"
            " provider_status_expires_wall REAL)"
        )
        conn.execute(
            "CREATE TABLE telemetry ("
            " provider TEXT NOT NULL, model TEXT NOT NULL,"
            " request_count INTEGER NOT NULL, success_count INTEGER NOT NULL,"
            " failure_count INTEGER NOT NULL, total_latency_ms INTEGER NOT NULL,"
            " PRIMARY KEY (provider, model))"
        )
        conn.execute(
            "CREATE TABLE telemetry_failures ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL,"
            " model TEXT NOT NULL, failure_type TEXT NOT NULL, ts REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO telemetry (provider, model, request_count,"
            " success_count, failure_count, total_latency_ms)"
            " VALUES ('LM Studio', 'qwen-7b', 4, 3, 1, 400)"
        )
        conn.commit()
        conn.close()

        store = StateStore(str(path))
        assert store.stats()["schema_version"] == 3

        loaded = store.load_telemetry()
        assert loaded == [
            {
                "provider": "LM Studio",
                "model": "qwen-7b",
                "request_count": 4,
                "success_count": 3,
                "failure_count": 1,
                "total_latency_ms": 400,
                "ewma_success": None,
                "ewma_latency_ms": None,
                "last_updated_wall": None,
                "recent_failures": [],
            }
        ]
        store.close()

        conn = sqlite3.connect(path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 3

        telemetry_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(telemetry)")
        }
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        conn.close()

        assert {
            "ewma_success",
            "ewma_latency_ms",
            "last_updated_wall",
        } <= telemetry_columns
        assert {"quality_aggregates", "decision_stats"} <= tables

    def test_reopen_does_not_rerun_migrations(self, tmp_path):
        path = tmp_path / "stable.db"
        StateStore(str(path)).close()

        store = StateStore(str(path))
        assert store.load_telemetry() == []
        store.close()

        conn = sqlite3.connect(path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        assert version == StateStore.SCHEMA_VERSION

    def test_newer_schema_version_rejected(self, tmp_path):
        path = tmp_path / "newer.db"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()

        with pytest.raises(StateStoreError):
            StateStore(str(path))

    def test_wal_mode_enabled(self, tmp_path):
        path = tmp_path / "wal.db"
        store = StateStore(str(path))

        mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        store.close()

        assert mode.lower() == "wal"

    def test_corruption_backup_and_recovery(self, tmp_path):
        path = tmp_path / "state.db"
        path.write_bytes(b"this is not a sqlite database at all")

        store = StateStore(str(path))
        assert store.load_learned_state() == {}
        store.close()

        backups = list(tmp_path.glob("state.db.corrupt-*.bak"))
        assert len(backups) == 1
        assert backups[0].read_bytes().startswith(b"this is not a sqlite")

        store = StateStore(str(path))
        store.save_learned_state(
            {"P": {"provider_status": None, "model_marks": {}}}
        )
        assert store.load_learned_state() == {
            "P": {
                "provider_status": None,
                "provider_status_remaining_seconds": None,
                "provider_status_expires_wall": None,
                "model_marks": {},
                "model_counts": {},
                "provider_counts": {},
            }
        }
        store.close()


class TestLearnedRoundTrip:
    def test_save_load_round_trip(self, tmp_path):
        source = HealthStore()
        make_learned(source)

        exported = source.export_learned_state()
        assert set(exported) == {"LM Studio"}

        store = StateStore(str(tmp_path / "state.db"))
        store.save_learned_state(exported)

        loaded = store.load_learned_state()
        assert set(loaded) == {"LM Studio"}
        assert loaded["LM Studio"]["provider_status"] == "degraded"
        assert loaded["LM Studio"]["provider_status_remaining_seconds"] > 0
        assert "qwen-7b" in loaded["LM Studio"]["model_marks"]
        assert "degraded" in loaded["LM Studio"]["model_marks"]["qwen-7b"]
        assert "rate_limit" in loaded["LM Studio"]["provider_counts"]

        restored = HealthStore()
        restored.import_learned_state(loaded)

        state = restored.learned("LM Studio")
        assert state is not None
        assert state.provider_status == "degraded"
        assert state.degraded_models == {"qwen-7b"}

    def test_import_rebuilds_monotonic_expiry(self):
        source_clock = FakeClock()
        target_clock = FakeClock()
        source = HealthStore(now=source_clock)
        make_learned(source)

        exported = source.export_learned_state()
        assert exported["LM Studio"]["provider_status_remaining_seconds"] > 50

        restored = HealthStore(now=target_clock)
        restored.import_learned_state(exported)

        assert restored.learned("LM Studio") is not None

        target_clock.t = 61
        assert restored.learned("LM Studio") is None

    def test_expired_state_removed_on_export(self):
        clock = FakeClock()
        store = HealthStore(now=clock)
        store.record_failure("P", "m", "timeout")
        store.record_failure("P", "m", "timeout")

        assert store.learned("P") is not None

        clock.t = 61
        assert store.export_learned_state() == {}
        assert store.learned("P") is None

    def test_counters_survive_without_marks(self):
        clock = FakeClock()
        store = HealthStore(now=clock)
        store.record_failure("P", "m", "timeout")

        exported = store.export_learned_state()
        assert "P" in exported
        assert exported["P"]["model_counts"]["m"]["timeout"][0] == 1

    def test_success_clears_marks_but_keeps_raw_counters(self):
        clock = FakeClock()
        store = HealthStore(now=clock)
        make_learned(store)
        store.record_success("LM Studio", "qwen-7b")

        exported = store.export_learned_state()
        assert exported["LM Studio"]["provider_status"] is None
        assert exported["LM Studio"]["model_marks"] == {}
        assert exported["LM Studio"]["model_counts"] == {}


class TestTelemetryRoundTrip:
    def test_save_load_round_trip(self, tmp_path):
        source = TelemetryStore(max_failure_history=10)
        source.record_attempt("LM Studio", "qwen-7b", True, latency_ms=120)
        source.record_attempt(
            "LM Studio",
            "qwen-7b",
            False,
            latency_ms=80,
            failure_type="timeout",
        )
        source.record_attempt(
            "OpenAI", "gpt-4o", False, latency_ms=5, failure_type="server_error"
        )

        store = StateStore(str(tmp_path / "state.db"))
        store.save_telemetry(source.export_state())

        loaded = store.load_telemetry()
        restored = TelemetryStore(max_failure_history=10)
        restored.import_state(loaded)

        lm = restored.get("LM Studio", "qwen-7b")
        assert lm is not None
        assert lm.request_count == 2
        assert lm.success_count == 1
        assert lm.failure_count == 1
        assert lm.average_latency_ms == 100.0

        oa = restored.get("OpenAI", "gpt-4o")
        assert oa is not None
        assert oa.failure_count == 1

        events = restored.recent_failures(
            "LM Studio", "qwen-7b", window_seconds=60
        )
        assert len(events) == 1
        assert events[0].failure_type == "timeout"

    def test_monotonic_wall_clock_conversion(self):
        source = TelemetryStore(max_failure_history=5)
        source.record_attempt(
            "P", "m", False, latency_ms=10, failure_type="timeout"
        )

        exported = source.export_state()
        wall_ts = exported[0]["recent_failures"][0]["ts"]
        assert abs(wall_ts - time.time()) < 5

        restored = TelemetryStore(max_failure_history=5)
        restored.import_state(exported)

        events = restored.recent_failures("P", "m", window_seconds=60)
        assert len(events) == 1
        assert events[0].failure_type == "timeout"

        stats = restored.get("P", "m")
        assert stats.request_count == 1
        assert stats.failure_count == 1

    def test_failure_history_capped_on_import(self):
        exported = {
            "provider": "P",
            "model": "m",
            "request_count": 10,
            "success_count": 0,
            "failure_count": 10,
            "total_latency_ms": 0,
            "recent_failures": [
                {"failure_type": "timeout", "ts": time.time() - i}
                for i in range(10)
            ],
        }

        restored = TelemetryStore(max_failure_history=3)
        restored.import_state([exported])

        assert len(restored.recent_failures("P", "m")) == 3

    def test_import_replaces_existing_data(self):
        source = TelemetryStore()
        source.record_attempt("A", "m1", True, latency_ms=1)
        restored = TelemetryStore()
        restored.record_attempt("B", "m2", True, latency_ms=2)
        restored.import_state(source.export_state())

        assert restored.get("A", "m1") is not None
        assert restored.get("B", "m2") is None


class TestQualityRoundTrip:
    def test_save_load_round_trip(self, tmp_path):
        source = QualityStore(min_samples=3)
        source.record("LM Studio", "qwen-7b", 5, category="speed")
        source.record("LM Studio", "qwen-7b", 4, category="accuracy")
        source.record("LM Studio", "qwen-7b", 2)

        store = StateStore(str(tmp_path / "state.db"))
        store.save_quality(source.export_state())

        loaded = store.load_quality()
        assert len(loaded) == 1
        assert loaded[0]["provider"] == "LM Studio"
        assert loaded[0]["model"] == "qwen-7b"
        assert loaded[0]["sample_count"] == 3
        assert loaded[0]["positive_count"] == 2
        assert loaded[0]["negative_count"] == 1
        assert loaded[0]["categories"] == {"speed": 1, "accuracy": 1}
        assert loaded[0]["ewma_score"] == pytest.approx(source.export_state()[0]["ewma_score"])

        restored = QualityStore(min_samples=3)
        restored.import_state(loaded)

        signal = restored.quality_signal("LM Studio", "qwen-7b")
        assert signal is not None
        assert signal.sample_count == 3
        assert signal.confidence == 1.0
        assert signal.score == pytest.approx(loaded[0]["ewma_score"])

    def test_empty_round_trip(self, tmp_path):
        store = StateStore(str(tmp_path / "state.db"))
        store.save_quality([])
        assert store.load_quality() == []

    def test_import_replaces_existing_data(self):
        source = QualityStore()
        source.record("A", "m1", 5)
        restored = QualityStore()
        restored.record("B", "m2", 3)
        restored.import_state(source.export_state())

        assert restored.quality_signal("A", "m1") is not None
        assert restored.quality_signal("B", "m2") is None

    def test_monotonic_wall_clock_conversion(self):
        source = QualityStore()
        source.record("A", "m1", 5)

        exported = source.export_state()
        assert abs(exported[0]["last_updated_wall"] - time.time()) < 5

        restored = QualityStore()
        restored.import_state(exported)
        signal = restored.quality_signal("A", "m1")
        assert signal is not None


class TestDecisionStatsRoundTrip:
    def test_save_load_round_trip(self, tmp_path):
        source = DecisionStats()
        source.record(None, 4)
        source.record(None, 0)
        source.record(None, 3)

        store = StateStore(str(tmp_path / "state.db"))
        store.save_decision_stats(source.export_state())

        loaded = store.load_decision_stats()
        assert loaded is not None
        assert loaded["decisions"] == 3
        assert loaded["candidates"] == 7
        assert set(loaded["selected"]) == set(source.snapshot()["selected"])
        assert set(loaded["by_band"]) == set(source.snapshot()["by_band"])

        restored = DecisionStats()
        restored.import_state(loaded)
        snapshot = restored.snapshot()
        assert snapshot["decisions"] == 3
        assert snapshot["candidates"] == 7

    def test_empty_and_missing_decision_stats(self, tmp_path):
        store = StateStore(str(tmp_path / "state.db"))
        assert store.load_decision_stats() is None

        store.save_decision_stats({})
        assert store.load_decision_stats() is None

    def test_import_replaces_existing_data(self):
        source = DecisionStats()
        source.record(None, 2)
        restored = DecisionStats()
        restored.record(None, 9)
        restored.import_state(source.export_state())

        assert restored.snapshot()["decisions"] == 1
        assert restored.snapshot()["candidates"] == 2

    def test_malformed_import_defaults_to_zero(self):
        stats = DecisionStats()
        stats.import_state({"decisions": -3, "selected": {"A/m": "oops"}})

        snapshot = stats.snapshot()
        assert snapshot["decisions"] == 0


class TestSchemaVersion:
    def test_schema_version_reported_in_stats(self, tmp_path):
        store = StateStore(str(tmp_path / "state.db"))
        assert store.stats()["schema_version"] == StateStore.SCHEMA_VERSION
        store.close()

    def test_memory_counts_report_persisted_rows(self, tmp_path):
        store = StateStore(str(tmp_path / "state.db"))

        health = HealthStore()
        health.record_failure("P", "m", "timeout")
        quality = QualityStore()
        quality.record("P", "m", 5)
        decision = DecisionStats()
        decision.record(None, 2)
        telemetry = TelemetryStore()
        telemetry.record_attempt("P", "m", True, latency_ms=1)

        store.save_learned_state(health.export_learned_state())
        store.save_telemetry(telemetry.export_state())
        store.save_quality(quality.export_state())
        store.save_decision_stats(decision.export_state())

        assert store.memory_counts() == {
            "learned_providers": 1,
            "telemetry_pairs": 1,
            "quality_pairs": 1,
            "decision_stats_rows": 1,
        }
        store.close()


class TestQualityRetention:
    def test_old_quality_aggregates_pruned(self, tmp_path):
        store = StateStore(str(tmp_path / "state.db"))

        fresh = QualityStore()
        fresh.record("FRESH", "m", 5)
        stale = QualityStore()
        stale.record("STALE", "m", 5)

        export = stale.export_state()
        export[0]["last_updated_wall"] = time.time() - 40 * 86400
        store.save_quality(export)

        loaded = store.load_quality()
        assert len(loaded) == 1
        assert loaded[0]["provider"] == "STALE"

        store.prune_retention(30)
        assert store.load_quality() == []

        store.save_quality(fresh.export_state())
        store.prune_retention(30)
        assert len(store.load_quality()) == 1


class TestConcurrency:
    def test_concurrent_saves_are_atomic(self, tmp_path):
        store = StateStore(str(tmp_path / "state.db"))
        errors = []

        def writer(i):
            try:
                learned = {
                    f"P{i}": {
                        "provider_status": None,
                        "provider_status_remaining_seconds": None,
                        "model_marks": {},
                        "model_counts": {f"m{i}": {"timeout": [i, 30.0]}},
                        "provider_counts": {},
                    }
                }
                store.save_learned_state(learned)

                telemetry = [
                    {
                        "provider": f"P{i}",
                        "model": f"m{i}",
                        "request_count": i,
                        "success_count": i,
                        "failure_count": 0,
                        "total_latency_ms": i * 10,
                        "recent_failures": [],
                    }
                ]
                store.save_telemetry(telemetry)

                quality = [
                    {
                        "provider": f"P{i}",
                        "model": f"m{i}",
                        "sample_count": i + 1,
                        "positive_count": i,
                        "negative_count": 1,
                        "ewma_score": 0.5,
                        "categories": {},
                    }
                ]
                store.save_quality(quality)

                stats = DecisionStats()
                stats.record(None, i + 1)
                store.save_decision_stats(stats.export_state())
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(i,)) for i in range(8)
        ]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()

        assert errors == []

        learned = store.load_learned_state()
        telemetry = store.load_telemetry()

        for provider, data in learned.items():
            model_key = f"m{provider[1:]}"
            assert data["model_counts"][model_key]["timeout"][0] == int(
                provider[1:]
            )

        for entry in telemetry:
            assert entry["provider"] == f"P{int(entry['provider'][1:])}"

        assert store.load_decision_stats() is not None
        store.close()


class TestPrivacy:
    def test_no_prompt_or_response_content_stored(self, tmp_path):
        health = HealthStore()
        telemetry = TelemetryStore()
        quality = QualityStore()
        decision = DecisionStats()

        health.record_failure("P", "m", "timeout")
        telemetry.record_attempt(
            "P", "m", False, latency_ms=42, failure_type="timeout"
        )
        telemetry.record_attempt("P", "m", True, latency_ms=10)
        quality.record(
            "P", "m", 5, category="speed", correlation_id="cid-secret"
        )
        decision.record(None, 2)

        exported = {
            "learned": health.export_learned_state(),
            "telemetry": telemetry.export_state(),
            "quality": quality.export_state(),
            "decision": decision.export_state(),
        }

        serialized = json.dumps(exported)
        store = StateStore(str(tmp_path / "state.db"))
        store.save_learned_state(exported["learned"])
        store.save_telemetry(exported["telemetry"])
        store.save_quality(exported["quality"])
        store.save_decision_stats(exported["decision"])
        store.close()

        raw = (tmp_path / "state.db").read_bytes().decode(
            "utf-8", errors="ignore"
        )

        for secret in ("SECRET_PROMPT", "SECRET_RESPONSE", "Bearer sk-"):
            assert secret not in serialized
            assert secret not in raw

        assert "cid-secret" not in serialized
        assert "cid-secret" not in raw
