"""
HTTP integration tests for the P9c continuity layer.

Covers the wire contract end-to-end: conversation header echo, server
issued conversation ids, malformed-header rejection (generic 400), the
additive ``relay:*`` SSE events on /v1 streams, envelope injection on
resume, write-behind durability through the flusher, switch caps on the
failover path, and the flag-off parity guarantee.
"""

import json
import pytest

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.relay import Relay
from app.main import app as fastapi_app
from app.providers.base import ModelProbe, Provider
from app.providers.exceptions import ProviderError
from app.services.continuity_headers import derive_project_key

import app.api.chat
import app.api.diagnostics
import app.api.decision
import app.api.health
import app.api.openai
import app.api.providers
from app.security.auth import _reset_key_store
from app.services.key_store import KeyStore
from app.services.metrics import relay_metrics

_CID_HEADER = "X-Relay-Conversation-Id"


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
    Deterministic client for chat + probe flows. Outcome queues are
    per-model; an Exception instance is raised, otherwise the string (or
    dict) is returned. Message-style calls record the full payload so
    tests can assert on envelope injection.
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

    def _take(self, model):
        queue = self._outcomes.get(model)
        if not queue:
            raise ProviderError(f"no outcome configured for {model}")
        outcome = queue[0]
        if len(queue) > 1:
            queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

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

    def _chunk(self, model, content):
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": content},
                    "finish_reason": None,
                }
            ],
        }

    def chat(self, provider, model, message, **kwargs):
        self.chat_calls.append((provider.name, model, message))
        return self._take(model)

    async def achat(self, provider, model, message, **kwargs):
        self.chat_calls.append((provider.name, model, message))
        return self._take(model)

    def chat_stream(self, provider, model, message, **kwargs):
        self.chat_calls.append((provider.name, model, message))
        yield self._take(model)

    async def achat_stream(self, provider, model, message, **kwargs):
        self.chat_calls.append((provider.name, model, message))
        yield self._take(model)

    def chat_messages(self, provider, payload):
        self.chat_calls.append((provider.name, payload))
        outcome = self._take(payload["model"])
        if isinstance(outcome, dict):
            return outcome
        return self._default_response(payload["model"], outcome)

    async def achat_messages(self, provider, payload):
        self.chat_calls.append((provider.name, payload))
        outcome = self._take(payload["model"])
        if isinstance(outcome, dict):
            return outcome
        return self._default_response(payload["model"], outcome)

    def chat_stream_messages(self, provider, payload):
        self.chat_calls.append((provider.name, payload))
        outcome = self._take(payload["model"])
        if isinstance(outcome, dict):
            yield outcome
        else:
            yield self._chunk(payload["model"], outcome)

    async def achat_stream_messages(self, provider, payload):
        self.chat_calls.append((provider.name, payload))
        outcome = self._take(payload["model"])
        if isinstance(outcome, dict):
            yield outcome
        else:
            yield self._chunk(payload["model"], outcome)

    def probe_model(self, provider, model):
        self.probe_calls.append((provider.name, model))
        probe = self._probes.get(model)
        if probe is None:
            return ModelProbe(False, 0, 404, "missing probe")
        return probe


def make_client(holder, name, outcomes_by_model=None, probes=None):
    client = FakeClient()
    for model, outcomes in (outcomes_by_model or {}).items():
        client.set_outcomes(model, outcomes)
    for model, probe in (probes or {}).items():
        client.set_probe(model, probe)
    holder[name] = client
    return client


def _register(relay, holder, name, models, outcomes, probes=None):
    """Register a provider and its fake client; returns the fake client."""
    relay.provider_manager.register(make_provider(name, models))
    return make_client(holder, name, outcomes_by_model=outcomes, probes=probes)


def _wire_relay(monkeypatch, relay):
    monkeypatch.setattr(app.api.chat, "relay", relay)
    monkeypatch.setattr(app.api.openai, "relay", relay)
    monkeypatch.setattr(app.api.diagnostics, "relay", relay)
    monkeypatch.setattr(app.api.decision, "relay", relay)
    monkeypatch.setattr(app.api.health, "relay", relay)
    monkeypatch.setattr(app.api.providers, "relay", relay)


def _build_continuity_relay(
    monkeypatch, fake_registry, tmp_path, *, max_switches_per_turn=None
):
    """Build and wire a Relay with the continuity layer enabled."""
    monkeypatch.setattr(settings, "continuity_enabled", True)
    monkeypatch.setattr(settings, "continuity_retention_days", 0)
    monkeypatch.setattr(settings, "continuity_flush_interval_seconds", 60)
    monkeypatch.setattr(settings, "persistence_path", str(tmp_path / "platform.db"))
    if max_switches_per_turn is not None:
        monkeypatch.setattr(settings, "max_switches_per_turn", max_switches_per_turn)

    relay = Relay()
    _wire_relay(monkeypatch, relay)
    return relay


@pytest.fixture(autouse=True)
def reset_state():
    relay_metrics.reset()
    _reset_key_store()
    yield
    relay_metrics.reset()
    _reset_key_store()


@pytest.fixture
def fake_registry(monkeypatch):
    from app.services import client_registry

    holder = {}

    def fake_get(self, provider_name):
        return holder[provider_name]

    monkeypatch.setattr(client_registry.ClientRegistry, "get", fake_get)
    return holder


@pytest.fixture
def store(tmp_path):
    instance = KeyStore(tmp_path / "relay_keys.db")
    yield instance
    instance.close()


@pytest.fixture
def store_auth(monkeypatch, store):
    monkeypatch.setattr("app.security.auth._key_store", lambda: store)
    monkeypatch.setattr(settings, "relay_api_key", "")
    monkeypatch.setattr(settings, "relay_auth_store", True)
    return store


@pytest.fixture
def continuity_relay(monkeypatch, fake_registry, tmp_path):
    return _build_continuity_relay(monkeypatch, fake_registry, tmp_path)


@pytest.fixture
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


def _create_key(store):
    return store.create("test")


def _flush(relay):
    relay.continuity_flusher.flush()


class TestChatContinuity:
    def test_echoes_conversation_header_and_persists(
        self, continuity_relay, fake_registry, store_auth, client
    ):
        _register(
            continuity_relay,
            fake_registry,
            "A",
            ["a-1"],
            {"a-1": ["hello world"]},
        )
        key_id, raw_key = _create_key(store_auth)
        cid = "a" * 32

        response = client.post(
            "/chat",
            headers={
                "Authorization": f"Bearer {raw_key}",
                "X-Relay-Conversation-Id": cid,
                "X-Relay-Project-Id": "proj-1",
            },
            json={"message": "hello"},
        )

        assert response.status_code == 200
        assert response.headers.get(_CID_HEADER) == cid

        _flush(continuity_relay)
        row = continuity_relay.conversation_store.get(cid, key_id=key_id)
        assert row is not None
        assert row["project_key"] == derive_project_key(key_id, "proj-1")

    def test_project_only_gets_server_issued_id(
        self, continuity_relay, fake_registry, store_auth, client
    ):
        _register(
            continuity_relay,
            fake_registry,
            "A",
            ["a-1"],
            {"a-1": ["hello world"]},
        )
        key_id, raw_key = _create_key(store_auth)

        response = client.post(
            "/chat",
            headers={
                "Authorization": f"Bearer {raw_key}",
                "X-Relay-Project-Id": "proj-1",
            },
            json={"message": "hello"},
        )

        assert response.status_code == 200
        cid = response.headers.get(_CID_HEADER)
        assert cid is not None
        assert len(cid) == 32

        _flush(continuity_relay)
        row = continuity_relay.conversation_store.get(cid, key_id=key_id)
        assert row is not None

    def test_malformed_conversation_header_generic_400(
        self, continuity_relay, fake_registry, store_auth, client
    ):
        _register(
            continuity_relay,
            fake_registry,
            "A",
            ["a-1"],
            {"a-1": ["hello world"]},
        )
        _, raw_key = _create_key(store_auth)

        response = client.post(
            "/chat",
            headers={
                "Authorization": f"Bearer {raw_key}",
                "X-Relay-Conversation-Id": "x" * 200,
            },
            json={"message": "hello"},
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid relay continuity header."

    def test_no_headers_means_no_continuity(
        self, continuity_relay, fake_registry, store_auth, client
    ):
        _register(
            continuity_relay,
            fake_registry,
            "A",
            ["a-1"],
            {"a-1": ["hello world"]},
        )
        _, raw_key = _create_key(store_auth)

        response = client.post(
            "/chat",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={"message": "hello"},
        )

        assert response.status_code == 200
        assert response.headers.get(_CID_HEADER) is None

        _flush(continuity_relay)
        stats = continuity_relay.continuity_flusher.flush_stats()
        assert stats["drained_total"] == 0

    def test_bootstrap_key_gets_no_continuity(
        self, continuity_relay, fake_registry, client, monkeypatch
    ):
        _register(
            continuity_relay,
            fake_registry,
            "A",
            ["a-1"],
            {"a-1": ["hello world"]},
        )
        monkeypatch.setattr(settings, "relay_api_key", "bootstrap-secret")
        monkeypatch.setattr(settings, "relay_auth_store", False)

        response = client.post(
            "/chat",
            headers={
                "Authorization": "Bearer bootstrap-secret",
                "X-Relay-Conversation-Id": "b" * 32,
                "X-Relay-Project-Id": "proj-1",
            },
            json={"message": "hello"},
        )

        assert response.status_code == 200
        assert response.headers.get(_CID_HEADER) is None


class TestOpenAIContinuity:
    def test_non_stream_injects_envelope_on_resume(
        self, continuity_relay, fake_registry, store_auth, client
    ):
        fake = _register(
            continuity_relay,
            fake_registry,
            "A",
            ["a-1"],
            {"a-1": ["first", "second"]},
        )
        _, raw_key = _create_key(store_auth)
        cid = "c" * 32
        headers = {
            "Authorization": f"Bearer {raw_key}",
            "X-Relay-Conversation-Id": cid,
            "X-Relay-Project-Id": "proj-1",
        }
        payload = {"model": "a-1", "messages": [{"role": "user", "content": "hi"}]}

        first = client.post("/v1/chat/completions", headers=headers, json=payload)
        assert first.status_code == 200
        assert first.headers.get(_CID_HEADER) == cid

        second = client.post("/v1/chat/completions", headers=headers, json=payload)
        assert second.status_code == 200
        assert second.headers.get(_CID_HEADER) == cid

        sent = [call for call in fake.chat_calls if isinstance(call[1], dict)]
        assert len(sent) == 2
        # Fresh conversation: the first request carried the verbatim payload.
        assert sent[0][1]["messages"] == payload["messages"]
        # Resume: the second request carried the continuity envelope as a
        # leading synthetic system message.
        messages = sent[1][1]["messages"]
        assert messages[0]["role"] == "system"
        assert "[continuity context]" in messages[0]["content"]

    def test_stream_emits_relay_sse_events(
        self, continuity_relay, fake_registry, store_auth, client
    ):
        _register(
            continuity_relay,
            fake_registry,
            "A",
            ["a-1"],
            {"a-1": ["hello stream"]},
        )
        _, raw_key = _create_key(store_auth)
        cid = "d" * 32

        response = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {raw_key}",
                "X-Relay-Conversation-Id": cid,
                "X-Relay-Project-Id": "proj-1",
            },
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        assert response.headers.get(_CID_HEADER) == cid
        body = response.text
        assert "event: relay:conversation" in body
        assert f'"conversation_id": "{cid}"' in body
        assert "data: [DONE]" in body

    def test_stream_failover_emits_model_switched(
        self, continuity_relay, fake_registry, store_auth, client
    ):
        _register(
            continuity_relay,
            fake_registry,
            "A",
            ["m1"],
            {"m1": [ProviderError("boom")]},
        )
        _register(
            continuity_relay,
            fake_registry,
            "B",
            ["m1"],
            {"m1": ["recovered"]},
        )
        _, raw_key = _create_key(store_auth)
        cid = "e" * 32

        response = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {raw_key}",
                "X-Relay-Conversation-Id": cid,
                "X-Relay-Project-Id": "proj-1",
            },
            json={
                "model": "m1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        assert response.headers.get(_CID_HEADER) == cid
        body = response.text
        assert "event: relay:conversation" in body
        assert "event: relay:model_switched" in body
        assert '"from_provider": "A"' in body
        assert '"to_provider": "B"' in body
        assert "data: [DONE]" in body


class TestSwitchCaps:
    def test_cap_stops_failover_with_502(
        self, monkeypatch, fake_registry, store_auth, client, tmp_path
    ):
        relay = _build_continuity_relay(
            monkeypatch, fake_registry, tmp_path, max_switches_per_turn=1
        )
        for name in ("A", "B", "C"):
            _register(
                relay,
                fake_registry,
                name,
                ["m1"],
                {"m1": [ProviderError("boom")]},
            )
        _, raw_key = _create_key(store_auth)
        cid = "f" * 32

        response = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {raw_key}",
                "X-Relay-Conversation-Id": cid,
                "X-Relay-Project-Id": "proj-1",
            },
            json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert response.status_code == 502
        assert response.headers.get(_CID_HEADER) == cid
        assert relay_metrics.continuity_denials.value() == 1


class TestFlagOffParity:
    def test_disabled_ignores_continuity_headers(
        self, monkeypatch, fake_registry, client
    ):
        relay = Relay()
        _wire_relay(monkeypatch, relay)
        _register(
            relay,
            fake_registry,
            "A",
            ["a-1"],
            {"a-1": ["hello world"]},
        )
        monkeypatch.setattr(settings, "relay_api_key", "secret-token")
        monkeypatch.setattr(settings, "relay_auth_store", False)

        response = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer secret-token",
                "X-Relay-Conversation-Id": "g" * 32,
                "X-Relay-Project-Id": "proj-1",
            },
            json={"model": "a-1", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert response.status_code == 200
        assert response.headers.get(_CID_HEADER) is None
        assert relay.continuity_handoff is None
        assert relay.continuity_flusher is None
