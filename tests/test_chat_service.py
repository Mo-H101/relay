import pytest

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
    Deterministic chat client driven by a per-model outcome queue.

    Each outcome is either a string (success response) or an Exception
    instance (raised by chat()). The last outcome repeats if the queue
    is exhausted.
    """

    def __init__(self):
        self.calls = []
        self._outcomes = {}

    def set_outcomes(self, model, outcomes):
        self._outcomes[model] = list(outcomes)

    def chat(self, provider, model, message):
        self.calls.append((provider.name, model))

        queue = self._outcomes.get(model)

        if not queue:
            raise ProviderError(f"no outcome configured for {model}")

        outcome = queue[0]

        if len(queue) > 1:
            queue.pop(0)

        if isinstance(outcome, Exception):
            raise outcome

        return outcome


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


class TestSuccessfulChat:
    def test_first_candidate_succeeds(self, fake_registry):
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(fake_registry, "A", {"a-1": ["hello world"]})
        service = ChatService()

        result = service.chat_across(
            [(provider, "a-1"), (provider, "a-2")],
            "hello",
        )

        assert result["success"] is True
        assert result["provider"] == "A"
        assert result["model"] == "a-1"
        assert result["response"] == "hello world"
        assert isinstance(result["latency_ms"], int)
        assert result["fallback_reason"] is None

    def test_response_content_is_preserved(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["  multi\nline  ok  "]})
        service = ChatService()

        result = service.chat_across([(provider, "a-1")], "msg")

        assert result["response"] == "  multi\nline  ok  "

    def test_attempt_history_records_success(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["ok"]})
        service = ChatService()

        result = service.chat_across([(provider, "a-1")], "msg")

        attempts = result["attempts"]
        assert len(attempts) == 1
        assert attempts[0]["provider"] == "A"
        assert attempts[0]["model"] == "a-1"
        assert attempts[0]["attempt"] == 0
        assert attempts[0]["success"] is True
        assert attempts[0]["failure_type"] is None


class TestModelLevelFailover:
    def test_skips_failed_models_and_returns_success(
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
        service = ChatService()

        result = service.chat_across(
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

    def test_attempt_history_matches_failures_and_success(
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
        service = ChatService()

        result = service.chat_across(
            [(provider, "a-1"), (provider, "a-2")],
            "hello",
            max_retries=0,
        )

        attempts = result["attempts"]
        assert len(attempts) == 2
        assert attempts[0]["success"] is False
        assert attempts[0]["failure_type"] == "unknown"
        assert attempts[0]["reason"] == "a-1 down"
        assert attempts[1]["success"] is True
        assert attempts[1]["model"] == "a-2"

    def test_fallback_reason_is_last_failure(self, fake_registry):
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(
            fake_registry,
            "A",
            {
                "a-1": [ProviderError("first failure")],
                "a-2": ["ok"],
            },
        )
        service = ChatService()

        result = service.chat_across(
            [(provider, "a-1"), (provider, "a-2")],
            "hello",
            max_retries=0,
        )

        assert result["fallback_reason"] == "first failure"

    def test_fallback_reason_none_when_first_candidate_wins(
        self, fake_registry
    ):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["ok"]})
        service = ChatService()

        result = service.chat_across([(provider, "a-1")], "hello")

        assert result["fallback_reason"] is None

    def test_fallback_reason_from_retry_of_same_model(self, fake_registry):
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
        service = ChatService()

        result = service.chat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=1,
        )

        assert result["model"] == "a-1"
        assert result["fallback_reason"] is None


class TestProviderLevelFailover:
    def test_auth_failure_skips_entire_provider(self, fake_registry):
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
        service = ChatService()

        result = service.chat_across(
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
        assert result["fallback_reason"] == "HTTP 401: auth rejected"

    def test_quota_failure_skips_entire_provider(self, fake_registry):
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
        service = ChatService()

        result = service.chat_across(
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
    def test_retryable_failure_retries_then_succeeds(
        self, fake_registry
    ):
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderTimeout("slow"), "ok-after-retry"]},
        )
        service = ChatService()

        result = service.chat_across(
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

    def test_retryable_failure_respects_max_retries(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderTimeout("slow")]},
        )
        service = ChatService()

        result = service.chat_across(
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

    def test_non_retryable_failure_does_not_retry(self, fake_registry):
        provider_a = make_provider("A", ["a-1"])
        provider_b = make_provider("B", ["b-1"])
        client_a = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(400, "bad request")]},
        )
        make_client(fake_registry, "B", {"b-1": ["ok-from-b"]})
        service = ChatService()

        result = service.chat_across(
            [(provider_a, "a-1"), (provider_b, "b-1")],
            "hello",
            max_retries=2,
        )

        assert result["success"] is True
        assert result["provider"] == "B"
        assert client_a.calls == [("A", "a-1")]

    def test_provider_level_failure_does_not_retry(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(403, "forbidden")]},
        )
        service = ChatService()

        result = service.chat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=3,
        )

        assert result["success"] is False
        assert client.calls == [("A", "a-1")]

    def test_unknown_failure_is_retryable(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderError("weird"), "ok-on-second"]},
        )
        service = ChatService()

        result = service.chat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=1,
        )

        assert result["success"] is True
        assert client.calls == [("A", "a-1"), ("A", "a-1")]


class TestAllCandidatesFail:
    def test_returns_success_false_with_aggregate_error(
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
        service = ChatService()

        result = service.chat_across(
            [(provider, "a-1"), (provider, "a-2")],
            "hello",
            max_retries=0,
        )

        assert result["success"] is False
        assert result["provider"] == "A"
        assert result["model"] == "a-1"
        assert "response" not in result
        assert result["error"] == (
            "a-1 (A): HTTP 400: bad request; a-2 (A): slow"
        )

    def test_attempt_history_is_preserved(self, fake_registry):
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(
            fake_registry,
            "A",
            {
                "a-1": [ProviderHTTPError(500, "server error")],
                "a-2": [ProviderHTTPError(429, "rate limited")],
            },
        )
        service = ChatService()

        result = service.chat_across(
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

    def test_failure_result_shape_is_normalized(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(400, "bad request")]},
        )
        service = ChatService()

        result = service.chat_across(
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

    def test_empty_candidates_reports_no_candidates(self, fake_registry):
        service = ChatService()

        result = service.chat_across([], "hello")

        assert result["success"] is False
        assert result["error"] == "No candidates to try."
        assert result["provider"] == ""
        assert result["model"] == ""
        assert result["fallback_reason"] is None
        assert result["attempts"] == []


class TestFailureClassification:
    """Verify classify() results surface through attempt records."""

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
    def test_classification(self, fake_registry, exc, expected):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": [exc]})
        service = ChatService()

        result = service.chat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=0,
        )

        attempt = result["attempts"][0]
        assert attempt["failure_type"] == expected
        assert attempt["success"] is False

    def test_rate_limit_is_retryable(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(429, "slow down"), "ok"]},
        )
        service = ChatService()

        result = service.chat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=1,
        )

        assert result["success"] is True
        assert client.calls == [("A", "a-1"), ("A", "a-1")]

    def test_server_error_is_retryable(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        client = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(503, "down"), "ok"]},
        )
        service = ChatService()

        result = service.chat_across(
            [(provider, "a-1")],
            "hello",
            max_retries=1,
        )

        assert result["success"] is True
        assert client.calls == [("A", "a-1"), ("A", "a-1")]


class TestConvenienceChat:
    def test_chat_returns_model_and_response(self, fake_registry):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["answer"]})
        service = ChatService()

        model, response = service.chat(provider, "hello")

        assert model == "a-1"
        assert response == "answer"

    def test_chat_filters_non_chat_models(self, fake_registry):
        provider = make_provider(
            "A", ["nvidia/nim-embedding", "a-1", "meta-llama-guard-2"]
        )
        make_client(fake_registry, "A", {"a-1": ["answer"]})
        service = ChatService()

        model, response = service.chat(provider, "hello")

        assert model == "a-1"
        assert response == "answer"

    def test_chat_raises_provider_error_when_all_fail(
        self, fake_registry
    ):
        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(400, "bad request")]},
        )
        service = ChatService()

        with pytest.raises(ProviderError) as excinfo:
            service.chat(provider, "hello")

        assert "bad request" in str(excinfo.value)
