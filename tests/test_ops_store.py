"""
Tests for the in-memory operations store backing the diagnostics
"operations" block and rolling request statistics.
"""

from app.services.ops_store import OpsEvent, RequestStatsStore


def make_store(window=3600, max_events=10000):
    return RequestStatsStore(window_seconds=window, max_events=max_events)


def chat(store, provider="ProviderA", model="m1", success=True, fallback=False,
         attempts=0, stream=False, latency_ms=10.0):
    store.record_chat(
        endpoint="/chat",
        stream=stream,
        provider=provider,
        model=model,
        success=success,
        fallback=fallback,
        latency_ms=latency_ms,
        attempts=attempts,
    )


class TestHttpRecording:
    def test_empty_stats(self):
        stats = make_store().stats()
        assert stats["requests"] == 0
        assert stats["successes"] == 0
        assert stats["failures"] == 0
        assert stats["success_rate"] is None
        assert stats["average_latency_ms"] is None
        assert stats["p50_latency_ms"] is None
        assert stats["p95_latency_ms"] is None
        assert stats["chats"] == 0
        assert stats["streaming"]["requests"] == 0
        assert stats["providers"] == []
        assert stats["endpoints"] == []

    def test_http_success(self):
        store = make_store()
        store.record_http(
            method="GET", route="/health", status=200, latency_ms=10.0
        )
        stats = store.stats()
        assert stats["requests"] == 1
        assert stats["successes"] == 1
        assert stats["failures"] == 0
        assert stats["success_rate"] == 1.0
        assert stats["average_latency_ms"] == 10.0
        assert stats["endpoints"] == [
            {"route": "/health", "requests": 1, "average_latency_ms": 10.0}
        ]

    def test_http_failure_bands(self):
        store = make_store()
        for status in (404, 500, 401, 503, 200):
            store.record_http("GET", "/x", status, 5.0)
        stats = store.stats()
        assert stats["requests"] == 5
        assert stats["successes"] == 1
        assert stats["failures"] == 4
        assert stats["success_rate"] == 0.2
        assert stats["endpoints"][0]["requests"] == 5

    def test_200_series_and_redirects_count_success(self):
        store = make_store()
        store.record_http("GET", "/a", 204, 1.0)
        store.record_http("GET", "/a", 302, 1.0)
        store.record_http("GET", "/a", 500, 1.0)
        assert store.stats()["successes"] == 2
        assert store.stats()["failures"] == 1

    def test_percentiles(self):
        store = make_store()
        for i in range(1, 101):
            store.record_http("GET", "/p", 200, float(i))
        stats = store.stats()
        assert stats["p50_latency_ms"] == 50.0
        assert stats["p95_latency_ms"] == 95.0
        assert stats["average_latency_ms"] == 50.5

    def test_percentile_single_event(self):
        store = make_store()
        store.record_http("GET", "/p", 200, 7.0)
        stats = store.stats()
        assert stats["p50_latency_ms"] == 7.0
        assert stats["p95_latency_ms"] == 7.0

    def test_window_prunes_old_events(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.ops_store.time.monotonic", lambda: 2000.0
        )
        store = make_store(window=10)
        store._append(
            OpsEvent(
                ts=1000.0, kind="http", method="GET", route="/old",
                status=200, latency_ms=1.0,
            )
        )
        store._append(
            OpsEvent(
                ts=1995.0, kind="http", method="GET", route="/new",
                status=200, latency_ms=1.0,
            )
        )
        stats = store.stats()
        assert stats["requests"] == 1
        assert stats["endpoints"][0]["route"] == "/new"

    def test_max_events_bounded(self):
        store = make_store(max_events=5)
        for i in range(10):
            store.record_http("GET", f"/{i}", 200, 1.0)
        stats = store.stats()
        assert stats["requests"] == 5
        assert len(stats["endpoints"]) == 5

    def test_clear(self):
        store = make_store()
        store.record_http("GET", "/a", 200, 1.0)
        store.clear()
        assert store.stats()["requests"] == 0


class TestChatRecording:
    def test_chat_attempts_recorded(self):
        store = make_store()
        chat(store, attempts=2, fallback=True, success=True)
        chat(store, attempts=0, fallback=False, success=True)
        stats = store.stats()
        assert stats["chats"] == 2
        assert stats["chat_attempts"] == 2
        assert stats["chat_fallbacks"] == 1

    def test_streaming_requests_recorded(self):
        store = make_store()
        chat(store, stream=True)
        stats = store.stats()
        assert stats["streaming"]["requests"] == 1
        assert stats["streaming"]["successes"] == 1

    def test_provider_rollup(self):
        store = make_store()
        chat(store, provider="ProviderA", success=True)
        chat(store, provider="ProviderA", success=True)
        chat(store, provider="ProviderB", success=False)
        stats = store.stats()
        providers = {p["provider"]: p for p in stats["providers"]}
        assert providers["ProviderA"]["requests"] == 2
        assert providers["ProviderA"]["success_rate"] == 1.0
        assert providers["ProviderB"]["requests"] == 1
        assert providers["ProviderB"]["success_rate"] == 0.0

    def test_chat_successes_count_in_overall(self):
        store = make_store()
        chat(store, success=True)
        chat(store, success=False)
        stats = store.stats()
        assert stats["requests"] == 2
        assert stats["successes"] == 1
        assert stats["failures"] == 1

    def test_chats_excluded_from_endpoints_rollup(self):
        store = make_store()
        chat(store)
        store.record_http("GET", "/health", 200, 1.0)
        stats = store.stats()
        assert stats["endpoints"] == [
            {"route": "/health", "requests": 1, "average_latency_ms": 1.0}
        ]
