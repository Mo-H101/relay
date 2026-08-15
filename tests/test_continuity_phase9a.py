"""
Phase 9A: cross-client continuity + restart-safe resume (HTTP verification).

End-to-end HTTP scenarios the Phase 9A report must evidence:

* Cross-client continuation: a request from one client (UA ``cline``) is
  served only after a provider failover, and a *different* client (UA
  ``opencode``) continues the same conversation using the resume token
  issued on the wire -- a single conversation, one contiguous durable
  sequence, and the continuity envelope injected on the resumed request.

* Restart-safe resume: a fresh Relay over the same platform.db resumes
  from the wire token without re-executing or duplicating acknowledged
  work; a wrong/stale token fails closed (chat still returns 200, the
  resume is denied, and the sequence is neither reset nor advanced by
  the token attempt itself).

* Privacy / memory contract: raw prompt/response content and raw resume
  tokens are never persisted (only SHA-256 hashes are), and the opt-in
  content context stays ephemeral (forwarded payload only).

* Targeted regressions on this path: bootstrap keys, header-less
  requests, flag-off parity, switch caps, and literal-model passthrough
  plus the Phase 8 actual-decision record.

Infrastructure mirrors tests/test_continuity_http.py: the provider
registry and auth are faked, the flusher is driven by explicit
``flush()`` calls (no background threads), and stores are file-backed in
``tmp_path``.
"""

import json
import sqlite3

import pytest

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.relay import Relay
from app.main import app as fastapi_app
from app.providers.base import ModelProbe, Provider
from app.providers.exceptions import ProviderError
from app.services.continuity_headers import (
    derive_project_key,
    derive_resume_token_hash,
)
from app.services.memory_contract import contains_never_captured

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
_RESUME_HEADER = "X-Relay-Resume-Token"

_P9A_CONTENT_MARKERS = (
    "P9A_SECRET_PROMPT_MARKER_7f3a",
    "P9A_SECRET_RESPONSE_MARKER_9c21",
    "P9A_SECRET_PROMPT_MARKER_1b2e",
    "P9A_SECRET_RESPONSE_MARKER_d4a8",
)


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


def _register(relay, holder, name, models, outcomes):
    """Register a provider and its fake client; returns the fake client."""
    relay.provider_manager.register(make_provider(name, models))
    client = FakeClient()
    for model, queue in outcomes.items():
        client.set_outcomes(model, queue)
    holder[name] = client
    return client


def _wire_relay(monkeypatch, relay):
    monkeypatch.setattr(app.api.chat, "relay", relay)
    monkeypatch.setattr(app.api.openai, "relay", relay)
    monkeypatch.setattr(app.api.diagnostics, "relay", relay)
    monkeypatch.setattr(app.api.decision, "relay", relay)
    monkeypatch.setattr(app.api.health, "relay", relay)
    monkeypatch.setattr(app.api.providers, "relay", relay)


def _build_continuity_relay(monkeypatch, fake_registry, tmp_path):
    """Build and wire a Relay with the continuity layer enabled."""
    monkeypatch.setattr(settings, "continuity_enabled", True)
    monkeypatch.setattr(settings, "continuity_retention_days", 0)
    monkeypatch.setattr(settings, "continuity_flush_interval_seconds", 60)
    monkeypatch.setattr(settings, "persistence_path", str(tmp_path / "platform.db"))

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
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


def _create_key(store):
    return store.create("test")


def _flush(relay):
    relay.continuity_flusher.flush()


def _post(
    client,
    *,
    raw_key,
    cid,
    project,
    ua,
    prompt,
    model="m1",
    stream=False,
    resume=None,
):
    """One /v1/chat/completions request with continuity headers + a UA."""
    headers = {
        "Authorization": f"Bearer {raw_key}",
        _CID_HEADER: cid,
        "X-Relay-Project-Id": project,
        "User-Agent": ua,
    }
    if resume:
        headers[_RESUME_HEADER] = resume
    return client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": stream,
        },
    )


def _payload_calls(fake):
    """The message-style calls that carry a forwardable payload dict."""
    return [call for call in fake.chat_calls if isinstance(call[1], dict)]


def _dump_store_text(path):
    """Concatenate every row of every table in the platform.db."""
    conn = sqlite3.connect(path)
    try:
        lines = []
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            " ORDER BY name"
        ):
            lines.append(f"== {table} ==")
            for row in conn.execute(f'SELECT * FROM "{table}"'):
                lines.append(repr(row))
        return "\n".join(lines)
    finally:
        conn.close()


def _failover_pair(relay, holder):
    """A failing provider A plus a working provider B (both host m1)."""
    fake_a = _register(
        relay,
        holder,
        "A",
        ["m1"],
        {"m1": [ProviderError("boom"), ProviderError("boom")]},
    )
    fake_b = _register(
        relay,
        holder,
        "B",
        ["m1"],
        {"m1": ["ok-B-1", "ok-B-2", "ok-B-3"]},
    )
    return fake_a, fake_b


class TestCrossClientFailover:
    def test_failover_then_cross_client_continues_same_conversation(
        self, monkeypatch, fake_registry, store_auth, client, tmp_path
    ):
        relay = _build_continuity_relay(monkeypatch, fake_registry, tmp_path)
        fake_a, fake_b = _failover_pair(relay, fake_registry)
        key_id, raw_key = _create_key(store_auth)
        cid = "1" * 32
        project = "proj-x"

        # Client 1 ("cline"): the model fails on A and is served by B.
        resp1 = _post(client, raw_key=raw_key, cid=cid, project=project,
                      ua="cline/1.2.3", prompt="first request")
        assert resp1.status_code == 200
        assert resp1.headers[_CID_HEADER] == cid
        raw1 = resp1.headers[_RESUME_HEADER]
        assert raw1

        # The request executed on B after failing on A.
        assert fake_a.chat_calls and fake_b.chat_calls
        assert fake_b.chat_calls[-1][0] == "B"
        # Fresh conversation: the first forwarded payload is verbatim.
        sent1 = _payload_calls(fake_b)
        assert len(sent1) == 1
        assert sent1[0][1]["model"] == "m1"
        assert sent1[0][1]["messages"] == [
            {"role": "user", "content": "first request"}
        ]

        _flush(relay)
        assert relay.conversation_store.turn_seqs(cid, key_id) == [1]

        # Client 2 ("opencode"): continues the same conversation with the
        # resume token from the wire. Same key, same project, same cid.
        resp2 = _post(client, raw_key=raw_key, cid=cid, project=project,
                      ua="opencode/0.1.0", prompt="second request",
                      resume=raw1)
        assert resp2.status_code == 200
        assert resp2.headers[_CID_HEADER] == cid
        raw2 = resp2.headers[_RESUME_HEADER]
        assert raw2 and raw2 != raw1

        # Still executed on B; a fresh token was issued for this turn.
        sent2 = _payload_calls(fake_b)
        assert len(sent2) == 2
        assert sent2[-1][1]["model"] == "m1"

        # The resumed request carried the continuity envelope as a leading
        # synthetic system message referencing the same conversation.
        system_content = sent2[-1][1]["messages"][0]["content"]
        assert system_content.startswith("[continuity context]")
        assert f"conversation: {cid}" in system_content
        assert "models: m1" in system_content

        _flush(relay)
        rows = relay.conversation_store.turns(cid, key_id, limit=10)
        assert [r["seq"] for r in rows] == [1, 2]
        assert relay.conversation_store.turn_seqs(cid, key_id) == [1, 2]

        # A single conversation row, created by the cline client, with the
        # durable project key derived from key_id + project id.
        conv = relay.conversation_store.get(cid, key_id=key_id)
        assert conv is not None
        assert conv["client_bucket"] == "cline"
        assert conv["project_key"] == derive_project_key(key_id, project)

        # Derived project state: turn + switch counters and the model chain.
        state = relay.conversation_store.project_state(
            key_id, derive_project_key(key_id, project)
        )
        assert state is not None
        assert state["counters"]["turns"] == 2
        assert state["counters"]["switches"] == 1
        assert state["last_models"] == ["m1"]

        # Only SHA-256 hashes of the resume tokens are durable.
        assert [r["resume_token_hash"] for r in rows] == [
            derive_resume_token_hash(raw1),
            derive_resume_token_hash(raw2),
        ]

    def test_stream_failover_cross_client_emits_events(
        self, monkeypatch, fake_registry, store_auth, client, tmp_path
    ):
        relay = _build_continuity_relay(monkeypatch, fake_registry, tmp_path)
        fake_a, fake_b = _failover_pair(relay, fake_registry)
        key_id, raw_key = _create_key(store_auth)
        cid = "2" * 32

        resp1 = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                      ua="cline/1.2.3", prompt="hello", stream=True)
        assert resp1.status_code == 200
        assert resp1.headers[_CID_HEADER] == cid
        raw1 = resp1.headers[_RESUME_HEADER]
        assert raw1
        body1 = resp1.text
        assert "event: relay:conversation" in body1
        assert "event: relay:model_switched" in body1
        assert '"from_provider": "A"' in body1
        assert '"to_provider": "B"' in body1

        _flush(relay)
        assert relay.conversation_store.turn_seqs(cid, key_id) == [1]

        # A different client resumes the stream and sees the additive
        # conversation event again; the handoff decision is on the wire.
        resp2 = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                      ua="opencode/0.1.0", prompt="continue", stream=True,
                      resume=raw1)
        assert resp2.status_code == 200
        assert resp2.headers[_CID_HEADER] == cid
        raw2 = resp2.headers[_RESUME_HEADER]
        assert raw2 and raw2 != raw1
        assert "event: relay:conversation" in resp2.text

        _flush(relay)
        assert relay.conversation_store.turn_seqs(cid, key_id) == [1, 2]


class TestRestartSafeResume:
    def test_restart_resume_continues_without_re_execution(
        self, monkeypatch, fake_registry, store_auth, client, tmp_path
    ):
        # Process A: commits turn 1, then "dies".
        relay1 = _build_continuity_relay(monkeypatch, fake_registry, tmp_path)
        fake_a, fake_b = _failover_pair(relay1, fake_registry)
        key_id, raw_key = _create_key(store_auth)
        cid = "3" * 32

        resp1 = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                      ua="cline/1.2.3", prompt="before restart")
        assert resp1.status_code == 200
        raw1 = resp1.headers[_RESUME_HEADER]
        assert raw1

        _flush(relay1)
        assert relay1.conversation_store.turn_seqs(cid, key_id) == [1]
        relay1.conversation_store.close()

        # Process B: a fresh Relay over the same platform.db. No shared
        # in-memory state; everything must come from the durable store.
        relay2 = _build_continuity_relay(monkeypatch, fake_registry, tmp_path)
        fake_a2, fake_b2 = _failover_pair(relay2, fake_registry)

        resp2 = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                      ua="opencode/0.1.0", prompt="after restart",
                      resume=raw1)
        assert resp2.status_code == 200
        assert resp2.headers[_CID_HEADER] == cid
        raw2 = resp2.headers[_RESUME_HEADER]
        assert raw2 and raw2 != raw1

        # The restarted process served the resumed request exactly once,
        # on the failover provider; no duplicate execution of turn 1.
        assert fake_b2.chat_calls and fake_b2.chat_calls[-1][0] == "B"
        sent = _payload_calls(fake_b2)
        assert len(sent) == 1

        # The resumed turn carried the reconstructed continuity envelope.
        system_content = sent[0][1]["messages"][0]["content"]
        assert "[continuity context]" in system_content
        assert f"conversation: {cid}" in system_content

        _flush(relay2)
        # Contiguous sequence, no reset to 1, no duplicate work.
        assert relay2.conversation_store.turn_seqs(cid, key_id) == [1, 2]
        rows = relay2.conversation_store.turns(cid, key_id, limit=10)
        assert [r["seq"] for r in rows] == [1, 2]
        assert [r["resume_token_hash"] for r in rows] == [
            derive_resume_token_hash(raw1),
            derive_resume_token_hash(raw2),
        ]

        # The pre-restart token is now stale: the commit replaced it, so it
        # can never resume the conversation again (token_mismatch, at the
        # acknowledged boundary of 2).
        denied = relay2.continuity_recovery.validate_resume(cid, key_id, raw1)
        assert denied["valid"] is False
        assert denied["reason"] == "token_mismatch"
        assert denied["last_seq"] == 2

    def test_wrong_token_fails_closed_without_reset_or_replay(
        self, monkeypatch, fake_registry, store_auth, client, tmp_path
    ):
        # Process A: commits turn 1.
        relay1 = _build_continuity_relay(monkeypatch, fake_registry, tmp_path)
        fake_a, fake_b = _failover_pair(relay1, fake_registry)
        key_id, raw_key = _create_key(store_auth)
        cid = "4" * 32

        resp1 = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                      ua="cline/1.2.3", prompt="one")
        assert resp1.status_code == 200
        raw1 = resp1.headers[_RESUME_HEADER]
        assert raw1
        _flush(relay1)
        relay1.conversation_store.close()

        # Process B: a wrong token must fail closed.
        relay2 = _build_continuity_relay(monkeypatch, fake_registry, tmp_path)
        fake_a2, fake_b2 = _failover_pair(relay2, fake_registry)
        wrong = "not-the-resume-token"

        # The denial carries the acknowledged boundary and advances nothing.
        denied = relay2.continuity_recovery.validate_resume(
            cid, key_id, wrong
        )
        assert denied["valid"] is False
        assert denied["reason"] == "token_mismatch"
        assert denied["last_seq"] == 1
        # A mismatched token never records a durable replay attempt.
        assert (
            relay2.conversation_store.resume_replay_attempts(
                cid, key_id, derive_resume_token_hash(wrong)
            )
            == 0
        )

        # Chat still succeeds; the wrong token never breaks the request.
        resp2 = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                      ua="opencode/0.1.0", prompt="two", resume=wrong)
        assert resp2.status_code == 200
        raw2 = resp2.headers[_RESUME_HEADER]
        assert raw2
        _flush(relay2)

        # The turn continued at last_seq + 1; no re-execution, no reset.
        assert relay2.conversation_store.turn_seqs(cid, key_id) == [1, 2]

        # The correct current token resumes cleanly at 3 afterwards.
        resp3 = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                      ua="opencode/0.1.0", prompt="three", resume=raw2)
        assert resp3.status_code == 200
        raw3 = resp3.headers[_RESUME_HEADER]
        assert raw3 and raw3 != raw2
        _flush(relay2)
        assert relay2.conversation_store.turn_seqs(cid, key_id) == [1, 2, 3]
        assert relay2.conversation_store.turn_seqs(cid, key_id) == [1, 2, 3]


class TestPrivacyMemoryContract:
    def test_flow_never_persists_content_or_raw_tokens(
        self, monkeypatch, fake_registry, store_auth, client, tmp_path
    ):
        relay = _build_continuity_relay(monkeypatch, fake_registry, tmp_path)
        fake_a, fake_b = _failover_pair(relay, fake_registry)
        fake_b.set_outcomes(
            "m1",
            [
                _P9A_CONTENT_MARKERS[1],
                _P9A_CONTENT_MARKERS[3],
            ],
        )
        key_id, raw_key = _create_key(store_auth)
        cid = "5" * 32

        resp1 = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                      ua="cline/1.2.3", prompt=_P9A_CONTENT_MARKERS[0])
        assert resp1.status_code == 200
        raw1 = resp1.headers[_RESUME_HEADER]
        _flush(relay)

        resp2 = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                      ua="opencode/0.1.0", prompt=_P9A_CONTENT_MARKERS[2],
                      resume=raw1)
        assert resp2.status_code == 200
        raw2 = resp2.headers[_RESUME_HEADER]
        _flush(relay)

        dump = _dump_store_text(relay.conversation_store.path)

        # Raw prompt/response content is never persisted anywhere.
        for marker in _P9A_CONTENT_MARKERS:
            assert marker not in dump, marker

        # Raw resume tokens are never persisted; only their hashes are.
        assert raw1 not in dump and raw2 not in dump
        assert derive_resume_token_hash(raw1) in dump
        assert derive_resume_token_hash(raw2) in dump

        # The metadata-only export surfaces carry no content-shaped keys.
        exports = {
            "conversation": relay.conversation_store.get(cid, key_id=key_id),
            "turns": relay.conversation_store.turns(cid, key_id, limit=10),
            "project_state": relay.conversation_store.project_state(
                key_id, derive_project_key(key_id, "proj-x")
            ),
            "summaries": relay.conversation_store.summaries(
                cid, key_id, limit=10
            ),
            "compactions": relay.conversation_store.compactions(
                cid, key_id, limit=10
            ),
            "counts": relay.conversation_store.counts(key_id),
        }
        assert contains_never_captured(exports) is False

    def test_content_context_is_ephemeral_when_enabled(
        self, monkeypatch, fake_registry, store_auth, client, tmp_path
    ):
        monkeypatch.setattr(settings, "continuity_content_context_enabled", True)
        relay = _build_continuity_relay(monkeypatch, fake_registry, tmp_path)
        fake = _register(
            relay,
            fake_registry,
            "A",
            ["a-1"],
            {"a-1": ["first", "second"]},
        )
        key_id, raw_key = _create_key(store_auth)
        cid = "6" * 32
        prompt_one = "P9A_EPHEMERAL_ONE"
        prompt_two = "P9A_EPHEMERAL_TWO"

        first = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                      ua="cline/1.2.3", prompt=prompt_one, model="a-1")
        assert first.status_code == 200
        _flush(relay)

        second = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                       ua="opencode/0.1.0", prompt=prompt_two, model="a-1",
                       resume=first.headers[_RESUME_HEADER])
        assert second.status_code == 200

        # The bounded content digest lives in the forwarded payload of the
        # current request only -- never in the store.
        sent = _payload_calls(fake)
        assert len(sent) == 2
        assert sent[0][1]["messages"] == [
            {"role": "user", "content": prompt_one}
        ]
        system_content = sent[1][1]["messages"][0]["content"]
        assert "[continuity context]" in system_content
        assert f"first user request: {prompt_two}" in system_content

        _flush(relay)
        dump = _dump_store_text(relay.conversation_store.path)
        assert prompt_one not in dump
        assert prompt_two not in dump


class TestPhase9ARegression:
    def test_bootstrap_key_gets_no_continuity(
        self, monkeypatch, fake_registry, client, tmp_path
    ):
        relay = _build_continuity_relay(monkeypatch, fake_registry, tmp_path)
        _register(relay, fake_registry, "A", ["a-1"], {"a-1": ["hello world"]})
        monkeypatch.setattr(settings, "relay_api_key", "bootstrap-secret")
        monkeypatch.setattr(settings, "relay_auth_store", False)

        response = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer bootstrap-secret",
                _CID_HEADER: "b" * 32,
                "X-Relay-Project-Id": "proj-1",
            },
            json={"model": "a-1", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        assert response.headers.get(_CID_HEADER) is None
        assert response.headers.get(_RESUME_HEADER) is None
        _flush(relay)
        assert relay.conversation_store.counts()["conversations"] == 0

    def test_no_continuity_headers_unchanged(
        self, monkeypatch, fake_registry, store_auth, client, tmp_path
    ):
        relay = _build_continuity_relay(monkeypatch, fake_registry, tmp_path)
        _register(relay, fake_registry, "A", ["a-1"], {"a-1": ["hello world"]})
        key_id, raw_key = _create_key(store_auth)

        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={"model": "a-1", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        assert response.headers.get(_CID_HEADER) is None
        assert response.headers.get(_RESUME_HEADER) is None
        _flush(relay)
        assert relay.conversation_store.counts()["conversations"] == 0

    def test_continuity_disabled_parity(
        self, monkeypatch, fake_registry, client, tmp_path
    ):
        relay = Relay()
        _wire_relay(monkeypatch, relay)
        _register(relay, fake_registry, "A", ["a-1"], {"a-1": ["hello world"]})
        monkeypatch.setattr(settings, "relay_api_key", "secret-token")
        monkeypatch.setattr(settings, "relay_auth_store", False)

        response = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer secret-token",
                _CID_HEADER: "g" * 32,
                "X-Relay-Project-Id": "proj-1",
            },
            json={"model": "a-1", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        assert response.headers.get(_CID_HEADER) is None
        assert relay.continuity_handoff is None

    def test_literal_model_passthrough_and_decision_record(
        self, monkeypatch, fake_registry, store_auth, client, tmp_path
    ):
        relay = _build_continuity_relay(monkeypatch, fake_registry, tmp_path)
        fake = _register(relay, fake_registry, "A", ["a-1"], {"a-1": ["hello world"]})
        key_id, raw_key = _create_key(store_auth)
        cid = "7" * 32

        response = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                         ua="opencode/0.1.0", prompt="hi", model="a-1")
        assert response.status_code == 200
        assert response.headers[_CID_HEADER] == cid

        # Verbatim passthrough: the provider saw the exact literal model id.
        sent = _payload_calls(fake)
        assert sent[-1][1]["model"] == "a-1"

        # Phase 8 observability still fires on the continuity path.
        record = relay.decision_record_store.most_recent()
        assert record is not None
        assert record.requested_model == "a-1"
        assert record.routed is False
        assert record.selected_provider == "A"
        assert record.selected_model == "a-1"
        assert record.outcome == "succeeded"
