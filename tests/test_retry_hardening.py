"""
Regression tests for retry hardening: Retry-After honoring, exponential
backoff, request-timeout budgets, and failover preserved through both the
/chat (chat_across) and /v1 (chat_across_messages) retry loops.

All settings under test default to preserving the previous immediate-retry
behavior; every test enables exactly the knob it exercises.
"""
import time

import pytest

from app.core.config import settings
from app.providers.base import Provider
from app.providers.exceptions import (
    ProviderError,
    ProviderHTTPError,
    ProviderTimeout,
)
from app.services.chat_service import ChatService


def make_provider(name, models, priority=1):
    return Provider(
        name=name,
        base_url=f"https://{name.lower()}.invalid",
        api_key="test-key",
        priority=priority,
        models=list(models),
    )


class FakeClient:
    """
    Deterministic client with both chat() and chat_messages() paths,
    driven by a per-model outcome queue. The last outcome repeats when
    the queue is exhausted.
    """

    def __init__(self):
        self.calls = []
        self._outcomes = {}

    def set_outcomes(self, model, outcomes):
        self._outcomes[model] = list(outcomes)

    def _next(self, model):
        queue = self._outcomes.get(model)

        if not queue:
            raise ProviderError(f"no outcome configured for {model}")

        outcome = queue[0]

        if len(queue) > 1:
            queue.pop(0)

        if isinstance(outcome, Exception):
            raise outcome

        return outcome

    def chat(self, provider, model, message):
        self.calls.append((provider.name, model))
        return self._next(model)

    def chat_messages(self, provider, payload):
        self.calls.append((provider.name, payload["model"]))
        return self._next(payload["model"])


@pytest.fixture(autouse=True)
def fake_registry(monkeypatch):
    """Point the registry at FakeClients instead of real network clients."""
    from app.services import client_registry

    holder = {}

    def fake_get(self, provider_name):
        return holder[provider_name]

    monkeypatch.setattr(
        client_registry.ClientRegistry, "get", fake_get
    )
    return holder


def make_client(holder, name, outcomes_by_model):
    client = FakeClient()

    for model, outcomes in outcomes_by_model.items():
        client.set_outcomes(model, outcomes)

    holder[name] = client
    return client


@pytest.fixture
def slept(monkeypatch):
    """Capture and neutralize time.sleep calls."""
    recorded = []

    def fake_sleep(seconds):
        recorded.append(seconds)

    monkeypatch.setattr("time.sleep", fake_sleep)
    return recorded


class TestRetryAfter:
    def test_429_with_retry_after_waits_before_retry(
        self, fake_registry, slept, monkeypatch
    ):
        monkeypatch.setattr(settings, "retry_honor_retry_after", True)
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {
                "a-1": [
                    ProviderHTTPError(429, "slow down", retry_after=2),
                    "ok-after-wait",
                ]
            },
        )
        service = ChatService()

        result = service.chat_across_messages(
            [(provider, "a-1")],
            {"model": "a-1", "messages": [{"role": "user", "content": "hi"}]},
            max_retries=1,
        )

        assert result["success"] is True
        assert result["model"] == "a-1"
        assert result["response"] == "ok-after-wait"
        assert client.calls == [("A", "a-1"), ("A", "a-1")]
        assert slept == [2.0]

    def test_retry_after_ignored_by_default(
        self, fake_registry, slept
    ):
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {
                "a-1": [
                    ProviderHTTPError(429, "slow down", retry_after=2),
                    "ok",
                ]
            },
        )
        service = ChatService()

        result = service.chat_across_messages(
            [(provider, "a-1")],
            {"model": "a-1", "messages": [{"role": "user", "content": "hi"}]},
            max_retries=1,
        )

        assert result["success"] is True
        assert client.calls == [("A", "a-1"), ("A", "a-1")]
        assert slept == []

    def test_retry_after_does_not_block_failover(
        self, fake_registry, slept, monkeypatch
    ):
        monkeypatch.setattr(settings, "retry_honor_retry_after", True)
        provider_a = make_provider("A", ["a-1"])
        provider_b = make_provider("B", ["a-1"])
        client_a = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(429, "limited", retry_after=5)]},
        )
        make_client(fake_registry, "B", {"a-1": ["fallback-ok"]})
        service = ChatService()

        result = service.chat_across_messages(
            [(provider_a, "a-1"), (provider_b, "a-1")],
            {"model": "a-1", "messages": [{"role": "user", "content": "hi"}]},
            max_retries=1,
        )

        assert result["success"] is True
        assert result["provider"] == "B"
        assert result["model"] == "a-1"
        assert result["response"] == "fallback-ok"
        assert client_a.calls == [("A", "a-1"), ("A", "a-1")]
        assert slept == [5.0]


class TestBackoff:
    def test_backoff_disabled_by_default(
        self, fake_registry, slept, monkeypatch
    ):
        monkeypatch.setattr(settings, "retry_honor_retry_after", False)
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(429, "slow"), "ok"]},
        )
        service = ChatService()

        result = service.chat_across_messages(
            [(provider, "a-1")],
            {"model": "a-1", "messages": [{"role": "user", "content": "hi"}]},
            max_retries=1,
        )

        assert result["success"] is True
        assert client.calls == [("A", "a-1"), ("A", "a-1")]
        assert slept == []

    def test_exponential_backoff_applies_base_and_cap(
        self, fake_registry, slept, monkeypatch
    ):
        monkeypatch.setattr(settings, "retry_backoff_base_seconds", 1)
        monkeypatch.setattr(settings, "retry_backoff_max_seconds", 60)
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {
                "a-1": [
                    ProviderHTTPError(429, "slow"),
                    ProviderHTTPError(429, "slower"),
                    "ok",
                ]
            },
        )
        service = ChatService()

        result = service.chat_across_messages(
            [(provider, "a-1")],
            {"model": "a-1", "messages": [{"role": "user", "content": "hi"}]},
            max_retries=2,
        )

        assert result["success"] is True
        assert client.calls == [("A", "a-1"), ("A", "a-1"), ("A", "a-1")]
        assert slept == [1.0, 2.0]

    def test_backoff_respects_cap(self, fake_registry, slept, monkeypatch):
        monkeypatch.setattr(settings, "retry_backoff_base_seconds", 10)
        monkeypatch.setattr(settings, "retry_backoff_max_seconds", 15)
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {
                "a-1": [
                    ProviderHTTPError(429, "slow"),
                    ProviderHTTPError(429, "slower"),
                    "ok",
                ]
            },
        )
        service = ChatService()

        result = service.chat_across_messages(
            [(provider, "a-1")],
            {"model": "a-1", "messages": [{"role": "user", "content": "hi"}]},
            max_retries=2,
        )

        assert result["success"] is True
        assert slept == [10.0, 15.0]

    def test_chat_across_uses_same_backoff(
        self, fake_registry, slept, monkeypatch
    ):
        monkeypatch.setattr(settings, "retry_backoff_base_seconds", 1)
        monkeypatch.setattr(settings, "retry_backoff_max_seconds", 60)
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderTimeout("slow"), "ok"]},
        )
        service = ChatService()

        result = service.chat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=1,
        )

        assert result["success"] is True
        assert client.calls == [("A", "a-1"), ("A", "a-1")]
        assert slept == [1.0]


class FakeClock:
    """Deterministic perf_counter/sleep pair for budget tests."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def perf_counter(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class TestRequestTimeoutBudget:
    def test_budget_bounds_retries(self, fake_registry, monkeypatch):
        monkeypatch.setattr(settings, "retry_backoff_base_seconds", 1)
        monkeypatch.setattr(settings, "retry_backoff_max_seconds", 60)
        monkeypatch.setattr(settings, "request_timeout_budget_seconds", 2)
        clock = FakeClock()
        monkeypatch.setattr("time.perf_counter", clock.perf_counter)
        monkeypatch.setattr("time.sleep", clock.sleep)

        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(429, "slow")]},
        )
        service = ChatService()

        result = service.chat_across_messages(
            [(provider, "a-1")],
            {"model": "a-1", "messages": [{"role": "user", "content": "hi"}]},
            max_retries=5,
        )

        assert result["success"] is False
        # attempt 0 at t=0; retry sleeps 1s (t=1); attempt 1; retry sleeps
        # 1s (t=2); attempt 2 is skipped because the budget is exhausted.
        assert client.calls == [("A", "a-1"), ("A", "a-1")]
        assert len(result["attempts"]) == 2
        assert clock.slept == [1.0, 1.0]

    def test_no_budget_keeps_full_retries(self, fake_registry, monkeypatch):
        monkeypatch.setattr(settings, "retry_backoff_base_seconds", 1)
        monkeypatch.setattr(settings, "retry_backoff_max_seconds", 60)
        monkeypatch.setattr(settings, "request_timeout_budget_seconds", 0)
        clock = FakeClock()
        monkeypatch.setattr("time.perf_counter", clock.perf_counter)
        monkeypatch.setattr("time.sleep", clock.sleep)

        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(429, "slow")]},
        )
        service = ChatService()

        result = service.chat_across_messages(
            [(provider, "a-1")],
            {"model": "a-1", "messages": [{"role": "user", "content": "hi"}]},
            max_retries=2,
        )

        assert result["success"] is False
        assert client.calls == [("A", "a-1"), ("A", "a-1"), ("A", "a-1")]
        assert clock.slept == [1.0, 2.0]

    def test_budget_caps_retry_after_wait(
        self, fake_registry, monkeypatch
    ):
        monkeypatch.setattr(settings, "retry_honor_retry_after", True)
        monkeypatch.setattr(settings, "retry_after_max_seconds", 60)
        monkeypatch.setattr(settings, "request_timeout_budget_seconds", 3)
        clock = FakeClock()
        monkeypatch.setattr("time.perf_counter", clock.perf_counter)
        monkeypatch.setattr("time.sleep", clock.sleep)

        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(429, "slow", retry_after=10)]},
        )
        service = ChatService()

        result = service.chat_across_messages(
            [(provider, "a-1")],
            {"model": "a-1", "messages": [{"role": "user", "content": "hi"}]},
            max_retries=5,
        )

        assert result["success"] is False
        # First wait: min(10, 60, budget=3) = 3s -> t=3, budget exhausted.
        assert client.calls == [("A", "a-1")]
        assert clock.slept == [3.0]
