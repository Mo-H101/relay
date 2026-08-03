"""
Persistence failure metrics (Phase 6E).

The declared relay_persistence_flush_failures_total and
relay_persistence_load_failures_total counters must increment whenever
a flush or load fails, so failures stay visible to monitoring even when
Relay degrades to in-memory operation.
"""

from types import SimpleNamespace

import pytest

from app.services.metrics import relay_metrics
from app.services.state_flusher import StateFlusher
from app.services.state_store import StateStore


class TestFlushFailures:
    def test_flush_failure_increments_counter(self):
        before = relay_metrics.persistence_flush_failures.total()

        class BoomStore:
            def save_learned_state(self, state):
                raise RuntimeError("disk full")

            def save_telemetry(self, state):
                raise RuntimeError("disk full")

            def prune_retention(self, retention_days):
                pass

        flusher = StateFlusher(
            health_store=SimpleNamespace(export_learned_state=lambda: {}),
            telemetry=SimpleNamespace(export_state=lambda: []),
            state_store=BoomStore(),
        )

        with pytest.raises(RuntimeError):
            flusher.flush()

        assert relay_metrics.persistence_flush_failures.total() == before + 1

    def test_successful_flush_does_not_increment_counter(self, tmp_path):
        before = relay_metrics.persistence_flush_failures.total()
        store = StateStore(str(tmp_path / "state.db"))

        flusher = StateFlusher(
            health_store=SimpleNamespace(export_learned_state=lambda: {}),
            telemetry=SimpleNamespace(export_state=lambda: []),
            state_store=store,
        )

        flusher.flush()

        assert relay_metrics.persistence_flush_failures.total() == before

    def test_quality_flush_failure_increments_counter(self):
        before = relay_metrics.persistence_flush_failures.total()

        class BoomQuality:
            def export_state(self):
                raise RuntimeError("boom")

        flusher = StateFlusher(
            health_store=SimpleNamespace(export_learned_state=lambda: {}),
            telemetry=SimpleNamespace(export_state=lambda: []),
            state_store=SimpleNamespace(
                save_learned_state=lambda state: None,
                save_telemetry=lambda state: None,
                save_quality=lambda state: None,
                save_decision_stats=lambda state: None,
                prune_retention=lambda days: None,
            ),
            quality_store=BoomQuality(),
        )

        with pytest.raises(RuntimeError):
            flusher.flush()

        assert relay_metrics.persistence_flush_failures.total() == before + 1

    def test_flush_persists_quality_and_decision_stats(self, tmp_path):
        from app.services.decision_engine import DecisionStats
        from app.services.quality import QualityStore

        quality = QualityStore()
        quality.record("P", "m", 5)
        decision = DecisionStats()
        decision.record(None, 2)

        store = StateStore(str(tmp_path / "state.db"))

        flusher = StateFlusher(
            health_store=SimpleNamespace(export_learned_state=lambda: {}),
            telemetry=SimpleNamespace(export_state=lambda: []),
            state_store=store,
            quality_store=quality,
            decision_engine=SimpleNamespace(stats=decision.snapshot),
        )

        flusher.flush()

        loaded_quality = store.load_quality()
        assert len(loaded_quality) == 1
        assert loaded_quality[0]["sample_count"] == 1

        loaded_decision = store.load_decision_stats()
        assert loaded_decision["decisions"] == 1
        assert loaded_decision["candidates"] == 2


class TestLoadFailures:
    @pytest.mark.parametrize("method", ["load_learned_state", "load_telemetry"])
    def test_load_failure_increments_counter(self, tmp_path, method):
        class BadConn:
            def execute(self, sql, *args):
                raise RuntimeError("database is locked")

        store = StateStore(str(tmp_path / "state.db"))
        before = relay_metrics.persistence_load_failures.total()
        store._conn = BadConn()

        with pytest.raises(RuntimeError):
            getattr(store, method)()

        assert relay_metrics.persistence_load_failures.total() == before + 1

    def test_successful_load_does_not_increment_counter(self, tmp_path):
        before = relay_metrics.persistence_load_failures.total()
        store = StateStore(str(tmp_path / "state.db"))

        assert store.load_learned_state() == {}
        assert store.load_telemetry() == []

        assert relay_metrics.persistence_load_failures.total() == before
