"""
Tests for the OpenAI-compatible API endpoints (Phase 6A).
"""
import json
import pytest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.relay import Relay
from app.main import app as fastapi_app
from app.providers.base import ModelProbe, Provider
from app.providers.exceptions import (
    ProviderError,
    ProviderHTTPError,
    ProviderTimeout,
)
from app.services.health_checker import DEGRADED, HEALTHY, ProviderHealth

import app.api.chat
import app.api.diagnostics
import app.api.decision
import app.api.health
import app.api.openai
import app.api.providers


def make_provider(name, models, priority=1, api_key="test-key", enabled=True):
    return Provider(
        name=name,
        base_url=f"https://{name.lower()}.invalid",
        api_key=api_key,
        enabled=enabled,
        priority=priority,
        models=list(models),
    )


class FakeClient:
    """
    Deterministic client used for both chat and probe flows.

    Chat outcomes are a per-model queue of strings (success) or Exception
    instances (raised). Probe results map model -> ModelProbe.
    """

    def __init__(self):
        self.chat_calls = []
        self.probe_calls = []
        self._outcomes = {}
        self._probes = {}

    def set_outcomes(self, model, outcomes):
        self._outcomes[model] = list(outcomes)

    def set_probe(self, model, probe):
        self._probes[model] = probe

    def chat(self, provider, model, message, **kwargs):
        self.chat_calls.append((provider.name, model, message, kwargs))

        queue = self._outcomes.get(model)

        if not queue:
            raise ProviderError(f"no outcome configured for {model}")

        outcome = queue[0]

        if len(queue) > 1:
            queue.pop(0)

        if isinstance(outcome, Exception):
            raise outcome

        return outcome

    def chat_stream(self, provider, model, message, **kwargs):
        self.chat_calls.append((provider.name, model, message, kwargs))

        queue = self._outcomes.get(model)

        if not queue:
            raise ProviderError(f"no outcome configured for {model}")

        # For streaming, we iterate through the queue and yield/raise each item.
        # Empty/falsy outcomes are skipped (simulating chunks with no content),
        # and a clean exhaustion of the queue ends the stream normally.
        while queue:
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome:
                yield outcome

    async def achat(self, provider, model, message, **kwargs):
        """Async version of chat()."""
        self.chat_calls.append((provider.name, model, message, kwargs))

        queue = self._outcomes.get(model)

        if not queue:
            raise ProviderError(f"no outcome configured for {model}")

        outcome = queue[0]

        if len(queue) > 1:
            queue.pop(0)

        if isinstance(outcome, Exception):
            raise outcome

        return outcome

    async def achat_stream(self, provider, model, message, **kwargs):
        """Async version of chat_stream()."""
        self.chat_calls.append((provider.name, model, message, kwargs))

        queue = self._outcomes.get(model)

        if not queue:
            raise ProviderError(f"no outcome configured for {model}")

        # For streaming, we iterate through the queue and yield/raise each item.
        # Empty/falsy outcomes are skipped (simulating chunks with no content),
        # and a clean exhaustion of the queue ends the stream normally.
        while queue:
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome:
                yield outcome

    def _default_response(self, model, content):
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 1700000000,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    def chat_messages(self, provider, payload):
        self.chat_calls.append((provider.name, payload))

        queue = self._outcomes.get(payload["model"])

        if not queue:
            raise ProviderError(f"no outcome configured for {payload['model']}")

        outcome = queue[0]

        if len(queue) > 1:
            queue.pop(0)

        if isinstance(outcome, Exception):
            raise outcome

        if isinstance(outcome, dict):
            return outcome

        return self._default_response(payload["model"], outcome)

    def chat_stream_messages(self, provider, payload):
        self.chat_calls.append((provider.name, payload))

        queue = self._outcomes.get(payload["model"])

        if not queue:
            raise ProviderError(f"no outcome configured for {payload['model']}")

        produced = False

        while queue:
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            if isinstance(outcome, dict):
                yield outcome
                produced = True
            elif outcome:
                yield {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": payload["model"],
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": outcome},
                            "finish_reason": None,
                        }
                    ],
                }
                produced = True

        if produced:
            yield {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": payload["model"],
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ],
            }

    def probe_model(self, provider, model):
        self.probe_calls.append((provider.name, model))

        probe = self._probes.get(model)

        if probe is None:
            return ModelProbe(False, 0, 404, "missing probe")

        return probe

    async def achat_messages(self, provider, payload):
        self.chat_calls.append((provider.name, payload))

        queue = self._outcomes.get(payload["model"])

        if not queue:
            raise ProviderError(f"no outcome configured for {payload['model']}")

        outcome = queue[0]

        if len(queue) > 1:
            queue.pop(0)

        if isinstance(outcome, Exception):
            raise outcome

        if isinstance(outcome, dict):
            return outcome

        return self._default_response(payload["model"], outcome)

    async def achat_stream_messages(self, provider, payload):
        self.chat_calls.append((provider.name, payload))

        queue = self._outcomes.get(payload["model"])

        if not queue:
            raise ProviderError(f"no outcome configured for {payload['model']}")

        produced = False

        while queue:
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            if isinstance(outcome, dict):
                yield outcome
                produced = True
            elif outcome:
                yield {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": payload["model"],
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": outcome},
                            "finish_reason": None,
                        }
                    ],
                }
                produced = True

        if produced:
            yield {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": payload["model"],
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ],
            }

    async def achat_messages(self, provider, payload):
        self.chat_calls.append((provider.name, payload))

        queue = self._outcomes.get(payload["model"])

        if not queue:
            raise ProviderError(f"no outcome configured for {payload['model']}")

        outcome = queue[0]

        if len(queue) > 1:
            queue.pop(0)

        if isinstance(outcome, Exception):
            raise outcome

        if isinstance(outcome, dict):
            return outcome

        return self._default_response(payload["model"], outcome)

    async def achat_stream_messages(self, provider, payload):
        self.chat_calls.append((provider.name, payload))

        queue = self._outcomes.get(payload["model"])

        if not queue:
            raise ProviderError(f"no outcome configured for {payload['model']}")

        produced = False

        while queue:
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            if isinstance(outcome, dict):
                yield outcome
                produced = True
            elif outcome:
                yield {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": payload["model"],
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": outcome},
                            "finish_reason": None,
                        }
                    ],
                }
                produced = True

        if produced:
            yield {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": payload["model"],
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ],
            }


@pytest.fixture(autouse=True)
def fake_registry(monkeypatch):
    """Point every ClientRegistry at FakeClients, no real network."""
    from app.services import client_registry

    holder = {}

    def fake_get(self, provider_name):
        return holder[provider_name]

    monkeypatch.setattr(
        client_registry.ClientRegistry, "get", fake_get
    )
    return holder


def make_client(holder, name, outcomes_by_model=None, probes=None):
    client = FakeClient()

    for model, outcomes in (outcomes_by_model or {}).items():
        client.set_outcomes(model, outcomes)

    for model, probe in (probes or {}).items():
        client.set_probe(model, probe)

    holder[name] = client
    return client


@pytest.fixture
def wired_relay(monkeypatch, fake_registry):
    """
    Build a Relay with fake providers/clients and wire it into every API
    router in place of the module-level singleton.
    """

    relays = {}

    def _build(providers=None, clients=None):
        relay = Relay()

        for provider in providers or []:
            relay.provider_manager.register(provider)

        for name, client in (clients or {}).items():
            fake_registry[name] = client

        monkeypatch.setattr(app.api.chat, "relay", relay)
        monkeypatch.setattr(app.api.decision, "relay", relay)
        monkeypatch.setattr(app.api.health, "relay", relay)
        monkeypatch.setattr(app.api.providers, "relay", relay)
        monkeypatch.setattr(app.api.openai, "relay", relay)
        monkeypatch.setattr(app.api.diagnostics, "relay", relay)

        relays[id(relay)] = relay
        return relay

    yield _build

    for relay in relays.values():
        monkeypatch.setattr(app.api.chat, "relay", relay)
        monkeypatch.setattr(app.api.decision, "relay", relay)
        monkeypatch.setattr(app.api.health, "relay", relay)
        monkeypatch.setattr(app.api.providers, "relay", relay)
        monkeypatch.setattr(app.api.openai, "relay", relay)
        monkeypatch.setattr(app.api.diagnostics, "relay", relay)


@pytest.fixture
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


class TestOpenAIInputGuards:
    def test_max_tokens_above_cap_rejected(self, client):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 1_000_001,
            },
        )

        assert response.status_code == 422

    def test_too_many_messages_rejected(self, client):
        messages = [{"role": "user", "content": "m"} for _ in range(10_001)]

        response = client.post(
            "/v1/chat/completions",
            json={"model": "a-1", "messages": messages},
        )

        assert response.status_code == 422

    def test_max_tokens_at_cap_passes_validation(self, client):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 1_000_000,
            },
        )

        # Passed pydantic validation: without any wired provider the
        # endpoint reports "no providers available" (400), never a 422.
        assert response.status_code == 400


class TestOpenAIChatCompletions:
    def test_success_basic(self, wired_relay, fake_registry, client):
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(
            fake_registry,
            "A",
            {"a-1": ["hello world"], "a-2": ["nope"]},
        )
        wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["model"] == "a-1"
        assert payload["choices"][0]["message"]["content"] == "hello world"
        assert payload["choices"][0]["finish_reason"] == "stop"
        assert "id" in payload
        assert "created" in payload
        assert payload["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def test_success_with_parameters(self, wired_relay, fake_registry, client):
        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": ["param response"]},
        )
        wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hello"}],
                "temperature": 0.7,
                "top_p": 0.9,
                "max_tokens": 100,
                "stop": ["STOP"],
                "frequency_penalty": 0.5,
                "presence_penalty": 0.3,
                "seed": 42,
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["choices"][0]["message"]["content"] == "param response"

        # Verify the provider client received the parameters
        calls = fake_registry["A"].chat_calls
        assert len(calls) == 1
        _, payload = calls[0]
        assert payload["temperature"] == 0.7
        assert payload["top_p"] == 0.9
        assert payload["max_tokens"] == 100
        assert payload["stop"] == ["STOP"]
        assert payload["frequency_penalty"] == 0.5
        assert payload["presence_penalty"] == 0.3
        assert payload["seed"] == 42
        assert payload["messages"] == [{"role": "user", "content": "hello"}]

    def test_unknown_model_returns_400(self, wired_relay, client):
        provider = make_provider("A", ["a-1"])
        wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "unknown-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        assert response.status_code == 400
        payload = response.json()
        assert "error" in payload
        assert "unknown-model" in payload["error"]["message"]

    def test_provider_failure_maps_to_502(self, wired_relay, fake_registry, client):
        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(400, "bad request")]},
        )
        wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={"model": "a-1", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert response.status_code == 502
        payload = response.json()
        assert "error" in payload
        assert "bad request" in payload["error"]["message"]

    def test_no_providers_available_returns_400(self, wired_relay, client):
        wired_relay(providers=[])

        response = client.post(
            "/v1/chat/completions",
            json={"model": "a-1", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert response.status_code == 400
        payload = response.json()
        assert "error" in payload
        assert "not available" in payload["error"]["message"]

    def test_message_passthrough(self, wired_relay, fake_registry, client):
        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": ["passthrough response"]},
        )
        wired_relay(providers=[provider])

        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "how are you"},
        ]

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": messages,
            },
        )

        assert response.status_code == 200
        # Verify the exact message array reaches the provider: no flattening.
        calls = fake_registry["A"].chat_calls
        assert len(calls) == 1
        _, payload = calls[0]
        assert payload["messages"] == messages

    def test_streaming_response(self, wired_relay, fake_registry, client, monkeypatch):
        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": ["streamed response"]},
        )
        # Enable telemetry and health feedback for this test
        monkeypatch.setattr(settings, "telemetry_enabled", True)
        monkeypatch.setattr(settings, "health_feedback_enabled", True)
        relay = wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        # Collect the streamed content
        content = response.text
        # Should contain SSE data lines
        assert "data:" in content
        assert "[DONE]" in content
        # Check that the content is in the stream
        assert "streamed response" in content
        # Verify the SSE format: each data line is JSON
        lines = content.strip().split("\n")
        data_lines = [line for line in lines if line.startswith("data: ")]
        assert len(data_lines) >= 2  # at least one chunk and [DONE]
        # Last data line should be [DONE]
        assert data_lines[-1] == "data: [DONE]"
        # Parse the first chunk
        import json
        first_chunk = json.loads(data_lines[0][6:])
        assert first_chunk["choices"][0]["delta"]["content"] == "streamed response"
        assert first_chunk["choices"][0]["finish_reason"] is None
        # The second to last chunk should have finish_reason "stop"
        last_chunk = json.loads(data_lines[-2][6:])
        assert last_chunk["choices"][0]["finish_reason"] == "stop"

        # Verify telemetry recorded success
        telemetry = relay.telemetry
        stats = telemetry.get("A", "a-1")
        assert stats is not None
        assert stats.request_count == 1
        assert stats.success_count == 1
        assert stats.failure_count == 0

        # Verify health feedback recorded success (no learned degradation)
        learned = relay.health_store.learned("A")
        assert learned is None

    def test_streaming_failure_mid_stream(self, wired_relay, fake_registry, client, monkeypatch):
        """Test that a streaming failure mid-stream returns error chunk and [DONE], and records failure."""
        provider = make_provider("A", ["a-1"])
        # First yield a chunk, then raise an exception
        make_client(
            fake_registry,
            "A",
            {"a-1": ["first chunk", ProviderHTTPError(500, "stream server error")]},
        )
        monkeypatch.setattr(settings, "telemetry_enabled", True)
        monkeypatch.setattr(settings, "health_feedback_enabled", True)
        relay = wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        content = response.text
        # Should contain error chunk and [DONE]
        assert "stream_error" in content
        assert "[DONE]" in content
        # Verify that telemetry recorded failure
        telemetry = relay.telemetry
        stats = telemetry.get("A", "a-1")
        assert stats is not None
        assert stats.request_count == 1
        assert stats.success_count == 0
        assert stats.failure_count == 1

        # Verify health feedback recorded failure
        learned = relay.health_store.learned("A")
        assert learned is not None
        assert "a-1" in learned.degraded_models or "a-1" in learned.unavailable_models

    def test_streaming_fallback_when_first_provider_fails(self, wired_relay, fake_registry, client, monkeypatch):
        """Test that if first provider fails to start streaming, fallback to second provider."""
        provider1 = make_provider("A", ["a-1"], priority=10)
        provider2 = make_provider("B", ["a-1"], priority=5)
        # First provider fails to start streaming
        make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderTimeout("connection refused")]},
        )
        # Second provider succeeds
        make_client(
            fake_registry,
            "B",
            {"a-1": ["fallback streamed"]},
        )
        monkeypatch.setattr(settings, "telemetry_enabled", True)
        monkeypatch.setattr(settings, "health_feedback_enabled", True)
        relay = wired_relay(providers=[provider1, provider2])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        content = response.text
        assert "fallback streamed" in content
        assert "[DONE]" in content
        # Verify that the first provider was attempted (chat_stream called) but failed
        assert len(fake_registry["A"].chat_calls) == 1
        # The second provider should have been called
        assert len(fake_registry["B"].chat_calls) == 1

        # Verify telemetry recorded success for provider B
        telemetry = relay.telemetry
        stats_b = telemetry.get("B", "a-1")
        assert stats_b is not None
        assert stats_b.success_count == 1

        # Provider A should have failure recorded
        stats_a = telemetry.get("A", "a-1")
        assert stats_a is not None
        assert stats_a.failure_count == 1

    def test_telemetry_after_successful_stream(self, wired_relay, fake_registry, client, monkeypatch):
        """Test that telemetry is recorded after a successful stream."""
        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": ["successful stream"]},
        )
        monkeypatch.setattr(settings, "telemetry_enabled", True)
        monkeypatch.setattr(settings, "health_feedback_enabled", True)
        relay = wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        # Check telemetry
        telemetry = relay.telemetry
        stats = telemetry.get("A", "a-1")
        assert stats is not None
        assert stats.request_count == 1
        assert stats.success_count == 1
        assert stats.failure_count == 0

        # Health feedback should show no learned degradation after success
        learned = relay.health_store.learned("A")
        assert learned is None

    def test_telemetry_after_failed_stream(self, wired_relay, fake_registry, client, monkeypatch):
        """Test that telemetry records failure when stream fails."""
        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": ["first chunk", ProviderHTTPError(500, "stream server error")]},
        )
        monkeypatch.setattr(settings, "telemetry_enabled", True)
        monkeypatch.setattr(settings, "health_feedback_enabled", True)
        relay = wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        # Check telemetry
        telemetry = relay.telemetry
        stats = telemetry.get("A", "a-1")
        assert stats is not None
        assert stats.request_count == 1
        assert stats.success_count == 0
        assert stats.failure_count == 1

        # Health feedback should show degradation
        learned = relay.health_store.learned("A")
        assert learned is not None
        assert "a-1" in learned.degraded_models or "a-1" in learned.unavailable_models

    def test_streaming_empty_stream_fails_over(self, wired_relay, fake_registry, client, monkeypatch):
        """A provider that yields no content counts as a failed start and we fail over."""
        provider1 = make_provider("A", ["a-1"], priority=10)
        provider2 = make_provider("B", ["a-1"], priority=5)
        make_client(fake_registry, "A", {"a-1": [""]})
        make_client(fake_registry, "B", {"a-1": ["fallback after empty"]})
        monkeypatch.setattr(settings, "telemetry_enabled", True)
        monkeypatch.setattr(settings, "health_feedback_enabled", True)
        relay = wired_relay(providers=[provider1, provider2])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        content = response.text
        assert "fallback after empty" in content
        assert "[DONE]" in content

        stats_a = relay.telemetry.get("A", "a-1")
        assert stats_a is not None
        assert stats_a.request_count == 1
        assert stats_a.failure_count == 1

        stats_b = relay.telemetry.get("B", "a-1")
        assert stats_b is not None
        assert stats_b.success_count == 1

    def test_streaming_provider_exception_before_first_chunk(self, wired_relay, fake_registry, client, monkeypatch):
        """A provider that raises before the first chunk maps to 502 and records failure."""
        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(500, "start failed")]},
        )
        monkeypatch.setattr(settings, "telemetry_enabled", True)
        monkeypatch.setattr(settings, "health_feedback_enabled", True)
        relay = wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

        assert response.status_code == 502

        stats = relay.telemetry.get("A", "a-1")
        assert stats is not None
        assert stats.request_count == 1
        assert stats.failure_count == 1

        learned = relay.health_store.learned("A")
        assert learned is not None
        assert "a-1" in learned.degraded_models or "a-1" in learned.unavailable_models

    def test_streaming_final_termination(self, wired_relay, fake_registry, client, monkeypatch):
        """Every emitted chunk follows the chunk schema and the stream ends with exactly [DONE]."""
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["termination test"]})
        monkeypatch.setattr(settings, "telemetry_enabled", True)
        monkeypatch.setattr(settings, "health_feedback_enabled", True)
        wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        content = response.text
        lines = [line for line in content.strip().split("\n") if line.startswith("data: ")]

        # Exactly one [DONE] marker as the final line
        assert lines[-1] == "data: [DONE]"
        assert sum(1 for line in lines if line == "data: [DONE]") == 1

        # Every non-[DONE] line parses as a chat.completion.chunk
        for line in lines[:-1]:
            parsed = json.loads(line[6:])
            assert parsed["object"] == "chat.completion.chunk"
            assert "choices" in parsed
            choice = parsed["choices"][0]
            assert "delta" in choice
            assert "finish_reason" in choice

        # The final assistant chunk has an empty delta and finish_reason "stop"
        final_chunk = json.loads(lines[-2][6:])
        assert final_chunk["choices"][0]["delta"] == {}
        assert final_chunk["choices"][0]["finish_reason"] == "stop"

    def test_non_streaming_regression(self, wired_relay, fake_registry, client):
        """stream=false returns the regular JSON completion, not SSE."""
        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": ["normal json response"]},
        )
        wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
            },
        )

        assert response.status_code == 200
        assert "text/event-stream" not in response.headers.get("content-type", "")
        assert "data: " not in response.text
        payload = response.json()
        assert payload["object"] == "chat.completion"
        assert payload["choices"][0]["message"]["content"] == "normal json response"
        assert payload["choices"][0]["finish_reason"] == "stop"


class TestOpenAIModels:
    def test_models_endpoint_lists_models(self, wired_relay, client):
        provider = make_provider("A", ["a-1", "a-2"])
        provider2 = make_provider("B", ["b-1"])
        wired_relay(providers=[provider, provider2])

        response = client.get("/v1/models")

        assert response.status_code == 200
        payload = response.json()
        assert payload["object"] == "list"
        model_ids = {item["id"] for item in payload["data"]}
        # Upstream ids remain exposed so explicit passthrough clients
        # keep working unchanged...
        assert {"a-1", "a-2", "b-1"}.issubset(model_ids)
        # ...and the Relay-facing names are discoverable for automatic
        # and task-based routing.
        assert {"auto", "default", "relay"}.issubset(model_ids)
        assert {
            "coding", "vision", "reasoning", "general", "creative",
            "translation",
        }.issubset(model_ids)
        for item in payload["data"]:
            assert item["object"] == "model"
            if item["id"] in {"a-1", "a-2", "b-1"}:
                assert item["owned_by"] in {"A", "B"}
            else:
                assert item["owned_by"] == "relay"

    def test_models_endpoint_empty_when_no_providers(self, wired_relay, client):
        wired_relay(providers=[])

        response = client.get("/v1/models")

        assert response.status_code == 200
        payload = response.json()
        assert payload["object"] == "list"
        assert payload["data"] == []


class TestOpenAIVirtualModelRouting:
    """Relay-facing model interface on /v1 (Phase 3)."""

    def test_omitted_model_routes_automatically(
        self, wired_relay, fake_registry, client
    ):
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(
            fake_registry,
            "A",
            {"a-1": ["auto response"]},
        )
        wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["choices"][0]["message"]["content"] == "auto response"
        # The wire payload carries the concrete upstream model, never a
        # virtual name.
        calls = fake_registry["A"].chat_calls
        assert len(calls) == 1
        _, wire = calls[0]
        assert wire["model"] == "a-1"

    @pytest.mark.parametrize("virtual", ["auto", "default", "relay"])
    def test_virtual_model_names_route_automatically(
        self, wired_relay, fake_registry, client, virtual
    ):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": [f"{virtual} response"]})
        wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": virtual,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert (
            payload["choices"][0]["message"]["content"]
            == f"{virtual} response"
        )
        calls = fake_registry["A"].chat_calls
        assert len(calls) == 1
        _, wire = calls[0]
        assert wire["model"] == "a-1"

    def test_task_named_model_routes_via_preferences(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(fake_registry, "A", {"a-2": ["coding response"]})
        monkeypatch.setattr(settings, "task_routing_enabled", True)
        monkeypatch.setattr(settings, "task_coding", ["a-2"])
        wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "coding",
                "messages": [{"role": "user", "content": "write code"}],
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["choices"][0]["message"]["content"] == "coding response"
        calls = fake_registry["A"].chat_calls
        assert len(calls) == 1
        _, wire = calls[0]
        assert wire["model"] == "a-2"

    def test_omitted_model_classifies_task(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(
            fake_registry,
            "A",
            {"a-2": ["classified coding response"]},
        )
        monkeypatch.setattr(settings, "task_classification_enabled", True)
        monkeypatch.setattr(settings, "task_routing_enabled", True)
        monkeypatch.setattr(settings, "task_coding", ["a-2"])
        wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [
                    {"role": "user", "content": "write a python function"}
                ]
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert (
            payload["choices"][0]["message"]["content"]
            == "classified coding response"
        )
        calls = fake_registry["A"].chat_calls
        assert len(calls) == 1
        _, wire = calls[0]
        assert wire["model"] == "a-2"

    def test_streaming_virtual_model_reports_resolved_model(
        self, wired_relay, fake_registry, client
    ):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["streamed"]})
        wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        lines = response.text.strip().split("\n")
        data_lines = [line for line in lines if line.startswith("data: ")]
        first = json.loads(data_lines[0][6:])
        assert first["model"] == "a-1"

    def test_missing_model_no_providers_returns_400(
        self, wired_relay, client
    ):
        wired_relay(providers=[])

        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )

        assert response.status_code == 400
        payload = response.json()
        assert "error" in payload
        assert "automatic routing" in payload["error"]["message"]

    def test_decision_engine_records_on_virtual_routing(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["decided response"]})
        monkeypatch.setattr(settings, "decision_engine_enabled", True)
        relay = wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        assert response.status_code == 200
        stats = relay.decision_engine.stats()
        assert stats["decisions"] == 1
        assert stats["candidates"] == 1
        assert list(stats["selected"]) == ["A/a-1"]

    def test_decision_engine_skips_explicit_passthrough(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["passthrough response"]})
        monkeypatch.setattr(settings, "decision_engine_enabled", True)
        relay = wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        assert response.status_code == 200
        assert relay.decision_engine.stats()["decisions"] == 0


class TestActualDecisionRecord:
    """
    Phase 7 orchestration truth layer: the /v1 request records the actual
    decision (provider/model really executed), consistent with the wire
    payload, and /decision/explain/actual serves it back.
    """

    def _wire_task_routing(self, monkeypatch, **task_refs):
        monkeypatch.setattr(settings, "task_routing_enabled", True)
        for name, refs in task_refs.items():
            monkeypatch.setattr(settings, f"task_{name}", list(refs))

    def test_coding_request_actual_decision(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        provider = make_provider("A", ["coding-model", "a-2"])
        make_client(fake_registry, "A", {"coding-model": ["coding done"]})
        self._wire_task_routing(monkeypatch, coding=["coding-model"])
        monkeypatch.setattr(settings, "task_classification_enabled", True)
        relay = wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [
                    {"role": "user", "content": "write a python function"}
                ]
            },
        )

        assert response.status_code == 200
        calls = fake_registry["A"].chat_calls
        assert len(calls) == 1
        _, wire = calls[0]
        assert wire["model"] == "coding-model"

        record = relay.decision_record_store.most_recent()
        assert record is not None
        assert record.classified_task == "coding"
        assert record.routed is True
        assert record.selected_provider == "A"
        assert record.selected_model == "coding-model"
        assert record.outcome == "succeeded"
        assert record.requested_model is None
        assert record.selected_rank == 1
        assert record.selected_model == wire["model"]

    def test_reasoning_request_actual_decision(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        provider = make_provider("A", ["reason-model", "a-2"])
        make_client(fake_registry, "A", {"reason-model": ["reasoned"]})
        self._wire_task_routing(monkeypatch, reasoning=["reason-model"])
        monkeypatch.setattr(settings, "task_classification_enabled", True)
        relay = wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [
                    {"role": "user", "content": "solve this math logic puzzle"}
                ]
            },
        )

        assert response.status_code == 200
        calls = fake_registry["A"].chat_calls
        assert len(calls) == 1
        _, wire = calls[0]
        assert wire["model"] == "reason-model"

        record = relay.decision_record_store.most_recent()
        assert record is not None
        assert record.classified_task == "reasoning"
        assert record.selected_provider == "A"
        assert record.selected_model == "reason-model"
        assert record.outcome == "succeeded"
        assert record.selected_model == wire["model"]

    def test_general_request_actual_decision(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        provider = make_provider("A", ["general-model"])
        make_client(fake_registry, "A", {"general-model": ["generic reply"]})
        self._wire_task_routing(monkeypatch, general=["general-model"])
        monkeypatch.setattr(settings, "task_classification_enabled", True)
        relay = wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "hello there"}]
            },
        )

        assert response.status_code == 200
        calls = fake_registry["A"].chat_calls
        assert len(calls) == 1
        _, wire = calls[0]
        assert wire["model"] == "general-model"

        record = relay.decision_record_store.most_recent()
        assert record is not None
        assert record.classified_task == "general"
        assert record.selected_model == "general-model"
        assert record.selected_model == wire["model"]

    def test_explicit_model_passthrough_preserved(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(fake_registry, "A", {"a-1": ["verbatim reply"]})
        monkeypatch.setattr(settings, "task_routing_enabled", True)
        monkeypatch.setattr(settings, "task_coding", ["a-2"])
        relay = wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        assert response.status_code == 200
        calls = fake_registry["A"].chat_calls
        assert len(calls) == 1
        _, wire = calls[0]
        assert wire["model"] == "a-1"

        record = relay.decision_record_store.most_recent()
        assert record is not None
        assert record.routed is False
        assert record.requested_model == "a-1"
        assert record.selected_model == "a-1"
        assert record.selected_model == wire["model"]
        assert record.decision_reason == (
            "explicit upstream model passthrough"
        )
        # The decision engine still skips explicit passthrough.
        assert relay.decision_engine.stats()["decisions"] == 0

    @pytest.mark.parametrize("virtual", ["auto", "default", "relay"])
    def test_virtual_models_route_and_record(
        self, wired_relay, fake_registry, client, virtual
    ):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["routed reply"]})
        relay = wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": virtual,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        assert response.status_code == 200
        calls = fake_registry["A"].chat_calls
        assert len(calls) == 1
        _, wire = calls[0]
        assert wire["model"] == "a-1"

        record = relay.decision_record_store.most_recent()
        assert record is not None
        assert record.routed is True
        assert record.requested_model == virtual
        assert record.selected_model == "a-1"
        assert record.selected_model == wire["model"]

    def test_omitted_model_routes_and_records(
        self, wired_relay, fake_registry, client
    ):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["auto reply"]})
        relay = wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        assert response.status_code == 200
        calls = fake_registry["A"].chat_calls
        assert len(calls) == 1
        _, wire = calls[0]
        assert wire["model"] == "a-1"

        record = relay.decision_record_store.most_recent()
        assert record is not None
        assert record.routed is True
        assert record.requested_model is None
        assert record.selected_model == "a-1"
        assert record.selected_model == wire["model"]

    def test_failover_actual_decision_reports_executed_candidate(
        self, wired_relay, fake_registry, client
    ):
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(
            fake_registry,
            "A",
            {
                "a-1": [ProviderError("a-1 unavailable")],
                "a-2": ["recovered"],
            },
        )
        relay = wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        assert response.status_code == 200
        record = relay.decision_record_store.most_recent()
        assert record is not None
        assert record.selected_model == "a-2"
        assert record.selected_rank == 2
        assert record.outcome == "succeeded"
        assert record.decision_reason == "routed; executed candidate rank 2"
        assert len(record.attempts) >= 2
        assert record.attempts[0].success is False
        assert record.attempts[-1].success is True

    def test_decision_engine_signals_attached_when_enabled(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["decided reply"]})
        self._wire_task_routing(monkeypatch, coding=["a-1"])
        monkeypatch.setattr(settings, "task_classification_enabled", True)
        monkeypatch.setattr(settings, "decision_engine_enabled", True)
        relay = wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [
                    {"role": "user", "content": "write python code"}
                ]
            },
        )

        assert response.status_code == 200
        record = relay.decision_record_store.most_recent()
        assert record is not None
        assert record.classified_task == "coding"
        assert record.selected_model == "a-1"
        assert record.decision_reason is not None
        assert "health_band=" in record.decision_reason
        assert record.confidence is not None
        assert record.signals is not None
        assert "priority" in record.signals
        # The engine pass still recorded statistics for the routed request.
        assert relay.decision_engine.stats()["decisions"] == 1

    def test_streaming_actual_decision_reports_final_outcome(
        self, wired_relay, fake_registry, client
    ):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["streamed"]})
        relay = wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        record = relay.decision_record_store.most_recent()
        assert record is not None
        assert record.selected_model == "a-1"
        assert record.outcome == "succeeded"


class TestActualDecisionExplainEndpoint:
    def test_actual_explain_disabled_by_default(self, wired_relay, client):
        wired_relay(providers=[make_provider("A", ["a-1"])])

        response = client.get("/decision/explain/actual")

        assert response.status_code == 200
        assert response.json() == {
            "enabled": False,
            "message": "Decision explanations are disabled.",
        }

    def test_actual_explain_returns_most_recent(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["reply"]})
        monkeypatch.setattr(settings, "decision_explanations_enabled", True)
        relay = wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        assert response.status_code == 200
        correlation_id = response.headers["X-Relay-Correlation-Id"]
        record = relay.decision_record_store.most_recent()
        assert record is not None

        explain = client.get("/decision/explain/actual")

        assert explain.status_code == 200
        payload = explain.json()
        assert payload["correlation_id"] == correlation_id
        assert payload["selected_model"] == "a-1"
        assert payload["routed"] is True

    def test_actual_explain_by_correlation_id(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["reply"]})
        monkeypatch.setattr(settings, "decision_explanations_enabled", True)
        relay = wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        assert response.status_code == 200
        correlation_id = response.headers["X-Relay-Correlation-Id"]

        explain = client.get(
            "/decision/explain/actual",
            params={"correlation_id": correlation_id},
        )

        assert explain.status_code == 200
        assert explain.json()["correlation_id"] == correlation_id
        assert explain.json()["selected_model"] == "a-1"

    def test_actual_explain_unknown_correlation_404(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        make_client(fake_registry, "A", {"a-1": ["reply"]})
        monkeypatch.setattr(settings, "decision_explanations_enabled", True)
        wired_relay(providers=[make_provider("A", ["a-1"])])

        explain = client.get(
            "/decision/explain/actual",
            params={"correlation_id": "does-not-exist"},
        )

        assert explain.status_code == 404

    def test_actual_explain_empty_store_404(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        make_client(fake_registry, "A", {"a-1": ["reply"]})
        monkeypatch.setattr(settings, "decision_explanations_enabled", True)
        wired_relay(providers=[make_provider("A", ["a-1"])])

        response = client.get("/decision/explain/actual")

        assert response.status_code == 404

    def test_predictive_explain_still_available(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["reply"]})
        monkeypatch.setattr(settings, "decision_explanations_enabled", True)
        wired_relay(providers=[provider])

        response = client.get("/decision/explain")

        assert response.status_code == 200
        payload = response.json()
        assert payload["selected"] == {"provider": "A", "model": "a-1"}
        assert "candidates" in payload
        assert "generated_at" in payload


class TestRegressionChatEndpoint:
    def test_chat_endpoint_unchanged(self, wired_relay, fake_registry, client):
        """Ensure the existing /chat endpoint still works and behaves identically."""
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(
            fake_registry,
            "A",
            {"a-1": ["chat response"], "a-2": ["fallback"]},
        )
        wired_relay(providers=[provider])

        # Use the original /chat endpoint
        response = client.post(
            "/chat",
            json={"message": "hello"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["provider"] == "A"
        assert payload["model"] == "a-1"
        assert payload["response"] == "chat response"

    def test_chat_endpoint_failover(self, wired_relay, fake_registry, client):
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderTimeout("timeout")], "a-2": ["fallback ok"]},
        )
        wired_relay(providers=[provider])

        response = client.post(
            "/chat",
            json={"message": "hello"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["model"] == "a-2"
        assert payload["response"] == "fallback ok"

    def test_chat_endpoint_with_parameters(self, wired_relay, fake_registry, client):
        """Test that /chat endpoint accepts and forwards generation parameters."""
        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": ["param chat response"]},
        )
        wired_relay(providers=[provider])

        response = client.post(
            "/chat",
            json={
                "message": "hello",
                "temperature": 0.5,
                "top_p": 0.8,
                "max_tokens": 50,
                "stop": ["END"],
                "frequency_penalty": 0.2,
                "presence_penalty": 0.1,
                "seed": 123,
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["response"] == "param chat response"

        # Verify the provider client received the parameters
        calls = fake_registry["A"].chat_calls
        assert len(calls) == 1
        _, _, _, kwargs = calls[0]
        assert kwargs.get("temperature") == 0.5
        assert kwargs.get("top_p") == 0.8
        assert kwargs.get("max_tokens") == 50
        assert kwargs.get("stop") == ["END"]
        assert kwargs.get("frequency_penalty") == 0.2
        assert kwargs.get("presence_penalty") == 0.1
        assert kwargs.get("seed") == 123

    def test_chat_endpoint_without_parameters_uses_defaults(self, wired_relay, fake_registry, client):
        """Ensure omitting parameters preserves old defaults."""
        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": ["default response"]},
        )
        wired_relay(providers=[provider])

        response = client.post(
            "/chat",
            json={"message": "hello"},
        )

        assert response.status_code == 200
        calls = fake_registry["A"].chat_calls
        assert len(calls) == 1
        _, _, _, kwargs = calls[0]
        # No generation kwargs should be present
        assert "temperature" not in kwargs
        assert "top_p" not in kwargs
        assert "max_tokens" not in kwargs
        assert "stop" not in kwargs
        assert "frequency_penalty" not in kwargs
        assert "presence_penalty" not in kwargs
        assert "seed" not in kwargs

    def test_chat_endpoint_empty_content_returns_502(self, wired_relay, fake_registry, client):
        """A provider success with empty content must not crash /chat."""
        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": [None]},
        )
        wired_relay(providers=[provider])

        response = client.post(
            "/chat",
            json={"message": "hello"},
        )

        assert response.status_code == 502
        assert "empty content" in response.json()["detail"]

    def test_chat_endpoint_fails_over_empty_content(self, wired_relay, fake_registry, client):
        """/chat fails over to the next candidate when one returns empty content."""
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(
            fake_registry,
            "A",
            {"a-1": [None], "a-2": ["fallback ok"]},
        )
        wired_relay(providers=[provider])

        response = client.post(
            "/chat",
            json={"message": "hello"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["model"] == "a-2"
        assert payload["response"] == "fallback ok"


class TestPrivacy:
    def test_no_api_keys_in_response(self, wired_relay, fake_registry, client):
        provider = make_provider("A", ["a-1"], api_key="sk-secret-key")
        make_client(
            fake_registry,
            "A",
            {"a-1": ["privacy test"]},
        )
        wired_relay(providers=[provider])

        # Check both endpoints
        for endpoint in ["/v1/chat/completions", "/v1/models"]:
            if endpoint == "/v1/chat/completions":
                response = client.post(
                    endpoint,
                    json={"model": "a-1", "messages": [{"role": "user", "content": "hi"}]},
                )
            else:
                response = client.get(endpoint)

            assert response.status_code == 200
            raw = response.text
            assert "sk-secret-key" not in raw
            assert "api_key" not in raw.lower()  # Ensure no accidental leakage

    def test_no_prompts_or_responses_in_logs(self, wired_relay, fake_registry, client, caplog):
        """Ensure prompts and responses are not logged."""
        import logging
        caplog.set_level(logging.DEBUG)

        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": ["secret response"]},
        )
        wired_relay(providers=[provider])

        client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "secret prompt"}],
            },
        )

        logs = caplog.text
        assert "secret prompt" not in logs
        assert "secret response" not in logs


class TestV1HealthLearning:
    """
    The /v1/chat/completions path must feed per-attempt telemetry and
    health feedback exactly like the /chat pipeline, so routing learns
    from real failures and a request recovered by failover does not
    unfairly degrade the provider that served it.
    """

    def test_v1_failover_records_failure_and_success(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        monkeypatch.setattr(settings, "telemetry_enabled", True)
        monkeypatch.setattr(settings, "health_feedback_enabled", True)

        provider_a = make_provider("A", ["a-1"])
        provider_b = make_provider("B", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(500, "boom")]},
        )
        make_client(
            fake_registry,
            "B",
            {"a-1": ["ok"]},
        )
        relay = wired_relay(providers=[provider_a, provider_b])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        assert response.status_code == 200
        assert response.json()["model"] == "a-1"
        assert response.json()["choices"][0]["message"]["content"] == "ok"

        # a-1's real server_error is learned as a model-level degradation.
        learned_a = relay.health_store.learned("A")
        assert learned_a is not None
        assert "a-1" in learned_a.degraded_models
        assert learned_a.unavailable_models == frozenset()
        # The provider as a whole is not taken down by the recovered
        # failover, and the winning provider stays clean.
        assert learned_a.provider_status is None
        assert relay.health_store.learned("B") is None

        # Telemetry recorded the failed attempts too.
        assert len(relay.telemetry.recent_failures("A", "a-1")) == 2

    def test_v1_retry_recovery_clears_learned_degradation(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        monkeypatch.setattr(settings, "telemetry_enabled", True)
        monkeypatch.setattr(settings, "health_feedback_enabled", True)

        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(500, "boom"), "ok"]},
        )
        relay = wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        assert response.status_code == 200

        # The retry recovered on the same model; the recorded success
        # clears the transient degradation.
        assert relay.health_store.learned("A") is None
        assert len(relay.telemetry.recent_failures("A", "a-1")) == 1

    def test_v1_all_attempts_fail_records_each(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        monkeypatch.setattr(settings, "telemetry_enabled", True)
        monkeypatch.setattr(settings, "health_feedback_enabled", True)

        provider_a = make_provider("A", ["a-1"])
        provider_b = make_provider("B", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(500, "boom")]},
        )
        make_client(
            fake_registry,
            "B",
            {"a-1": [ProviderHTTPError(500, "boom")]},
        )
        relay = wired_relay(providers=[provider_a, provider_b])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        assert response.status_code == 502

        learned_a = relay.health_store.learned("A")
        learned_b = relay.health_store.learned("B")
        assert "a-1" in learned_a.degraded_models
        assert "a-1" in learned_b.degraded_models
        assert len(relay.telemetry.recent_failures("A", "a-1")) >= 2
        assert len(relay.telemetry.recent_failures("B", "a-1")) >= 2