"""
Sync/async behavior parity gate.

Drives the sync ``ChatService`` and the async ``AsyncChatService`` with
identical candidate lists and identical fake outcome queues, then proves
both produce identical attempt sequences and result fields (non-stream
and stream-start).
"""
import asyncio

import pytest

from app.providers.base import Provider
from app.providers.exceptions import (
    ProviderError,
    ProviderHTTPError,
    ProviderTimeout,
)
from app.services.async_chat_service import AsyncChatService
from app.services.chat_service import ChatService


def make_provider(name, models, priority=1):
    return Provider(
        name=name,
        base_url=f"https://{name.lower()}.invalid",
        api_key="test-key",
        priority=priority,
        models=list(models),
    )


class DualFake:
    """
    Deterministic client exposing both the sync (``chat`` /
    ``chat_stream``) and async (``achat`` / ``achat_stream``) surfaces.

    Both stacks are fed from the same per-model outcome queues, but each
    keeps its own copy of the queue and its own call log, so the parity
    harness can run sync first, async second, and compare the two
    independent attempt sequences.
    """

    def __init__(self):
        self.sync_calls = []
        self.async_calls = []
        self.sync_stream_calls = []
        self.async_stream_calls = []
        self._sync_outcomes = {}
        self._async_outcomes = {}
        self._sync_stream_outcomes = {}
        self._async_stream_outcomes = {}

    def set_outcomes(self, model, outcomes):
        self._sync_outcomes[model] = list(outcomes)
        self._async_outcomes[model] = list(outcomes)

    def set_stream_outcomes(self, model, outcomes):
        self._sync_stream_outcomes[model] = list(outcomes)
        self._async_stream_outcomes[model] = list(outcomes)

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

    def chat(self, provider, model, message, **kwargs):
        self.sync_calls.append((provider.name, model))
        return self._next(self._sync_outcomes, model)

    def chat_stream(self, provider, model, message, **kwargs):
        self.sync_stream_calls.append((provider.name, model))
        outcome = self._next(self._sync_stream_outcomes, model)

        for chunk in outcome:
            yield chunk

    async def achat(self, provider, model, message, **kwargs):
        self.async_calls.append((provider.name, model))
        return self._next(self._async_outcomes, model)

    async def achat_stream(self, provider, model, message, **kwargs):
        self.async_stream_calls.append((provider.name, model))
        outcome = self._next(self._async_stream_outcomes, model)

        for chunk in outcome:
            yield chunk


@pytest.fixture(autouse=True)
def fake_registry(monkeypatch):
    """Point the registry at DualFake clients instead of real clients."""
    from app.services import client_registry

    holder = {}

    def fake_get(self, provider_name):
        return holder[provider_name]

    monkeypatch.setattr(
        client_registry.ClientRegistry, "get", fake_get
    )
    return holder


@pytest.fixture(autouse=True)
def recorded_sleeps(monkeypatch):
    """Neutralize both sleep paths and record the waits."""
    sync_slept = []
    async_slept = []

    def fake_sync_sleep(seconds):
        sync_slept.append(seconds)

    async def fake_async_sleep(seconds):
        async_slept.append(seconds)

    monkeypatch.setattr("time.sleep", fake_sync_sleep)
    monkeypatch.setattr("asyncio.sleep", fake_async_sleep)
    return sync_slept, async_slept


def make_client(holder, name, outcomes_by_model):
    client = DualFake()

    for model, outcomes in outcomes_by_model.items():
        client.set_outcomes(model, outcomes)

    holder[name] = client
    return client


def make_stream_client(holder, name, outcomes_by_model):
    client = DualFake()

    for model, outcomes in outcomes_by_model.items():
        client.set_stream_outcomes(model, outcomes)

    holder[name] = client
    return client


def strip_latency(attempts):
    return [
        {key: value for key, value in record.items() if key != "latency_ms"}
        for record in attempts
    ]


def compare_results(sync_result, async_result):
    """Assert the two result dicts agree on every field."""
    assert set(async_result.keys()) == set(sync_result.keys())

    for key in sync_result:
        if key == "attempts":
            assert (
                strip_latency(async_result[key])
                == strip_latency(sync_result[key])
            )
        elif key == "stream_gen":
            continue
        else:
            assert async_result[key] == sync_result[key], key


class TestNonStreamParity:
    @pytest.mark.asyncio
    async def test_first_candidate_success(self, fake_registry):
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(
            fake_registry,
            "A",
            {"a-1": ["hello world"], "a-2": ["unused"]},
        )
        sync_result = ChatService().chat_across(
            [(provider, "a-1"), (provider, "a-2")], "msg"
        )
        async_result = await AsyncChatService().achat_across(
            [(provider, "a-1"), (provider, "a-2")], "msg"
        )

        compare_results(sync_result, async_result)

    @pytest.mark.asyncio
    async def test_retry_then_success(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderTimeout("slow"), "ok"]},
        )
        sync_result = ChatService().chat_across(
            [(provider, "a-1")], "msg", max_retries=1
        )
        async_result = await AsyncChatService().achat_across(
            [(provider, "a-1")], "msg", max_retries=1
        )

        compare_results(sync_result, async_result)
        assert client.sync_calls == client.async_calls == [("A", "a-1"), ("A", "a-1")]

    @pytest.mark.asyncio
    async def test_backoff_sleep_parity(
        self, fake_registry, recorded_sleeps, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "retry_backoff_base_seconds", 1)
        monkeypatch.setattr(settings, "retry_backoff_max_seconds", 60)
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(429, "slow"), "ok"]},
        )
        sync_result = ChatService().chat_across(
            [(provider, "a-1")], "msg", max_retries=1
        )
        async_result = await AsyncChatService().achat_across(
            [(provider, "a-1")], "msg", max_retries=1
        )

        compare_results(sync_result, async_result)
        assert client.sync_calls == client.async_calls == [("A", "a-1"), ("A", "a-1")]
        sync_slept, async_slept = recorded_sleeps
        assert async_slept == sync_slept == [1.0]

    @pytest.mark.asyncio
    async def test_retry_after_sleep_parity(
        self, fake_registry, recorded_sleeps, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "retry_honor_retry_after", True)
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {
                "a-1": [
                    ProviderHTTPError(429, "slow", retry_after=2),
                    "ok",
                ]
            },
        )
        sync_result = ChatService().chat_across(
            [(provider, "a-1")], "msg", max_retries=1
        )
        async_result = await AsyncChatService().achat_across(
            [(provider, "a-1")], "msg", max_retries=1
        )

        compare_results(sync_result, async_result)
        assert client.sync_calls == client.async_calls == [("A", "a-1"), ("A", "a-1")]
        sync_slept, async_slept = recorded_sleeps
        assert async_slept == sync_slept == [2.0]

    @pytest.mark.asyncio
    async def test_model_failover(self, fake_registry):
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
        candidates = [
            (provider, "a-1"),
            (provider, "a-2"),
            (provider, "a-3"),
        ]
        sync_result = ChatService().chat_across(candidates, "msg")
        async_result = await AsyncChatService().achat_across(candidates, "msg")

        compare_results(sync_result, async_result)
        # Both stacks should produce identical call sequences
        assert client.sync_calls == client.async_calls

    @pytest.mark.asyncio
    async def test_provider_level_skip(self, fake_registry):
        provider_a = make_provider("A", ["a-1", "a-2"])
        provider_b = make_provider("B", ["b-1"])
        client_a = make_client(
            fake_registry,
            "A",
            {
                "a-1": [ProviderHTTPError(401, "auth rejected")],
                "a-2": [ProviderError("should not run")],
            },
        )
        make_client(fake_registry, "B", {"b-1": ["ok-from-b"]})
        candidates = [
            (provider_a, "a-1"),
            (provider_a, "a-2"),
            (provider_b, "b-1"),
        ]
        sync_result = ChatService().chat_across(candidates, "msg")
        async_result = await AsyncChatService().achat_across(candidates, "msg")

        compare_results(sync_result, async_result)
        assert client_a.sync_calls == client_a.async_calls == [("A", "a-1")]

    @pytest.mark.asyncio
    async def test_non_retryable_failover(self, fake_registry):
        provider_a = make_provider("A", ["a-1"])
        provider_b = make_provider("B", ["b-1"])
        client_a = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(400, "bad request")]},
        )
        make_client(fake_registry, "B", {"b-1": ["ok-from-b"]})
        candidates = [(provider_a, "a-1"), (provider_b, "b-1")]
        sync_result = ChatService().chat_across(
            candidates, "msg", max_retries=2
        )
        async_result = await AsyncChatService().achat_across(
            candidates, "msg", max_retries=2
        )

        compare_results(sync_result, async_result)
        assert client_a.sync_calls == client_a.async_calls == [("A", "a-1")]

    @pytest.mark.asyncio
    async def test_all_fail_aggregate_error(self, fake_registry):
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(
            fake_registry,
            "A",
            {
                "a-1": [ProviderHTTPError(400, "bad request")],
                "a-2": [ProviderTimeout("slow")],
            },
        )
        candidates = [(provider, "a-1"), (provider, "a-2")]
        sync_result = ChatService().chat_across(
            candidates, "msg", max_retries=0
        )
        async_result = await AsyncChatService().achat_across(
            candidates, "msg", max_retries=0
        )

        compare_results(sync_result, async_result)
        assert async_result["success"] is False

    @pytest.mark.asyncio
    async def test_empty_candidates(self, fake_registry):
        sync_result = ChatService().chat_across([], "msg")
        async_result = await AsyncChatService().achat_across([], "msg")

        compare_results(sync_result, async_result)


class TestStreamParity:
    @pytest.mark.asyncio
    async def test_first_candidate_stream(self, fake_registry):
        provider = make_provider("A", ["a-1", "a-2"])
        make_stream_client(
            fake_registry,
            "A",
            {
                "a-1": [["hel", "lo"]],
                "a-2": [["unused"]],
            },
        )
        candidates = [(provider, "a-1"), (provider, "a-2")]
        sync_result = ChatService().chat_across_stream(candidates, "msg")
        async_result = await AsyncChatService().achat_across_stream(
            candidates, "msg"
        )

        compare_results(sync_result, async_result)
        sync_chunks = list(sync_result["stream_gen"])
        async_chunks = [c async for c in async_result["stream_gen"]]
        assert async_chunks == sync_chunks == ["hel", "lo"]

    @pytest.mark.asyncio
    async def test_empty_stream_failover(self, fake_registry):
        provider = make_provider("A", ["a-1", "a-2"])
        make_stream_client(
            fake_registry,
            "A",
            {
                "a-1": [[]],
                "a-2": [["ok-from-a-2"]],
            },
        )
        candidates = [(provider, "a-1"), (provider, "a-2")]
        sync_result = ChatService().chat_across_stream(candidates, "msg")
        async_result = await AsyncChatService().achat_across_stream(
            candidates, "msg"
        )

        compare_results(sync_result, async_result)
        sync_chunks = list(sync_result["stream_gen"])
        async_chunks = [c async for c in async_result["stream_gen"]]
        assert async_chunks == sync_chunks == ["ok-from-a-2"]

    @pytest.mark.asyncio
    async def test_stream_failure_failover(self, fake_registry):
        provider = make_provider("A", ["a-1", "a-2"])
        make_stream_client(
            fake_registry,
            "A",
            {
                "a-1": [ProviderHTTPError(429, "rate limited")],
                "a-2": [["fallback"]],
            },
        )
        candidates = [(provider, "a-1"), (provider, "a-2")]
        sync_result = ChatService().chat_across_stream(candidates, "msg")
        async_result = await AsyncChatService().achat_across_stream(
            candidates, "msg"
        )

        compare_results(sync_result, async_result)
        sync_chunks = list(sync_result["stream_gen"])
        async_chunks = [c async for c in async_result["stream_gen"]]
        assert async_chunks == sync_chunks == ["fallback"]

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
        candidates = [(provider, "a-1"), (provider, "a-2")]
        sync_result = ChatService().chat_across_stream(candidates, "msg")
        async_result = await AsyncChatService().achat_across_stream(
            candidates, "msg"
        )

        compare_results(sync_result, async_result)
        assert async_result["success"] is False
        assert async_result["stream_gen"] is None


class TestAttemptRecordParity:
    @pytest.mark.asyncio
    async def test_attempts_match_across_failover(self, fake_registry):
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(
            fake_registry,
            "A",
            {
                "a-1": [ProviderHTTPError(500, "server error")],
                "a-2": [ProviderHTTPError(429, "rate limited")],
            },
        )
        candidates = [(provider, "a-1"), (provider, "a-2")]
        sync_result = ChatService().chat_across(
            candidates, "msg", max_retries=0
        )
        async_result = await AsyncChatService().achat_across(
            candidates, "msg", max_retries=0
        )

        compare_results(sync_result, async_result)
        assert [
            a["failure_type"] for a in async_result["attempts"]
        ] == ["server_error", "rate_limit"]
