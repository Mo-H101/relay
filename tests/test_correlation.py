"""
Correlation id behavior (Phase 0).

Correlation ids are random, opaque tokens generated per chat request,
carried on the result dict, emitted as a response header, and never
persisted.
"""

import pytest

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.relay import Relay
from app.main import app as fastapi_app
from app.providers.base import Provider
from app.providers.exceptions import ProviderError
from app.services.correlation import new_correlation_id

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


class FakeClient:
    def __init__(self):
        self._outcomes = {}

    def set_outcomes(self, model, outcomes):
        self._outcomes[model] = list(outcomes)

    def chat(self, provider, model, message, timeout=None, max_tokens=None):
        queue = self._outcomes.get(model)

        if not queue:
            raise ProviderError(f"no outcome configured for {model}")

        outcome = queue[0]

        if len(queue) > 1:
            queue.pop(0)

        if isinstance(outcome, Exception):
            raise outcome

        return outcome

    async def achat(self, provider, model, message, timeout=None, max_tokens=None):
        """Async version of chat()."""
        queue = self._outcomes.get(model)

        if not queue:
            raise ProviderError(f"no outcome configured for {model}")

        outcome = queue[0]

        if len(queue) > 1:
            queue.pop(0)

        if isinstance(outcome, Exception):
            raise outcome

        return outcome

    async def achat_stream(self, provider, model, message, timeout=None, max_tokens=None):
        """Async version of chat_stream()."""
        queue = self._outcomes.get(model)

        if not queue:
            raise ProviderError(f"no outcome configured for {model}")

        while queue:
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome:
                yield outcome


@pytest.fixture(autouse=True)
def fake_registry(monkeypatch):
    from app.services import client_registry

    holder = {}

    monkeypatch.setattr(
        client_registry.ClientRegistry,
        "get",
        lambda self, provider_name: holder[provider_name],
    )
    return holder


@pytest.fixture
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


def make_client(holder, name, outcomes):
    fake = FakeClient()

    for model, queue in (outcomes or {}).items():
        fake.set_outcomes(model, queue)

    holder[name] = fake
    return fake


def wire_relay(monkeypatch, relay):
    monkeypatch.setattr(app.api.chat, "relay", relay)
    monkeypatch.setattr(app.api.decision, "relay", relay)
    monkeypatch.setattr(app.api.health, "relay", relay)
    monkeypatch.setattr(app.api.providers, "relay", relay)


class TestNewCorrelationId:
    def test_returns_hex_string(self):
        cid = new_correlation_id()

        assert isinstance(cid, str)
        assert len(cid) == 32
        assert all(c in "0123456789abcdef" for c in cid)

    def test_ids_are_unique(self):
        ids = {new_correlation_id() for _ in range(100)}

        assert len(ids) == 100


class TestRelayChatCorrelationId:
    def test_chat_without_providers_still_returns_id(self):
        relay = Relay()

        result = relay.chat("hello")

        assert result["success"] is False
        assert isinstance(result["correlation_id"], str)

    def test_correlation_id_not_in_durable_exports(
        self, fake_registry, monkeypatch
    ):
        monkeypatch.setattr(settings, "telemetry_enabled", True)
        monkeypatch.setattr(settings, "health_feedback_enabled", True)

        relay = Relay()
        relay.provider_manager.register(make_provider("A", ["a-1"]))
        make_client(fake_registry, "A", {"a-1": ["ok"]})

        result = relay.chat("hello")

        assert result["success"] is True
        cid = result["correlation_id"]

        serialized = str(relay.telemetry.export_state()) + str(
            relay.health_store.export_learned_state()
        )

        assert cid not in serialized


class TestChatEndpointHeader:
    def test_success_response_sets_header(
        self, fake_registry, client, monkeypatch
    ):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["hello world"]})
        relay = Relay()
        relay.provider_manager.register(provider)
        wire_relay(monkeypatch, relay)

        response = client.post("/chat", json={"message": "hello"})

        assert response.status_code == 200
        assert response.headers.get("X-Relay-Correlation-Id")

    def test_502_error_response_sets_header(
        self, fake_registry, client, monkeypatch
    ):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": [ProviderError("boom")]})
        relay = Relay()
        relay.provider_manager.register(provider)
        wire_relay(monkeypatch, relay)

        response = client.post("/chat", json={"message": "hello"})

        assert response.status_code == 502
        assert response.headers.get("X-Relay-Correlation-Id")

    def test_503_error_response_sets_header(self, client, monkeypatch):
        relay = Relay()
        wire_relay(monkeypatch, relay)

        response = client.post("/chat", json={"message": "hello"})

        assert response.status_code == 503
        assert response.headers.get("X-Relay-Correlation-Id")
