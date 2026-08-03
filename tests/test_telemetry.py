import threading

import pytest

from app.core.config import settings
from app.core.relay import Relay
from app.providers.base import Provider
from app.providers.exceptions import ProviderHTTPError
from app.services.telemetry import FailureEvent, TelemetryStore


class TestTelemetryStore:
    def test_no_stats_before_recording(self):
        store = TelemetryStore()

        assert store.get("A", "a-1") is None

    def test_records_request_success_and_failure_counts(self):
        store = TelemetryStore()
        store.record_attempt("A", "a-1", success=True, latency_ms=100)
        store.record_attempt("A", "a-1", success=True, latency_ms=200)
        store.record_attempt("A", "a-1", success=False, latency_ms=50)

        stats = store.get("A", "a-1")

        assert stats.request_count == 3
        assert stats.success_count == 2
        assert stats.failure_count == 1

    def test_averages_latency(self):
        store = TelemetryStore()
        store.record_attempt("A", "a-1", success=True, latency_ms=100)
        store.record_attempt("A", "a-1", success=False, latency_ms=300)

        stats = store.get("A", "a-1")

        assert stats.average_latency_ms == 200.0

    def test_average_latency_rounds(self):
        store = TelemetryStore()
        store.record_attempt("A", "a-1", success=True, latency_ms=101)
        store.record_attempt("A", "a-1", success=True, latency_ms=100)

        stats = store.get("A", "a-1")

        assert stats.average_latency_ms == 100.5

    def test_failures_recorded_with_type(self):
        store = TelemetryStore()
        store.record_attempt(
            "A", "a-1", success=False, failure_type="timeout"
        )

        stats = store.get("A", "a-1")

        assert len(stats.recent_failures) == 1
        event = stats.recent_failures[0]
        assert isinstance(event, FailureEvent)
        assert event.failure_type == "timeout"

    def test_failure_history_is_bounded(self):
        store = TelemetryStore(max_failure_history=3)
        for i in range(5):
            store.record_attempt(
                "A", "a-1", success=False, failure_type=f"kind-{i}"
            )

        stats = store.get("A", "a-1")

        assert len(stats.recent_failures) == 3
        assert [e.failure_type for e in stats.recent_failures] == [
            "kind-4",
            "kind-3",
            "kind-2",
        ]

    def test_recent_failures_newest_first(self):
        store = TelemetryStore()
        store.record_attempt("A", "a-1", success=False, failure_type="a")
        store.record_attempt("A", "a-1", success=False, failure_type="b")

        events = store.recent_failures("A", "a-1")

        assert [e.failure_type for e in events] == ["b", "a"]

    def test_recent_failures_window_filters_old(self):
        store = TelemetryStore()
        store.record_attempt("A", "a-1", success=False, failure_type="a")

        included = store.recent_failures(
            "A", "a-1", window_seconds=10 ** 9
        )
        excluded = store.recent_failures("A", "a-1", window_seconds=-1)

        assert [e.failure_type for e in included] == ["a"]
        assert excluded == []

    def test_separate_pairs_track_independently(self):
        store = TelemetryStore()
        store.record_attempt("A", "a-1", success=True, latency_ms=10)
        store.record_attempt("A", "b-1", success=False, latency_ms=20)

        a = store.get("A", "a-1")
        b = store.get("A", "b-1")

        assert a.request_count == 1
        assert a.failure_count == 0
        assert b.request_count == 1
        assert b.failure_count == 1

    def test_all_returns_snapshots(self):
        store = TelemetryStore()
        store.record_attempt("A", "a-1", success=True)
        store.record_attempt("B", "b-1", success=False)

        stats = store.all()

        assert len(stats) == 2

    def test_clear_removes_everything(self):
        store = TelemetryStore()
        store.record_attempt("A", "a-1", success=True)

        store.clear()

        assert store.get("A", "a-1") is None
        assert store.all() == []

    def test_concurrent_recording_is_safe(self):
        store = TelemetryStore()
        errors = []

        def worker():
            try:
                for _ in range(200):
                    store.record_attempt(
                        "P1", "m1", success=True, latency_ms=10
                    )
                    store.record_attempt(
                        "P1", "m1", success=False, failure_type="timeout"
                    )
                    store.get("P1", "m1")
                    store.all()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()

        assert errors == []

        stats = store.get("P1", "m1")

        assert stats.request_count == 8 * 400
        assert stats.success_count == 8 * 200
        assert stats.failure_count == 8 * 200


def make_provider(name, models, priority=1):
    return Provider(
        name=name,
        base_url=f"https://{name.lower()}.invalid",
        api_key="test-key",
        enabled=True,
        priority=priority,
        models=list(models),
    )


class FakeClient:
    def __init__(self):
        self._outcomes = {}

    def set_outcomes(self, model, outcomes):
        self._outcomes[model] = list(outcomes)

    def chat(self, provider, model, message, timeout=None, max_tokens=None):
        queue = self._outcomes.get(model)

        if not queue:
            raise ProviderHTTPError(500, f"no outcome for {model}")

        outcome = queue[0]

        if len(queue) > 1:
            queue.pop(0)

        if isinstance(outcome, Exception):
            raise outcome

        return outcome


@pytest.fixture(autouse=True)
def fake_registry(monkeypatch):
    from app.services import client_registry

    holder = {}

    def fake_get(self, provider_name):
        return holder[provider_name]

    monkeypatch.setattr(
        client_registry.ClientRegistry, "get", fake_get
    )
    return holder


class TestRelayTelemetry:
    def test_records_attempts_when_enabled(
        self, monkeypatch, fake_registry
    ):
        monkeypatch.setattr(settings, "telemetry_enabled", True)

        relay = Relay()
        p_a = make_provider("A", ["a-1"], priority=10)
        p_b = make_provider("B", ["b-1"], priority=1)
        relay.provider_manager.register(p_a)
        relay.provider_manager.register(p_b)

        client_a = FakeClient()
        client_a.set_outcomes(
            "a-1", [ProviderHTTPError(429, "rate limited")]
        )
        fake_registry["A"] = client_a

        client_b = FakeClient()
        client_b.set_outcomes("b-1", ["hello from b"])
        fake_registry["B"] = client_b

        result = relay.chat("hi")

        assert result["success"] is True

        a = relay.telemetry.get("A", "a-1")
        b = relay.telemetry.get("B", "b-1")

        assert a is not None
        assert a.request_count == 2
        assert a.success_count == 0
        assert a.failure_count == 2
        assert a.recent_failures[0].failure_type == "rate_limit"

        assert b is not None
        assert b.success_count == 1
        assert b.failure_count == 0

    def test_records_failed_chat_attempts(
        self, monkeypatch, fake_registry
    ):
        monkeypatch.setattr(settings, "telemetry_enabled", True)

        relay = Relay()
        p_a = make_provider("A", ["a-1"], priority=1)
        relay.provider_manager.register(p_a)

        client_a = FakeClient()
        client_a.set_outcomes(
            "a-1", [ProviderHTTPError(401, "invalid api key")]
        )
        fake_registry["A"] = client_a

        result = relay.chat("hi")

        assert result["success"] is False

        a = relay.telemetry.get("A", "a-1")

        assert a is not None
        assert a.failure_count == 1
        assert a.recent_failures[0].failure_type == "auth_error"

    def test_no_recording_when_disabled(
        self, monkeypatch, fake_registry
    ):
        monkeypatch.setattr(settings, "telemetry_enabled", False)

        relay = Relay()
        p_a = make_provider("A", ["a-1"], priority=1)
        relay.provider_manager.register(p_a)

        client_a = FakeClient()
        client_a.set_outcomes("a-1", ["ok"])
        fake_registry["A"] = client_a

        result = relay.chat("hi")

        assert result["success"] is True
        assert relay.telemetry.get("A", "a-1") is None
        assert relay.telemetry.all() == []
