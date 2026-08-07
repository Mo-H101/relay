"""
Hardening tests (final production-readiness pass).

Lifecycle and concurrency coverage for the hot request path: concurrent
chat requests never mix responses, provider failure storms through the
adaptive stack record every attempt exactly, hot reload racing with
provider readers stays coherent, and the application lifespan starts and
stops its background components cleanly.
"""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

import app.main as app_main
from app.core.config import settings
from app.core.relay import Relay
from app.providers.base import Provider
from app.providers.exceptions import ProviderTimeout
from app.services.chat_service import ChatService
from app.services.provider_manager import ProviderManager
from app.services.reload import reload_config


def run_threads(target, count=8):
    threads = [threading.Thread(target=target) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def make_provider(name, models, priority=1, api_key="test-key", enabled=True):
    return Provider(
        name=name,
        base_url=f"https://{name.lower()}.invalid",
        api_key=api_key,
        enabled=enabled,
        priority=priority,
        models=list(models),
    )


class EchoClient:
    """Returns the request message verbatim so cross-thread mixing is visible."""

    def chat(self, provider, model, message):
        return message


class FailingClient:
    def chat(self, provider, model, message):
        raise ProviderTimeout("boom")


@pytest.fixture(autouse=True)
def fake_registry(monkeypatch):
    """Point the client registry at the in-memory clients for this file."""
    from app.services import client_registry

    holder = {}

    def fake_get(self, provider_name):
        return holder[provider_name]

    monkeypatch.setattr(client_registry.ClientRegistry, "get", fake_get)
    return holder


class TestConcurrentChat:
    def test_concurrent_chats_do_not_mix_responses(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        fake_registry["A"] = EchoClient()
        service = ChatService()
        errors = []

        def worker(n):
            for _ in range(50):
                result = service.chat_across([(provider, "a-1")], f"msg-{n}")
                if not result["success"] or result["response"] != f"msg-{n}":
                    errors.append((n, result.get("response")))

        threads = [
            threading.Thread(target=worker, args=(n,)) for n in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []


class TestFailureStorm:
    def test_failure_storm_records_every_attempt(self, monkeypatch, fake_registry):
        monkeypatch.setattr(settings, "telemetry_enabled", True)
        monkeypatch.setattr(settings, "health_feedback_enabled", True)
        monkeypatch.setattr(settings, "max_retries", 1)

        # Keep thresholds far above the storm volume so the single model
        # is never down-graded out of the candidate list mid-test.
        monkeypatch.setattr(
            settings, "health_feedback_model_timeout_degraded_threshold", 10000
        )
        monkeypatch.setattr(
            settings, "health_feedback_model_timeout_unavailable_threshold", 10000
        )
        monkeypatch.setattr(
            settings, "health_feedback_model_server_error_threshold", 10000
        )
        monkeypatch.setattr(
            settings, "health_feedback_provider_server_error_threshold", 10000
        )
        monkeypatch.setattr(
            settings, "health_feedback_model_invalid_request_unavailable_threshold", 10000
        )
        monkeypatch.setattr(
            settings, "health_feedback_model_unknown_degraded_threshold", 10000
        )

        relay = Relay()
        relay.provider_manager.register(make_provider("A", ["a-1"]))
        fake_registry["A"] = FailingClient()

        results = []
        lock = threading.Lock()
        errors = []

        def worker():
            try:
                for _ in range(20):
                    result = relay.chat("hello")
                    if result.get("success"):
                        errors.append("unexpected success")
                        return
                    with lock:
                        results.append(result)
            except Exception as exc:
                errors.append(exc)

        run_threads(worker)

        assert errors == []

        total_attempts = sum(len(r["attempts"]) for r in results)
        assert total_attempts == len(results) * 2

        stats = relay.telemetry.get("A", "a-1")
        assert stats is not None
        assert stats.failure_count == total_attempts
        assert stats.request_count == total_attempts


class TestReloadRacingWithRequests:
    def test_provider_mutation_while_iterating_is_coherent(self, monkeypatch):
        monkeypatch.setattr(settings, "nvidia_enabled", False)
        monkeypatch.setattr(settings, "openai_enabled", False)
        monkeypatch.setattr(settings, "lmstudio_enabled", False)

        manager = ProviderManager()
        manager.register(make_provider("NVIDIA", ["a-1", "a-2"]))
        manager.register(make_provider("OpenAI", ["b-1"]))
        manager.register(make_provider("LM Studio", ["c-1"]))

        relay = FakeRelay(manager)
        stop = threading.Event()
        errors = []

        env_on = SimpleNamespace(
            nvidia_enabled=True,
            nvidia_api_key="",
            openai_enabled=True,
            openai_api_key="",
            lmstudio_enabled=False,
            lmstudio_api_key="",
        )
        env_off = SimpleNamespace(
            nvidia_enabled=False,
            nvidia_api_key="",
            openai_enabled=False,
            openai_api_key="",
            lmstudio_enabled=False,
            lmstudio_api_key="",
        )
        envs = [env_on, env_off]

        def reloader(seed):
            for i in range(20):
                try:
                    reload_config(relay, env=envs[(seed + i) % 2])
                except Exception as exc:
                    errors.append(exc)

        def reader():
            while not stop.is_set():
                try:
                    for fn in (manager.all, manager.enabled, manager.ranked):
                        for provider in fn():
                            if not isinstance(provider.enabled, bool):
                                errors.append(RuntimeError("torn enabled"))
                            if not isinstance(provider.api_key, str):
                                errors.append(RuntimeError("torn api_key"))
                            if not isinstance(provider.models, (list, tuple)):
                                errors.append(RuntimeError("torn models"))
                            for model in provider.models:
                                if not isinstance(model, str):
                                    errors.append(RuntimeError("torn model entry"))
                except Exception as exc:
                    errors.append(exc)
                time.sleep(0.001)

        reloaders = [
            threading.Thread(target=reloader, args=(seed,)) for seed in range(3)
        ]
        reader_thread = threading.Thread(target=reader)

        for thread in reloaders:
            thread.start()
        reader_thread.start()

        for thread in reloaders:
            thread.join()
        stop.set()
        reader_thread.join()

        assert errors == []


class TestProviderManagerConcurrency:
    def test_register_while_iterating_is_safe(self):
        manager = ProviderManager()
        for i in range(5):
            manager.register(make_provider(f"P{i}", [f"p{i}-1"]))

        stop = threading.Event()
        errors = []

        def reader():
            while not stop.is_set():
                try:
                    for provider in manager.all():
                        assert isinstance(provider, Provider)
                    for provider in manager.enabled():
                        assert isinstance(provider, Provider)
                    for provider in manager.ranked():
                        assert isinstance(provider, Provider)
                    assert manager.get("P0") is not None
                except Exception as exc:
                    errors.append(exc)
                time.sleep(0.001)

        def registrar(n):
            for i in range(200):
                manager.register(make_provider(f"New{n}-{i}", [f"m{i}"]))

        readers = [
            threading.Thread(target=reader) for _ in range(3)
        ]
        registrars = [
            threading.Thread(target=registrar, args=(n,)) for n in range(3)
        ]

        for thread in readers + registrars:
            thread.start()
        for thread in registrars:
            thread.join()
        stop.set()
        for thread in readers:
            thread.join()

        assert errors == []
        assert len(manager.all()) == 5 + 3 * 200


class TestMetricsProviderStatuses:
    def test_concurrent_health_updates_leave_one_active_status(self):
        from app.services.metrics import RelayMetrics

        metrics = RelayMetrics()
        statuses = ["healthy", "degraded", "unavailable"]

        def worker(n):
            for i in range(100):
                report = SimpleNamespace(
                    name="A",
                    status=statuses[(n + i) % 3],
                    connectivity=True,
                )
                metrics.update_provider_health(report)

        threads = [
            threading.Thread(target=worker, args=(n,)) for n in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        metrics.update_provider_health(
            SimpleNamespace(name="A", status="healthy", connectivity=True)
        )

        active = [
            line
            for line in metrics.render().splitlines()
            if line.startswith(
                'relay_provider_health_info{provider="A"'
            )
            and line.rstrip().endswith((" 1", " 1.0"))
        ]

        assert len(active) == 1
        assert 'status="healthy"' in active[0]


class TestLifespan:
    def test_lifespan_starts_and_stops_health_refresher(self, monkeypatch):
        monkeypatch.setattr(settings, "health_refresh_enabled", True)
        monkeypatch.setattr(settings, "health_refresh_interval_seconds", 60)

        test_relay = Relay()
        monkeypatch.setattr(app_main, "relay", test_relay)

        async def run_lifespan():
            async with app_main.lifespan(app_main.app):
                assert test_relay.health_refresher.is_running is True

        asyncio.run(run_lifespan())

        assert test_relay.health_refresher.is_running is False

    def test_lifespan_starts_and_flushes_state_flusher(
        self, monkeypatch, tmp_path
    ):
        from app.services.state_store import StateStore

        path = tmp_path / "state.db"
        monkeypatch.setattr(settings, "persistence_enabled", True)
        monkeypatch.setattr(settings, "persistence_path", str(path))
        monkeypatch.setattr(settings, "persistence_flush_interval_seconds", 60)
        monkeypatch.setattr(settings, "persistence_retention_days", 0)
        monkeypatch.setattr(settings, "telemetry_enabled", True)

        test_relay = Relay()
        test_relay.telemetry.record_attempt(
            "LM Studio", "qwen-7b", True, latency_ms=10
        )
        monkeypatch.setattr(app_main, "relay", test_relay)

        async def run_lifespan():
            async with app_main.lifespan(app_main.app):
                assert test_relay.state_flusher.is_running is True

        asyncio.run(run_lifespan())

        assert test_relay.state_flusher.is_running is False

        store = StateStore(str(path))
        assert any(
            entry["provider"] == "LM Studio" and entry["model"] == "qwen-7b"
            for entry in store.load_telemetry()
        )

    def test_lifespan_shutdown_closes_request_log(self, monkeypatch):
        from app.services import request_log as request_log_module

        closed = []

        class FakeRequestLog:
            def close(self):
                closed.append(True)

        test_relay = Relay()
        monkeypatch.setattr(app_main, "relay", test_relay)
        monkeypatch.setattr(
            request_log_module, "request_log", lambda: FakeRequestLog()
        )

        async def run_lifespan():
            async with app_main.lifespan(app_main.app):
                pass

        asyncio.run(run_lifespan())

        assert closed == [True]


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
    def __init__(self, provider_manager):
        self.provider_manager = provider_manager
        self.routing = FakeRefreshable()
        self.health_store = FakeRefreshable()
        self.candidate_builder = FakeRefreshable()
        self.decision_engine = FakeRefreshable()
        self.telemetry = FakeRefreshable()
        self.quality_store = FakeRefreshable()
