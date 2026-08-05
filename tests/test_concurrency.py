"""
Concurrency tests (Phase 6E).

Thread-safety coverage for the metrics registry, the operations store,
and hot configuration reload running while other threads are active.
"""

import threading
from types import SimpleNamespace

from app.core.config import settings
from app.services.metrics import MetricsRegistry
from app.services.ops_store import RequestStatsStore
from app.services.reload import reload_config


def run_threads(target, count=8):
    threads = [threading.Thread(target=target) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


class TestMetricsRegistry:
    def test_counter_concurrent_increments_are_exact(self):
        registry = MetricsRegistry()
        counter = registry.counter("concurrent_total", "doc", ("key",))
        per_thread = 500

        def worker():
            for _ in range(per_thread):
                counter.inc(key="value")

        run_threads(worker)

        assert counter.value(key="value") == per_thread * 8
        assert counter.total() == per_thread * 8

    def test_histogram_concurrent_observations_are_exact(self):
        registry = MetricsRegistry()
        histogram = registry.histogram(
            "concurrent_duration", "doc", buckets=(1.0, 5.0)
        )
        per_thread = 300

        def worker():
            for _ in range(per_thread):
                histogram.observe(0.5)

        run_threads(worker)

        lines = registry.render()
        assert "# TYPE concurrent_duration histogram" in lines
        assert 'concurrent_duration_bucket{le="5.0"} 2400' in lines
        assert 'concurrent_duration_count 2400' in lines

    def test_render_is_safe_during_concurrent_updates(self):
        registry = MetricsRegistry()
        counter = registry.counter("render_total", "doc", ("key",))
        stop = threading.Event()
        errors = []

        def worker():
            while not stop.is_set():
                counter.inc(key="value")

        def renderer():
            while not stop.is_set():
                try:
                    registry.render()
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        threads.append(threading.Thread(target=renderer))
        for thread in threads:
            thread.start()
        stop.set()
        for thread in threads:
            thread.join()

        assert errors == []
        assert counter.total() > 0


class TestOpsStoreConcurrency:
    def test_concurrent_records_are_not_lost(self):
        store = RequestStatsStore(
            window_seconds=3600, max_events=100000
        )
        per_thread = 200

        def worker():
            for _ in range(per_thread):
                store.record_chat(
                    endpoint="/chat",
                    stream=False,
                    provider="P",
                    model="m",
                    success=True,
                    fallback=False,
                    latency_ms=1.0,
                )
                store.record_http("GET", "/health", 200, 1.0)

        run_threads(worker)

        assert len(store.events()) == per_thread * 8 * 2
        stats = store.stats()
        assert stats["requests"] == per_thread * 8 * 2
        assert stats["chats"] == per_thread * 8


class FakeProviderManager:
    def __init__(self):
        self.providers = {}

    def get(self, name):
        return self.providers.get(name)


class FakeRefreshable:
    def refresh(self):
        pass

    def refresh_thresholds(self):
        pass

    def refresh_scorer(self):
        pass

    def set_ewma_alpha(self, alpha):
        pass

    def set_alpha(self, alpha):
        pass

    def set_min_samples(self, min_samples):
        pass

    def set_retention_limit(self, limit):
        pass


class FakeRelay:
    def __init__(self):
        self.provider_manager = FakeProviderManager()
        self.routing = FakeRefreshable()
        self.health_store = FakeRefreshable()
        self.candidate_builder = FakeRefreshable()
        self.telemetry = FakeRefreshable()
        self.quality_store = FakeRefreshable()
        self.decision_engine = FakeRefreshable()


class TestEventPruneVsKeyUse:
    def test_retention_prune_and_mark_used_no_lock_escalation(self, tmp_path):
        """
        P6.2: the flusher's events-table retention prune runs on the same
        WAL file as auth's ``mark_used`` writes. Concurrent connections
        must complete without SQLITE_BUSY / lock escalation.
        """
        from app.services.event_log import EventLog
        from app.services.key_store import KeyStore

        db = str(tmp_path / "platform.db")
        log = EventLog(db)
        store = KeyStore(db)
        key_id, raw = store.create("ci")
        log.emit("auth.success", actor=key_id, target=key_id)

        errors = []

        def pruner():
            try:
                for _ in range(20):
                    log.prune_retention(30)
            except Exception as exc:
                errors.append(exc)

        def user():
            try:
                for _ in range(20):
                    store.mark_used(key_id)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=pruner) for _ in range(2)]
        threads += [threading.Thread(target=user) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        log.close()
        store.close()

        assert errors == []
        assert store.get_by_id(key_id)["last_used_at"] is not None
        assert log.count() == 1


class TestReloadWhileActive:
    def test_reload_does_not_tear_when_requests_are_running(self):
        original = settings.request_timeout
        values = [original + i for i in range(1, 6)]
        relay = FakeRelay()
        stop = threading.Event()
        errors = []

        def reloader():
            for value in values:
                try:
                    reload_config(relay, env=SimpleNamespace(request_timeout=value))
                except Exception as exc:
                    errors.append(exc)

        def reader():
            while not stop.is_set():
                value = settings.request_timeout
                if not isinstance(value, int) or value <= 0:
                    errors.append(RuntimeError(f"torn value: {value!r}"))

        reloaders = [threading.Thread(target=reloader) for _ in range(3)]
        reader_thread = threading.Thread(target=reader)

        for thread in reloaders:
            thread.start()
        reader_thread.start()

        for thread in reloaders:
            thread.join()
        stop.set()
        reader_thread.join()

        assert errors == []
        assert settings.request_timeout in values

    def test_concurrent_reloads_are_serialized(self):
        relay = FakeRelay()
        counter = {"n": 0}
        lock = threading.Lock()
        original = settings.request_timeout

        def reloader():
            reload_config(
                relay, env=SimpleNamespace(request_timeout=original + 1)
            )
            with lock:
                counter["n"] += 1

        run_threads(reloader, count=16)

        assert counter["n"] == 16
        assert settings.request_timeout == original + 1
