"""
Phase 7A/7B API routing tests.

Exercises the API-layer behavior of task classification, the task
capability catalog, and the request correlation id:
- classification on/off semantics for explicit vs. missing vs. invalid
  task fields
- catalog-driven model selection (flag on) vs. legacy ordering (flag
  off), with the health-band invariant preserved in both cases
- X-Relay-Correlation-Id on success and error responses, including the
  OpenAI-compatible endpoints
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.core.relay import Relay
from app.main import app as fastapi_app
from app.providers.base import ModelProbe, Provider
from app.providers.exceptions import (
    ProviderError,
    ProviderHTTPError,
)
from app.services.health_checker import DEGRADED, HEALTHY, ProviderHealth

import app.api.chat
import app.api.decision
import app.api.health
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


def make_report(
    name,
    status,
    healthy=(),
    degraded=(),
    unavailable=(),
    unsupported=(),
):
    return ProviderHealth(
        name=name,
        status=status,
        latency_ms=5,
        last_checked="now",
        details="ok",
        connectivity=True,
        rate_limit_status="ok",
        last_successful_request=None,
        healthy_models=list(healthy),
        degraded_models=list(degraded),
        unavailable_models=list(unavailable),
        unsupported_models=list(unsupported),
    )


class FakeClient:
    def __init__(self):
        self.chat_calls = []
        self.probe_calls = []
        self._outcomes = {}
        self._probes = {}
        self._streams = {}

    def set_outcomes(self, model, outcomes):
        self._outcomes[model] = list(outcomes)

    def set_probe(self, model, probe):
        self._probes[model] = probe

    def set_stream(self, model, chunks):
        self._streams[model] = list(chunks)

    def chat(self, provider, model, message, timeout=None, max_tokens=None, **kwargs):
        self.chat_calls.append((provider.name, model))

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
        queue = self._streams.get(model)

        if queue is None:
            raise ProviderError(f"no stream outcome configured for {model}")

        def gen():
            for chunk in queue:
                yield chunk

        return gen()

    async def achat(self, provider, model, message, timeout=None, max_tokens=None, **kwargs):
        """Async version of chat()."""
        self.chat_calls.append((provider.name, model))

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
        queue = self._streams.get(model)

        if queue is None:
            raise ProviderError(f"no stream outcome configured for {model}")

        async def gen():
            for chunk in queue:
                yield chunk

        return gen()

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

        queue = self._streams.get(payload["model"])

        if queue is None:
            raise ProviderError(f"no stream outcome configured for {payload['model']}")

        for chunk in queue:
            yield {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": chunk},
                        "finish_reason": None,
                    }
                ],
            }
        yield {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": payload["model"],
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }

    async def achat_messages(self, provider, payload):
        """Async version of chat_messages()."""
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
        """Async version of chat_stream_messages()."""
        self.chat_calls.append((provider.name, payload))

        queue = self._streams.get(payload["model"])

        if queue is None:
            raise ProviderError(f"no stream outcome configured for {payload['model']}")

        for chunk in queue:
            yield {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": chunk},
                        "finish_reason": None,
                    }
                ],
            }
        yield {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": payload["model"],
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }

    def probe_model(self, provider, model):
        self.probe_calls.append((provider.name, model))

        probe = self._probes.get(model)

        if probe is None:
            return ModelProbe(False, 0, 404, "missing probe")

        return probe


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


def make_client(holder, name, outcomes_by_model=None, probes=None, streams=None):
    client = FakeClient()

    for model, outcomes in (outcomes_by_model or {}).items():
        client.set_outcomes(model, outcomes)

    for model, probe in (probes or {}).items():
        client.set_probe(model, probe)

    for model, chunks in (streams or {}).items():
        client.set_stream(model, chunks)

    holder[name] = client
    return client


@pytest.fixture
def wired_relay(monkeypatch, fake_registry):
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

        relays[id(relay)] = relay
        return relay

    yield _build


@pytest.fixture
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


def _capture_task(relay, monkeypatch):
    """
    Wrap relay.achat so tests can observe the routing task the API layer
    actually resolved (explicit vs. classified).
    """
    captured = {}
    original_achat = relay.achat

    async def wrapped(message, task=None, **generation_kwargs):
        captured["task"] = task
        return await original_achat(message, task=task, **generation_kwargs)

    monkeypatch.setattr(relay, "achat", wrapped)
    return captured


def _hex32(value):
    return re.fullmatch(r"[0-9a-f]{32}", value) is not None


class TestCorrelationHeaders:
    def test_chat_success_carries_correlation_header(
        self, wired_relay, fake_registry, client
    ):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["hello"]})
        wired_relay(providers=[provider])

        response = client.post("/chat", json={"message": "hello"})

        assert response.status_code == 200
        header = response.headers.get("X-Relay-Correlation-Id")
        assert header
        assert _hex32(header)

    def test_chat_502_carries_correlation_header(
        self, wired_relay, fake_registry, client
    ):
        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(500, "down")]},
        )
        wired_relay(providers=[provider])

        response = client.post("/chat", json={"message": "hi"})

        assert response.status_code == 502
        header = response.headers.get("X-Relay-Correlation-Id")
        assert header
        assert _hex32(header)

    def test_chat_503_carries_correlation_header(self, wired_relay, client):
        wired_relay(providers=[])

        response = client.post("/chat", json={"message": "hi"})

        assert response.status_code == 503
        header = response.headers.get("X-Relay-Correlation-Id")
        assert header
        assert _hex32(header)


class TestTaskClassificationThroughAPI:
    def test_classification_disabled_invalid_task_is_400(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "task_classification_enabled", False)
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["ok"]})
        wired_relay(providers=[provider])

        response = client.post(
            "/chat",
            json={"message": "fix this python bug", "task": "bogus"},
        )

        assert response.status_code == 400
        assert "bogus" in response.json()["detail"]

    def test_classification_disabled_valid_task_passes_through(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "task_classification_enabled", False)
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["ok"]})
        relay = wired_relay(providers=[provider])
        captured = _capture_task(relay, monkeypatch)

        response = client.post(
            "/chat",
            json={"message": "hello", "task": "coding"},
        )

        assert response.status_code == 200
        assert captured["task"] == "coding"

    def test_classification_disabled_missing_task_is_none(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "task_classification_enabled", False)
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["ok"]})
        relay = wired_relay(providers=[provider])
        captured = _capture_task(relay, monkeypatch)

        response = client.post("/chat", json={"message": "hello"})

        assert response.status_code == 200
        assert captured["task"] is None

    def test_classification_enabled_valid_explicit_task_overrides(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "task_classification_enabled", True)
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["ok"]})
        relay = wired_relay(providers=[provider])
        captured = _capture_task(relay, monkeypatch)

        response = client.post(
            "/chat",
            json={"message": "fix this python bug", "task": "creative"},
        )

        assert response.status_code == 200
        assert captured["task"] == "creative"

    def test_classification_enabled_invalid_explicit_task_is_classified(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "task_classification_enabled", True)
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["ok"]})
        relay = wired_relay(providers=[provider])
        captured = _capture_task(relay, monkeypatch)

        response = client.post(
            "/chat",
            json={"message": "fix this python bug", "task": "bogus"},
        )

        assert response.status_code == 200
        assert captured["task"] == "coding"

    def test_classification_enabled_missing_task_is_classified(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "task_classification_enabled", True)
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["ok"]})
        relay = wired_relay(providers=[provider])
        captured = _capture_task(relay, monkeypatch)

        response = client.post(
            "/chat",
            json={"message": "describe this image"},
        )

        assert response.status_code == 200
        assert captured["task"] == "vision"

    def test_classification_enabled_weak_match_falls_back_to_general(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "task_classification_enabled", True)
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["ok"]})
        relay = wired_relay(providers=[provider])
        captured = _capture_task(relay, monkeypatch)

        response = client.post(
            "/chat",
            json={"message": "hello there friend"},
        )

        assert response.status_code == 200
        assert captured["task"] == "general"


class TestTaskCatalogThroughAPI:
    def _enable(self, monkeypatch, catalog=True, health=True):
        from app.core.config import settings

        monkeypatch.setattr(settings, "task_catalog_enabled", catalog)
        monkeypatch.setattr(settings, "health_aware_routing", health)
        monkeypatch.setattr(settings, "scoring_task_compatibility_weight", 1.0)

    def _wire_equal_telemetry(self, relay, model_a, model_b):
        relay.telemetry.record_attempt("A", model_a, True, 100)
        relay.telemetry.record_attempt("B", model_b, True, 100)

    def test_catalog_on_prefers_task_compatible_model(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        self._enable(monkeypatch, catalog=True)
        p_a = make_provider("A", ["gpt-4o-2024-05-13"], priority=10)
        p_b = make_provider("B", ["gpt-3.5-turbo"], priority=1)
        make_client(fake_registry, "A", {"gpt-4o-2024-05-13": ["from-4o"]})
        make_client(fake_registry, "B", {"gpt-3.5-turbo": ["from-35"]})
        relay = wired_relay(providers=[p_a, p_b])
        relay.health_store.save(
            make_report("A", HEALTHY, healthy=("gpt-4o-2024-05-13",))
        )
        relay.health_store.save(
            make_report("B", HEALTHY, healthy=("gpt-3.5-turbo",))
        )
        self._wire_equal_telemetry(relay, "gpt-4o-2024-05-13", "gpt-3.5-turbo")

        response = client.post(
            "/chat",
            json={"message": "describe this image", "task": "vision"},
        )

        assert response.status_code == 200
        assert response.json()["provider"] == "A"
        assert response.json()["model"] == "gpt-4o-2024-05-13"

    def test_catalog_on_preserves_health_band_invariant(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        self._enable(monkeypatch, catalog=True)
        p_a = make_provider("A", ["gpt-4o-2024-05-13"], priority=10)
        p_b = make_provider("B", ["gpt-3.5-turbo"], priority=1)
        make_client(fake_registry, "A", {"gpt-4o-2024-05-13": ["from-4o"]})
        make_client(fake_registry, "B", {"gpt-3.5-turbo": ["from-35"]})
        relay = wired_relay(providers=[p_a, p_b])
        relay.health_store.save(
            make_report("A", DEGRADED, degraded=("gpt-4o-2024-05-13",))
        )
        relay.health_store.save(
            make_report("B", HEALTHY, healthy=("gpt-3.5-turbo",))
        )
        self._wire_equal_telemetry(relay, "gpt-4o-2024-05-13", "gpt-3.5-turbo")

        response = client.post(
            "/chat",
            json={"message": "describe this image", "task": "vision"},
        )

        assert response.status_code == 200
        assert response.json()["provider"] == "B"

    def test_catalog_off_keeps_legacy_priority_ordering(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        self._enable(monkeypatch, catalog=False)
        p_a = make_provider("A", ["gpt-3.5-turbo"], priority=5)
        p_b = make_provider("B", ["gpt-5.6-sol"], priority=5)
        make_client(fake_registry, "A", {"gpt-3.5-turbo": ["from-35"]})
        make_client(fake_registry, "B", {"gpt-5.6-sol": ["from-56"]})
        relay = wired_relay(providers=[p_a, p_b])
        relay.health_store.save(
            make_report("A", HEALTHY, healthy=("gpt-3.5-turbo",))
        )
        relay.health_store.save(
            make_report("B", HEALTHY, healthy=("gpt-5.6-sol",))
        )
        self._wire_equal_telemetry(relay, "gpt-3.5-turbo", "gpt-5.6-sol")

        response = client.post(
            "/chat",
            json={"message": "write a function", "task": "coding"},
        )

        # Equal legacy fitness: stable input order; catalog (if enabled)
        # would prefer gpt-5.6-sol for coding.
        assert response.status_code == 200
        assert response.json()["provider"] == "A"

    def test_catalog_on_flips_ordering_for_coding(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        self._enable(monkeypatch, catalog=True)
        p_a = make_provider("A", ["gpt-3.5-turbo"], priority=5)
        p_b = make_provider("B", ["gpt-5.6-sol"], priority=5)
        make_client(fake_registry, "A", {"gpt-3.5-turbo": ["from-35"]})
        make_client(fake_registry, "B", {"gpt-5.6-sol": ["from-56"]})
        relay = wired_relay(providers=[p_a, p_b])
        relay.health_store.save(
            make_report("A", HEALTHY, healthy=("gpt-3.5-turbo",))
        )
        relay.health_store.save(
            make_report("B", HEALTHY, healthy=("gpt-5.6-sol",))
        )
        self._wire_equal_telemetry(relay, "gpt-3.5-turbo", "gpt-5.6-sol")

        response = client.post(
            "/chat",
            json={"message": "write a function", "task": "coding"},
        )

        assert response.status_code == 200
        assert response.json()["provider"] == "B"
        assert response.json()["model"] == "gpt-5.6-sol"


class TestOpenAICorrelationHeader:
    def _wire_openai(self, wired_relay, fake_registry, monkeypatch, providers):
        import app.api.openai

        relay = wired_relay(providers=providers)
        monkeypatch.setattr(app.api.openai, "relay", relay)
        return relay

    def test_non_streaming_carries_correlation_header(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        provider = make_provider("A", ["gpt-4o-mini"])
        make_client(fake_registry, "A", {"gpt-4o-mini": ["hello"]})
        self._wire_openai(wired_relay, fake_registry, monkeypatch, [provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 200
        header = response.headers.get("X-Relay-Correlation-Id")
        assert header
        assert _hex32(header)

    def test_streaming_carries_correlation_header(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        provider = make_provider("A", ["gpt-4o-mini"])
        make_client(
            fake_registry,
            "A",
            streams={"gpt-4o-mini": ["chunk1", "chunk2"]},
        )
        self._wire_openai(wired_relay, fake_registry, monkeypatch, [provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 200
        header = response.headers.get("X-Relay-Correlation-Id")
        assert header
        assert _hex32(header)
        assert "chunk1" in response.text

    def test_unknown_model_400_carries_correlation_header(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        provider = make_provider("A", ["gpt-4o-mini"])
        self._wire_openai(wired_relay, fake_registry, monkeypatch, [provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "no-such-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 400
        header = response.headers.get("X-Relay-Correlation-Id")
        assert header
        assert _hex32(header)
