"""
Integration tests for persistence wiring: Relay startup loading,
write-behind flushing, shutdown flush, retention, and privacy.
"""

import asyncio
import time

import pytest

import app.main as app_main
from app.core.config import settings
from app.core.relay import Relay
from app.services.state_store import StateStore


def disable_providers(monkeypatch):
    monkeypatch.setattr(settings, "nvidia_enabled", False)
    monkeypatch.setattr(settings, "openai_enabled", False)
    monkeypatch.setattr(settings, "lmstudio_enabled", False)


def enable_persistence(
    monkeypatch,
    path,
    flush_interval=60,
    retention_days=0,
):
    monkeypatch.setattr(settings, "persistence_enabled", True)
    monkeypatch.setattr(settings, "persistence_path", str(path))
    monkeypatch.setattr(
        settings,
        "persistence_flush_interval_seconds",
        flush_interval,
    )
    monkeypatch.setattr(settings, "persistence_retention_days", retention_days)
    monkeypatch.setattr(settings, "telemetry_enabled", True)
    monkeypatch.setattr(settings, "health_feedback_enabled", True)


def record_state(relay):
    relay.telemetry.record_attempt(
        "LM Studio", "qwen-7b", True, latency_ms=120
    )
    relay.telemetry.record_attempt(
        "LM Studio",
        "qwen-7b",
        False,
        latency_ms=80,
        failure_type="timeout",
    )
    relay.health_store.record_failure("LM Studio", "qwen-7b", "timeout")
    relay.health_store.record_failure("LM Studio", "qwen-7b", "timeout")


def make_provider(name, models, priority=1, api_key="test-key", enabled=True):
    from app.providers.base import Provider

    return Provider(
        name=name,
        base_url="https://example.invalid",
        api_key=api_key,
        enabled=enabled,
        models=models,
        priority=priority,
    )


def record_quality_decision(relay):
    relay.provider_manager.register(make_provider("A", ["a-1"], priority=10))
    relay.quality_store.record("A", "a-1", 5, category="speed")
    relay.decision_engine.decide(relay.provider_manager.ranked())


class TestStartupLoading:
    def test_state_survives_relay_restart(self, monkeypatch, tmp_path):
        path = tmp_path / "state.db"
        enable_persistence(monkeypatch, path)
        disable_providers(monkeypatch)

        r1 = Relay()
        record_state(r1)
        r1.state_flusher.flush()

        r2 = Relay()

        stats = r2.telemetry.get("LM Studio", "qwen-7b")
        assert stats is not None
        assert stats.request_count == 2
        assert stats.success_count == 1
        assert stats.failure_count == 1
        assert stats.average_latency_ms == 100.0

        learned = r2.health_store.learned("LM Studio")
        assert learned is not None
        assert "qwen-7b" in learned.degraded_models

    def test_quality_and_decision_stats_survive_restart(
        self, monkeypatch, tmp_path
    ):
        path = tmp_path / "state.db"
        enable_persistence(monkeypatch, path)
        disable_providers(monkeypatch)
        monkeypatch.setattr(settings, "quality_feedback_min_samples", 1)

        r1 = Relay()
        record_quality_decision(r1)
        r1.state_flusher.flush()

        r2 = Relay()

        signal = r2.quality_store.quality_signal("A", "a-1")
        assert signal is not None
        assert signal.sample_count == 1
        assert signal.score == 1.0
        assert signal.confidence == 1.0

        stats = r2.decision_engine.stats()
        assert stats["decisions"] == 1
        assert stats["candidates"] == 1
        assert stats["selected"] == {"A/a-1": 1}

    def test_expired_ttl_removed_after_downtime(self, monkeypatch, tmp_path):
        path = tmp_path / "state.db"
        monkeypatch.setattr(settings, "health_degraded_ttl_seconds", 1)
        enable_persistence(monkeypatch, path)
        disable_providers(monkeypatch)

        r1 = Relay()
        r1.health_store.record_failure("LM Studio", "qwen-7b", "timeout")
        r1.health_store.record_failure("LM Studio", "qwen-7b", "timeout")
        r1.state_flusher.flush()

        assert r1.health_store.learned("LM Studio") is not None

        time.sleep(1.2)

        r2 = Relay()
        assert r2.health_store.learned("LM Studio") is None

    def test_legacy_db_migrates_to_wall_clock_schema(self, monkeypatch, tmp_path):
        import sqlite3

        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "CREATE TABLE learned_state ("
            " provider TEXT PRIMARY KEY, provider_status TEXT,"
            " provider_status_remaining_seconds REAL, model_marks TEXT NOT NULL,"
            " model_counts TEXT NOT NULL, provider_counts TEXT NOT NULL)"
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
            " VALUES ('LM Studio', 'qwen-7b', 3, 2, 1, 300)"
        )
        conn.commit()
        conn.close()

        enable_persistence(monkeypatch, path)
        disable_providers(monkeypatch)

        relay = Relay()

        stats = relay.telemetry.get("LM Studio", "qwen-7b")
        assert stats is not None
        assert stats.request_count == 3
        assert stats.success_count == 2
        assert stats.failure_count == 1

        conn = sqlite3.connect(path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        conn.close()

        assert version == StateStore.SCHEMA_VERSION
        assert {"quality_aggregates", "decision_stats"} <= tables


class TestWriteBack:
    def test_periodic_flush_background_thread(self, monkeypatch, tmp_path):
        path = tmp_path / "state.db"
        enable_persistence(monkeypatch, path, flush_interval=1)
        disable_providers(monkeypatch)

        relay = Relay()
        relay.telemetry.record_attempt("LM Studio", "qwen-7b", True, latency_ms=5)

        relay.state_flusher.start()
        time.sleep(1.5)
        relay.state_flusher.stop()

        store = StateStore(str(path))
        loaded = store.load_telemetry()
        assert any(
            entry["provider"] == "LM Studio" and entry["model"] == "qwen-7b"
            for entry in loaded
        )

    def test_explicit_flush_writes_state(self, monkeypatch, tmp_path):
        path = tmp_path / "state.db"
        enable_persistence(monkeypatch, path)
        disable_providers(monkeypatch)

        relay = Relay()
        record_state(relay)
        relay.state_flusher.flush()

        store = StateStore(str(path))
        assert "LM Studio" in store.load_learned_state()
        assert store.load_telemetry()


class TestShutdownFlush:
    def test_shutdown_flush_via_lifespan(self, monkeypatch, tmp_path):
        path = tmp_path / "state.db"
        enable_persistence(monkeypatch, path)
        disable_providers(monkeypatch)

        test_relay = Relay()
        record_state(test_relay)

        monkeypatch.setattr(app_main, "relay", test_relay)

        async def run_lifespan():
            async with app_main.lifespan(app_main.app):
                pass

        asyncio.run(run_lifespan())

        store = StateStore(str(path))
        assert "LM Studio" in store.load_learned_state()

        loaded = store.load_telemetry()
        assert any(
            entry["provider"] == "LM Studio" and entry["model"] == "qwen-7b"
            for entry in loaded
        )


class TestDisabledAndPrivacy:
    def test_disabled_persistence_does_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "persistence_enabled", False)
        path = tmp_path / "should_not_exist.db"
        monkeypatch.setattr(settings, "persistence_path", str(path))
        disable_providers(monkeypatch)

        relay = Relay()

        assert relay.state_store is None
        assert relay.state_flusher is None

        record_state(relay)

        assert not path.exists()

    def test_no_user_data_reaches_sqlite(self, monkeypatch, tmp_path):
        path = tmp_path / "state.db"
        enable_persistence(monkeypatch, path)
        disable_providers(monkeypatch)
        monkeypatch.setattr(settings, "openai_api_key", "sk-super-secret-key")

        relay = Relay()
        record_state(relay)
        relay.state_flusher.flush()

        raw = path.read_bytes().decode("utf-8", errors="ignore")

        for secret in (
            "SECRET_PROMPT",
            "SECRET_RESPONSE",
            "sk-super-secret-key",
        ):
            assert secret not in raw
