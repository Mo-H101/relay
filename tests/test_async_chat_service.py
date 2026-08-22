"""
Unit tests for ``AsyncChatService``, mirroring ``test_chat_service.py``
and the retry/budget tests in ``test_retry_hardening.py`` over the async
provider methods (``achat`` / ``achat_stream``).
"""
import asyncio

import pytest

from app.core.config import settings
from app.providers.base import Provider
from app.providers.exceptions import (
    ProviderError,
    ProviderHTTPError,
    ProviderTimeout,
)
from app.services.async_chat_service import AsyncChatService
from app.services.chat_policy import MAX_ATTEMPTS_PER_REQUEST


def make_provider(name, models, priority=1):
    return Provider(
        name=name,
        base_url=f"https://{name.lower()}.invalid",
        api_key="test-key",
        priority=priority,
        models=list(models),
    )


class FakeAsyncClient:
    """
    Deterministic async client driven by per-model outcome queues.

    For ``achat``, each outcome is a string (success response) or an
    Exception (raised). For ``achat_stream``, each outcome is a list of
    content chunks (yielded in order; ``[]`` is an empty stream) or an
    Exception (raised on the first chunk pull). The last outcome repeats
    if the queue is exhausted.
    """

    def __init__(self):
        self.calls = []
        self.stream_calls = []
        self._outcomes = {}
        self._stream_outcomes = {}

    def set_outcomes(self, model, outcomes):
        self._outcomes[model] = list(outcomes)

    def set_stream_outcomes(self, model, outcomes):
        self._stream_outcomes[model] = list(outcomes)

    def _next(self, store, model):
        queue = store.get(model)

        if not queue:
            raise ProviderError(f"no outcome configured for {model}")

        outcome = queue[0]

        if len(queue) > 1:
            queue.pop(0)

        if isinstance(outcome, Exception):
            raise outcome

        return outcome

    async def achat(self, provider, model, message, **kwargs):
        self.calls.append((provider.name, model))
        return self._next(self._outcomes, model)

    async def achat_stream(self, provider, model, message, **kwargs):
        self.stream_calls.append((provider.name, model))
        outcome = self._next(self._stream_outcomes, model)

        for chunk in outcome:
            yield chunk


@pytest.fixture(autouse=True)
def fake_registry(monkeypatch):
    """Point the registry at FakeAsyncClients instead of real clients."""
    from app.services import client_registry

    holder = {}

    def fake_get(self, provider_name):
        return holder[provider_name]

    monkeypatch.setattr(
        client_registry.ClientRegistry, "get", fake_get
    )
    return holder


@pytest.fixture(autouse=True)
def aslept(monkeypatch):
    """Capture and neutralize asyncio.sleep calls."""
    recorded = []

    async def fake_sleep(seconds):
        recorded.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    return recorded


def make_client(holder, name, outcomes_by_model):
    client = FakeAsyncClient()

    for model, outcomes in outcomes_by_model.items():
        client.set_outcomes(model, outcomes)

    holder[name] = client
    return client


def make_stream_client(holder, name, outcomes_by_model):
    client = FakeAsyncClient()

    for model, outcomes in outcomes_by_model.items():
        client.set_stream_outcomes(model, outcomes)

    holder[name] = client
    return client


class TestSuccessfulChat:
    @pytest.mark.asyncio
    async def test_first_candidate_succeeds(self, fake_registry):
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(fake_registry, "A", {"a-1": ["hello world"]})
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1"), (provider, "a-2")],
            "hello",
        )

        assert result["success"] is True
        assert result["provider"] == "A"
        assert result["model"] == "a-1"
        assert result["response"] == "hello world"
        assert isinstance(result["latency_ms"], int)
        assert result["fallback_reason"] is None

    @pytest.mark.asyncio
    async def test_response_content_is_preserved(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["  multi\nline  ok  "]})
        service = AsyncChatService()

        result = await service.achat_across([(provider, "a-1")], "msg")

        assert result["response"] == "  multi\nline  ok  "

    @pytest.mark.asyncio
    async def test_attempt_history_records_success(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["ok"]})
        service = AsyncChatService()

        result = await service.achat_across([(provider, "a-1")], "msg")

        attempts = result["attempts"]
        assert len(attempts) == 1
        assert attempts[0]["provider"] == "A"
        assert attempts[0]["model"] == "a-1"
        assert attempts[0]["attempt"] == 0
        assert attempts[0]["success"] is True
        assert attempts[0]["failure_type"] is None


class TestModelLevelFailover:
    @pytest.mark.asyncio
    async def test_skips_failed_models_and_returns_success(
        self, fake_registry
    ):
        provider = make_provider("A", ["a-1", "a-2", "a-3"])
        client = make_client(
            fake_registry,
            "A",
            {
                "a-1": [ProviderError("a-1 down")],
                "a-2": [ProviderError("a-2 down")],
                "a-3": ["ok-from-a-3"],
            },
        )
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1"), (provider, "a-2"), (provider, "a-3")],
            "hello",
            max_retries=1,
        )

        assert result["success"] is True
        assert result["model"] == "a-3"
        assert result["response"] == "ok-from-a-3"
        assert client.calls == [
            ("A", "a-1"),
            ("A", "a-1"),
            ("A", "a-2"),
            ("A", "a-2"),
            ("A", "a-3"),
        ]

    @pytest.mark.asyncio
    async def test_attempt_history_matches_failures_and_success(
        self, fake_registry
    ):
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(
            fake_registry,
            "A",
            {
                "a-1": [ProviderError("a-1 down")],
                "a-2": ["ok"],
            },
        )
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1"), (provider, "a-2")],
            "hello",
            max_retries=0,
        )

        attempts = result["attempts"]
        assert len(attempts) == 2
        assert attempts[0]["success"] is False
        assert attempts[0]["failure_type"] == "unknown"
        assert attempts[0]["reason"] == "Provider request failed."
        assert attempts[1]["success"] is True
        assert attempts[1]["model"] == "a-2"

    @pytest.mark.asyncio
    async def test_fallback_reason_is_last_failure(self, fake_registry):
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(
            fake_registry,
            "A",
            {
                "a-1": [ProviderError("first failure")],
                "a-2": ["ok"],
            },
        )
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1"), (provider, "a-2")],
            "hello",
            max_retries=0,
        )

        assert result["fallback_reason"] == "Provider request failed."

    @pytest.mark.asyncio
    async def test_fallback_reason_none_when_first_candidate_wins(
        self, fake_registry
    ):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["ok"]})
        service = AsyncChatService()

        result = await service.achat_across([(provider, "a-1")], "hello")

        assert result["fallback_reason"] is None

    @pytest.mark.asyncio
    async def test_fallback_reason_from_retry_of_same_model(
        self, fake_registry
    ):
        """
        A retry success on the same model is not a fallback, so the
        reason stays None.
        """
        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderTimeout("slow"), "ok"]},
        )
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=1,
        )

        assert result["model"] == "a-1"
        assert result["fallback_reason"] is None


class TestEmptyContentFailover:
    @pytest.mark.asyncio
    async def test_empty_content_is_treated_as_failure(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": [None]})
        service = AsyncChatService()

        result = await service.achat_across([(provider, "a-1")], "hello")

        assert result["success"] is False
        assert result["error"] == "a-1 (A): Provider returned empty content."

    @pytest.mark.asyncio
    async def test_blank_content_is_treated_as_failure(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["   "]})
        service = AsyncChatService()

        result = await service.achat_across([(provider, "a-1")], "hello")

        assert result["success"] is False
        assert result["error"] == "a-1 (A): Provider returned empty content."

    @pytest.mark.asyncio
    async def test_fails_over_after_empty_content(self, fake_registry):
        provider = make_provider("A", ["a-1", "a-2"])
        client = make_client(
            fake_registry,
            "A",
            {
                "a-1": [None],
                "a-2": ["ok-from-a-2"],
            },
        )
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1"), (provider, "a-2")],
            "hello",
        )

        assert result["success"] is True
        assert result["model"] == "a-2"
        assert result["response"] == "ok-from-a-2"
        assert result["fallback_reason"] == "Provider returned empty content."
        assert client.calls == [("A", "a-1"), ("A", "a-2")]

    @pytest.mark.asyncio
    async def test_empty_content_attempt_is_recorded(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": [None]})
        service = AsyncChatService()

        result = await service.achat_across([(provider, "a-1")], "hello")

        attempts = result["attempts"]
        assert len(attempts) == 1
        assert attempts[0]["success"] is False
        assert attempts[0]["failure_type"] == "empty_response"
        assert attempts[0]["reason"] == "Provider returned empty content."


class TestProviderLevelFailover:
    @pytest.mark.asyncio
    async def test_auth_failure_skips_entire_provider(self, fake_registry):
        provider_a = make_provider("A", ["a-1", "a-2"])
        provider_b = make_provider("B", ["b-1"])
        client_a = make_client(
            fake_registry,
            "A",
            {
                "a-1": [ProviderHTTPError(401, "auth rejected")],
                "a-2": [ProviderError("a-2 should not run")],
            },
        )
        make_client(fake_registry, "B", {"b-1": ["ok-from-b"]})
        service = AsyncChatService()

        result = await service.achat_across(
            [
                (provider_a, "a-1"),
                (provider_a, "a-2"),
                (provider_b, "b-1"),
            ],
            "hello",
        )

        assert result["success"] is True
        assert result["provider"] == "B"
        assert result["model"] == "b-1"
        assert result["response"] == "ok-from-b"
        assert client_a.calls == [("A", "a-1")]
        assert result["fallback_reason"] == "Provider authentication failed."

    @pytest.mark.asyncio
    async def test_quota_failure_skips_entire_provider(self, fake_registry):
        provider_a = make_provider("A", ["a-1", "a-2"])
        provider_b = make_provider("B", ["b-1"])
        client_a = make_client(
            fake_registry,
            "A",
            {
                "a-1": [ProviderHTTPError(402, "insufficient_quota")],
                "a-2": [ProviderError("a-2 should not run")],
            },
        )
        make_client(fake_registry, "B", {"b-1": ["ok-from-b"]})
        service = AsyncChatService()

        result = await service.achat_across(
            [
                (provider_a, "a-1"),
                (provider_a, "a-2"),
                (provider_b, "b-1"),
            ],
            "hello",
        )

        assert result["success"] is True
        assert result["provider"] == "B"
        assert client_a.calls == [("A", "a-1")]


class TestRetryBehavior:
    @pytest.mark.asyncio
    async def test_retryable_failure_retries_then_succeeds(
        self, fake_registry
    ):
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderTimeout("slow"), "ok-after-retry"]},
        )
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=1,
        )

        assert result["success"] is True
        assert result["model"] == "a-1"
        assert result["response"] == "ok-after-retry"
        assert client.calls == [("A", "a-1"), ("A", "a-1")]
        assert len(result["attempts"]) == 2
        assert result["attempts"][0]["failure_type"] == "timeout"
        assert result["attempts"][1]["success"] is True

    @pytest.mark.asyncio
    async def test_retryable_failure_respects_max_retries(
        self, fake_registry
    ):
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderTimeout("slow")]},
        )
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=2,
        )

        assert result["success"] is False
        assert client.calls == [
            ("A", "a-1"),
            ("A", "a-1"),
            ("A", "a-1"),
        ]

    @pytest.mark.asyncio
    async def test_total_attempt_budget_bounds_large_retry_and_catalog(
        self, fake_registry
    ):
        models = [f"a-{index}" for index in range(MAX_ATTEMPTS_PER_REQUEST + 8)]
        provider = make_provider("A", models)
        client = make_client(
            fake_registry,
            "A",
            {model: [ProviderTimeout("slow")] for model in models},
        )

        result = await AsyncChatService().achat_across(
            [(provider, model) for model in models],
            "hello",
            max_retries=1000,
        )

        assert result["success"] is False
        assert len(client.calls) == MAX_ATTEMPTS_PER_REQUEST
        assert len(result["attempts"]) == MAX_ATTEMPTS_PER_REQUEST

    @pytest.mark.asyncio
    async def test_non_retryable_failure_does_not_retry(
        self, fake_registry
    ):
        provider_a = make_provider("A", ["a-1"])
        provider_b = make_provider("B", ["b-1"])
        client_a = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(400, "bad request")]},
        )
        make_client(fake_registry, "B", {"b-1": ["ok-from-b"]})
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider_a, "a-1"), (provider_b, "b-1")],
            "hello",
            max_retries=2,
        )

        assert result["success"] is True
        assert result["provider"] == "B"
        assert client_a.calls == [("A", "a-1")]

    @pytest.mark.asyncio
    async def test_provider_level_failure_does_not_retry(
        self, fake_registry
    ):
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(403, "forbidden")]},
        )
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=3,
        )

        assert result["success"] is False
        assert client.calls == [("A", "a-1")]

    @pytest.mark.asyncio
    async def test_unknown_failure_is_retryable(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderError("weird"), "ok-on-second"]},
        )
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=1,
        )

        assert result["success"] is True
        assert client.calls == [("A", "a-1"), ("A", "a-1")]


class TestAllCandidatesFail:
    @pytest.mark.asyncio
    async def test_returns_success_false_with_aggregate_error(
        self, fake_registry
    ):
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(
            fake_registry,
            "A",
            {
                "a-1": [ProviderHTTPError(400, "bad request")],
                "a-2": [ProviderTimeout("slow")],
            },
        )
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1"), (provider, "a-2")],
            "hello",
            max_retries=0,
        )

        assert result["success"] is False
        assert result["provider"] == "A"
        assert result["model"] == "a-1"
        assert "response" not in result
        assert result["error"] == (
            "a-1 (A): Provider rejected the request.; "
            "a-2 (A): Provider request timed out."
        )

    @pytest.mark.asyncio
    async def test_attempt_history_is_preserved(self, fake_registry):
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(
            fake_registry,
            "A",
            {
                "a-1": [ProviderHTTPError(500, "server error")],
                "a-2": [ProviderHTTPError(429, "rate limited")],
            },
        )
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1"), (provider, "a-2")],
            "hello",
            max_retries=0,
        )

        attempts = result["attempts"]
        assert len(attempts) == 2
        assert [a["failure_type"] for a in attempts] == [
            "server_error",
            "rate_limit",
        ]
        assert all(not a["success"] for a in attempts)

    @pytest.mark.asyncio
    async def test_failure_result_shape_is_normalized(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(400, "bad request")]},
        )
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=0,
        )

        assert set(result.keys()) == {
            "success",
            "provider",
            "model",
            "error",
            "fallback_reason",
            "attempts",
        }
        assert result["success"] is False
        assert result["fallback_reason"] is None

    @pytest.mark.asyncio
    async def test_empty_candidates_reports_no_candidates(
        self, fake_registry
    ):
        service = AsyncChatService()

        result = await service.achat_across([], "hello")

        assert result["success"] is False
        assert result["error"] == "No candidates to try."
        assert result["provider"] == ""
        assert result["model"] == ""
        assert result["fallback_reason"] is None
        assert result["attempts"] == []


class TestFailureClassification:
    """Verify classify() results surface through attempt records."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc, expected",
        [
            (ProviderTimeout("slow"), "timeout"),
            (ProviderHTTPError(429, "rate limited"), "rate_limit"),
            (ProviderHTTPError(402, "insufficient_quota"), "quota_exhausted"),
            (ProviderHTTPError(500, "boom"), "server_error"),
            (ProviderHTTPError(503, "unavailable"), "server_error"),
            (ProviderHTTPError(401, "auth"), "auth_error"),
            (ProviderHTTPError(403, "forbidden"), "auth_error"),
            (ProviderHTTPError(400, "bad"), "invalid_request"),
            (ProviderHTTPError(404, "missing"), "invalid_request"),
            (ProviderError("plain provider error"), "unknown"),
            (RuntimeError("some unexpected error"), "unknown"),
        ],
    )
    async def test_classification(self, fake_registry, exc, expected):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": [exc]})
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=0,
        )

        attempt = result["attempts"][0]
        assert attempt["failure_type"] == expected
        assert attempt["success"] is False

    @pytest.mark.asyncio
    async def test_rate_limit_is_retryable(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(429, "slow down"), "ok"]},
        )
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=1,
        )

        assert result["success"] is True
        assert client.calls == [("A", "a-1"), ("A", "a-1")]

    @pytest.mark.asyncio
    async def test_server_error_is_retryable(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(503, "down"), "ok"]},
        )
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=1,
        )

        assert result["success"] is True
        assert client.calls == [("A", "a-1"), ("A", "a-1")]


class TestRetryWaitPolicy:
    @pytest.mark.asyncio
    async def test_429_with_retry_after_waits_before_retry(
        self, fake_registry, aslept, monkeypatch
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
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=1,
        )

        assert result["success"] is True
        assert result["model"] == "a-1"
        assert result["response"] == "ok-after-wait"
        assert client.calls == [("A", "a-1"), ("A", "a-1")]
        assert aslept == [2.0]

    @pytest.mark.asyncio
    async def test_retry_after_ignored_by_default(
        self, fake_registry, aslept
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
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=1,
        )

        assert result["success"] is True
        assert client.calls == [("A", "a-1"), ("A", "a-1")]
        assert aslept == []

    @pytest.mark.asyncio
    async def test_retry_after_does_not_block_failover(
        self, fake_registry, aslept, monkeypatch
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
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider_a, "a-1"), (provider_b, "a-1")],
            "hello",
            max_retries=1,
        )

        assert result["success"] is True
        assert result["provider"] == "B"
        assert result["model"] == "a-1"
        assert result["response"] == "fallback-ok"
        assert client_a.calls == [("A", "a-1"), ("A", "a-1")]
        assert aslept == [5.0]

    @pytest.mark.asyncio
    async def test_exponential_backoff_applies_base_and_cap(
        self, fake_registry, aslept, monkeypatch
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
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=2,
        )

        assert result["success"] is True
        assert client.calls == [("A", "a-1"), ("A", "a-1"), ("A", "a-1")]
        assert aslept == [1.0, 2.0]

    @pytest.mark.asyncio
    async def test_backoff_respects_cap(
        self, fake_registry, aslept, monkeypatch
    ):
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
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=2,
        )

        assert result["success"] is True
        assert aslept == [10.0, 15.0]


class FakeAsyncClock:
    """Deterministic elapsed/sleep pair for budget tests."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def elapsed(self, start_wall):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def aclock(monkeypatch):
    clock = FakeAsyncClock()
    monkeypatch.setattr(
        "app.services.async_chat_service._loop_elapsed",
        clock.elapsed,
    )
    monkeypatch.setattr("asyncio.sleep", clock.sleep)
    return clock


class TestRequestTimeoutBudget:
    @pytest.mark.asyncio
    async def test_budget_bounds_retries(
        self, fake_registry, aclock, monkeypatch
    ):
        monkeypatch.setattr(settings, "retry_backoff_base_seconds", 1)
        monkeypatch.setattr(settings, "retry_backoff_max_seconds", 60)
        monkeypatch.setattr(settings, "request_timeout_budget_seconds", 2)
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(429, "slow")]},
        )
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=5,
        )

        assert result["success"] is False
        # attempt 0 at t=0; retry sleeps 1s (t=1); attempt 1; retry sleeps
        # 1s (t=2); attempt 2 is skipped because the budget is exhausted.
        assert client.calls == [("A", "a-1"), ("A", "a-1")]
        assert len(result["attempts"]) == 2
        assert aclock.slept == [1.0, 1.0]

    @pytest.mark.asyncio
    async def test_no_budget_keeps_full_retries(
        self, fake_registry, aclock, monkeypatch
    ):
        monkeypatch.setattr(settings, "retry_backoff_base_seconds", 1)
        monkeypatch.setattr(settings, "retry_backoff_max_seconds", 60)
        monkeypatch.setattr(settings, "request_timeout_budget_seconds", 0)
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(429, "slow")]},
        )
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=2,
        )

        assert result["success"] is False
        assert client.calls == [("A", "a-1"), ("A", "a-1"), ("A", "a-1")]
        assert aclock.slept == [1.0, 2.0]

    @pytest.mark.asyncio
    async def test_budget_caps_retry_after_wait(
        self, fake_registry, aclock, monkeypatch
    ):
        monkeypatch.setattr(settings, "retry_honor_retry_after", True)
        monkeypatch.setattr(settings, "retry_after_max_seconds", 60)
        monkeypatch.setattr(settings, "request_timeout_budget_seconds", 3)
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(429, "slow", retry_after=10)]},
        )
        service = AsyncChatService()

        result = await service.achat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=5,
        )

        assert result["success"] is False
        # First wait: min(10, 60, budget=3) = 3s -> t=3, budget exhausted.
        assert client.calls == [("A", "a-1")]
        assert aclock.slept == [3.0]


class TestStreaming:
    @pytest.mark.asyncio
    async def test_stream_first_candidate_succeeds(self, fake_registry):
        provider = make_provider("A", ["a-1", "a-2"])
        make_stream_client(
            fake_registry,
            "A",
            {
                "a-1": [["hel", "lo"]],
                "a-2": [["unused"]],
            },
        )
        service = AsyncChatService()

        result = await service.achat_across_stream(
            [(provider, "a-1"), (provider, "a-2")],
            "hello",
        )

        assert result["success"] is True
        assert result["provider"] == "A"
        assert result["model"] == "a-1"
        assert result["error"] is None
        assert result["attempts"] == []

        chunks = [chunk async for chunk in result["stream_gen"]]
        assert chunks == ["hel", "lo"]

    @pytest.mark.asyncio
    async def test_stream_empty_fails_over_to_next_candidate(
        self, fake_registry
    ):
        provider = make_provider("A", ["a-1", "a-2"])
        make_stream_client(
            fake_registry,
            "A",
            {
                "a-1": [[]],
                "a-2": [["ok-from-a-2"]],
            },
        )
        service = AsyncChatService()

        result = await service.achat_across_stream(
            [(provider, "a-1"), (provider, "a-2")],
            "hello",
        )

        assert result["success"] is True
        assert result["model"] == "a-2"
        assert result["attempts"][0]["failure_type"] == "empty_stream"
        assert result["attempts"][0]["reason"] == (
            "stream ended before producing content"
        )

        chunks = [chunk async for chunk in result["stream_gen"]]
        assert chunks == ["ok-from-a-2"]

    @pytest.mark.asyncio
    async def test_stream_failure_fails_over_to_next_candidate(
        self, fake_registry
    ):
        provider = make_provider("A", ["a-1", "a-2"])
        make_stream_client(
            fake_registry,
            "A",
            {
                "a-1": [ProviderHTTPError(429, "rate limited")],
                "a-2": [["fallback"]],
            },
        )
        service = AsyncChatService()

        result = await service.achat_across_stream(
            [(provider, "a-1"), (provider, "a-2")],
            "hello",
        )

        assert result["success"] is True
        assert result["model"] == "a-2"
        assert result["attempts"][0]["failure_type"] == "rate_limit"
        assert result["attempts"][0]["reason"] == (
            "Provider rate limit reached."
        )

        chunks = [chunk async for chunk in result["stream_gen"]]
        assert chunks == ["fallback"]

    @pytest.mark.asyncio
    async def test_stream_provider_level_failure_skips_provider(
        self, fake_registry
    ):
        provider_a = make_provider("A", ["a-1", "a-2"])
        provider_b = make_provider("B", ["b-1"])
        client_a = make_stream_client(
            fake_registry,
            "A",
            {
                "a-1": [ProviderHTTPError(401, "auth rejected")],
                "a-2": [["should-not-run"]],
            },
        )
        make_stream_client(fake_registry, "B", {"b-1": [["ok-from-b"]]})
        service = AsyncChatService()

        result = await service.achat_across_stream(
            [
                (provider_a, "a-1"),
                (provider_a, "a-2"),
                (provider_b, "b-1"),
            ],
            "hello",
        )

        assert result["success"] is True
        assert result["provider"] == "B"
        assert client_a.stream_calls == [("A", "a-1")]

    @pytest.mark.asyncio
    async def test_stream_all_fail(self, fake_registry):
        provider = make_provider("A", ["a-1", "a-2"])
        make_stream_client(
            fake_registry,
            "A",
            {
                "a-1": [ProviderHTTPError(400, "bad request")],
                "a-2": [[]],
            },
        )
        service = AsyncChatService()

        result = await service.achat_across_stream(
            [(provider, "a-1"), (provider, "a-2")],
            "hello",
        )

        assert result["success"] is False
        assert result["provider"] == "A"
        assert result["model"] == "a-1"
        assert result["stream_gen"] is None
        assert result["error"] == (
            "a-1 (A): Provider rejected the request.; "
            "a-2 (A): empty stream"
        )


class TestConvenienceChat:
    @pytest.mark.asyncio
    async def test_achat_returns_model_and_response(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["answer"]})
        service = AsyncChatService()

        model, response = await service.achat(provider, "hello")

        assert model == "a-1"
        assert response == "answer"

    @pytest.mark.asyncio
    async def test_achat_filters_non_chat_models(self, fake_registry):
        provider = make_provider(
            "A", ["nvidia/nim-embedding", "a-1", "meta-llama-guard-2"]
        )
        make_client(fake_registry, "A", {"a-1": ["answer"]})
        service = AsyncChatService()

        model, response = await service.achat(provider, "hello")

        assert model == "a-1"
        assert response == "answer"

    @pytest.mark.asyncio
    async def test_achat_raises_provider_error_when_all_fail(
        self, fake_registry
    ):
        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(400, "bad request")]},
        )
        service = AsyncChatService()

        with pytest.raises(ProviderError) as excinfo:
            await service.achat(provider, "hello")

        assert str(excinfo.value) == (
            "a-1 (A): Provider rejected the request."
        )
