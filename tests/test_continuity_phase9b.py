"""
Phase 9B: continuation anchor + cross-turn model transitions.

Unit + end-to-end verification the Phase 9B report must evidence:

* Anchor resolution: ``Relay.anchor_for`` returns the conversation's last
  committed logical ``(provider, model)`` -- in-memory committed view
  first, durable state as the restart/cross-process fallback -- and is
  key-scoped (a conversation id under another key never yields another
  key's anchor).

* Anchor tiering: ``CandidateBuilder.build(..., anchor=...)`` puts the
  anchor model tier first (fallback tier second), with no anchor it is
  byte-identical to the pre-Phase 9B plan, and an anchor model no
  provider can execute yields an empty anchor tier that falls through to
  the fallback tier. The anchor tier spans every provider that hosts the
  anchor model; no candidate is ever duplicated across tiers.

* Virtual names: an omitted model or a virtual model ("auto", "default",
  "relay") routes through Relay's candidate machinery and is therefore
  anchored on resume, while explicit literal models keep passthrough
  behavior.

* Transition classification: ``HandoffCoordinator.record_transition``
  records exactly one ``relay:model_switched`` event with
  ``switch_count=0`` when the resolved plan's first model differs from
  the anchor -- ``reason="selection"`` for an explicit literal model
  request, ``reason="failover"`` for Relay-initiated routing -- and no
  event when the model is unchanged (provider movement within a logical
  model stays with the execution-time ``on_switch``).

* Wire parity: the annotated event is emitted once on the stream, is
  never duplicated by a within-turn ``on_switch``, a fresh conversation
  emits none, a selection followed by an execution-time failover emits
  both events in order, and an anchor unavailable everywhere at routing
  time falls through to the fallback tier with a ``reason="failover"``
  transition. The durable per-turn model chain survives a relay restart.

Infrastructure mirrors tests/test_continuity_phase9a.py: the provider
registry and auth are faked, the flusher is driven by explicit
``flush()`` calls, and stores are file-backed in ``tmp_path``.
"""

import pytest

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.relay import Relay
from app.main import app as fastapi_app
from app.providers.base import ModelProbe, Provider
from app.providers.exceptions import ProviderError
from app.services.candidate_builder import CandidateBuilder
from app.services.handoff import TurnContext
from app.services.health_checker import HEALTHY, UNAVAILABLE, ProviderHealth
from app.services.health_store import HealthStore

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
    """Deterministic client for chat + probe flows, mirroring P9A."""

    def __init__(self):
        self.chat_calls = []
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


def _build_continuity_relay(monkeypatch, tmp_path):
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


def _post_chat(client, *, raw_key, cid, project, ua, message, task=None, resume=None):
    """One /chat request with continuity headers + a UA."""
    headers = {
        "Authorization": f"Bearer {raw_key}",
        _CID_HEADER: cid,
        "X-Relay-Project-Id": project,
        "User-Agent": ua,
    }
    if resume:
        headers[_RESUME_HEADER] = resume
    body = {"message": message}
    if task:
        body["task"] = task
    return client.post("/chat", headers=headers, json=body)


def _payload_calls(fake):
    """The message-style calls that carry a forwardable payload dict."""
    return [call for call in fake.chat_calls if isinstance(call[1], dict)]


def _scope(key_id, cid):
    """A resolved continuity scope for direct handoff/anchor tests."""
    return {
        "conversation_id": cid,
        "key_id": key_id,
        "client_bucket": "opencode",
        "project_key": "proj-x",
    }


# ---------------------------------------------------------------------------
# Relay.anchor_for: resolution, precedence, durability, key scoping
# ---------------------------------------------------------------------------


class TestAnchorResolution:
    def test_no_scope_or_continuity_off_returns_none(
        self, monkeypatch, tmp_path
    ):
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        assert relay.anchor_for(None) is None
        assert relay.anchor_for({}) is None

        monkeypatch.setattr(settings, "continuity_enabled", False)
        bare = Relay()
        assert bare.anchor_for(_scope("key-1", "c" * 32)) is None

    def test_unknown_conversation_returns_none(
        self, monkeypatch, tmp_path
    ):
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        scope = _scope("key-1", "c" * 32)
        # No in-memory state and no durable turn: no anchor.
        assert relay.anchor_for(scope) is None

    def test_in_memory_committed_view_wins(
        self, monkeypatch, tmp_path
    ):
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        scope = _scope("key-1", "c" * 32)
        turn = relay.continuity_handoff.start(
            key_id=scope["key_id"],
            client_bucket=scope["client_bucket"],
            project_key=scope["project_key"],
            conversation_id=scope["conversation_id"],
        )
        turn.finish(provider="A", model="m2")

        anchor = relay.anchor_for(scope)
        assert anchor == {"provider": "A", "model": "m2"}

    def test_durable_fallback_after_restart(
        self, monkeypatch, tmp_path
    ):
        relay1 = _build_continuity_relay(monkeypatch, tmp_path)
        scope = _scope("key-1", "c" * 32)
        turn = relay1.continuity_handoff.start(
            key_id=scope["key_id"],
            client_bucket=scope["client_bucket"],
            project_key=scope["project_key"],
            conversation_id=scope["conversation_id"],
        )
        turn.finish(provider="B", model="m3")
        _flush(relay1)
        relay1.conversation_store.close()

        relay2 = _build_continuity_relay(monkeypatch, tmp_path)
        assert relay2.anchor_for(scope) == {"provider": "B", "model": "m3"}

    def test_key_scoped_never_leaks(
        self, monkeypatch, tmp_path
    ):
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        scope = _scope("key-1", "c" * 32)
        turn = relay.continuity_handoff.start(
            key_id=scope["key_id"],
            client_bucket=scope["client_bucket"],
            project_key=scope["project_key"],
            conversation_id=scope["conversation_id"],
        )
        turn.finish(provider="A", model="m2")
        _flush(relay)

        # Same conversation id under a different key must see no anchor.
        assert relay.anchor_for(_scope("key-2", scope["conversation_id"])) is None

    def test_anchor_reflects_latest_committed_turn(
        self, monkeypatch, tmp_path
    ):
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        scope = _scope("key-1", "c" * 32)
        relay.continuity_handoff.start(
            key_id=scope["key_id"],
            client_bucket=scope["client_bucket"],
            project_key=scope["project_key"],
            conversation_id=scope["conversation_id"],
        ).finish(provider="A", model="m2")
        relay.continuity_handoff.start(
            key_id=scope["key_id"],
            client_bucket=scope["client_bucket"],
            project_key=scope["project_key"],
            conversation_id=scope["conversation_id"],
        ).finish(provider="B", model="m3")

        assert relay.anchor_for(scope) == {"provider": "B", "model": "m3"}


class TestLastCommitted:
    def test_returns_last_committed_turn(
        self, monkeypatch, tmp_path
    ):
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        scope = _scope("key-1", "c" * 32)
        relay.continuity_handoff.start(
            key_id=scope["key_id"],
            client_bucket=scope["client_bucket"],
            project_key=scope["project_key"],
            conversation_id=scope["conversation_id"],
        ).finish(provider="A", model="m2")

        last = relay.continuity_handoff.last_committed(
            scope["key_id"], scope["conversation_id"]
        )
        assert last == {"provider": "A", "model": "m2", "seq": 1}

    def test_key_scoped(self, monkeypatch, tmp_path):
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        scope = _scope("key-1", "c" * 32)
        relay.continuity_handoff.start(
            key_id=scope["key_id"],
            client_bucket=scope["client_bucket"],
            project_key=scope["project_key"],
            conversation_id=scope["conversation_id"],
        ).finish(provider="A", model="m2")

        assert (
            relay.continuity_handoff.last_committed(
                "key-2", scope["conversation_id"]
            )
            is None
        )


# ---------------------------------------------------------------------------
# record_transition: classification and event shape
# ---------------------------------------------------------------------------


class TestRecordTransition:
    def test_no_anchor_or_no_candidates_is_noop(self, monkeypatch, tmp_path):
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        turn = TurnContext(
            conversation_id="c" * 32,
            key_id="key-1",
            client_bucket="opencode",
            project_key="proj-x",
        )
        assert (
            relay.continuity_handoff.record_transition(
                turn, anchor=None, routed=True, candidates=[(object(), "m1")]
            )
            is None
        )
        assert (
            relay.continuity_handoff.record_transition(
                turn, anchor={"provider": "A", "model": "m2"}, routed=True, candidates=[]
            )
            is None
        )
        assert turn.events == []

    def test_same_model_is_noop(self, monkeypatch, tmp_path):
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        turn = TurnContext(
            conversation_id="c" * 32,
            key_id="key-1",
            client_bucket="opencode",
            project_key="proj-x",
        )
        provider = make_provider("A", ["m2"])
        assert (
            relay.continuity_handoff.record_transition(
                turn,
                anchor={"provider": "B", "model": "m2"},
                routed=True,
                candidates=[(provider, "m2")],
            )
            is None
        )
        assert turn.events == []

    def test_literal_selection_is_reason_selection(self, monkeypatch, tmp_path):
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        turn = TurnContext(
            conversation_id="c" * 32,
            key_id="key-1",
            client_bucket="opencode",
            project_key="proj-x",
        )
        provider = make_provider("A", ["m1"])
        event = relay.continuity_handoff.record_transition(
            turn,
            anchor={"provider": "B", "model": "m2"},
            routed=False,
            candidates=[(provider, "m1")],
        )
        assert event == {
            "type": "relay:model_switched",
            "conversation_id": turn.conversation_id,
            "from_provider": "B",
            "from_model": "m2",
            "to_provider": "A",
            "to_model": "m1",
            "reason": "selection",
            "switch_count": 0,
        }
        assert turn.events == [event]

    def test_relay_fallback_is_reason_failover(self, monkeypatch, tmp_path):
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        turn = TurnContext(
            conversation_id="c" * 32,
            key_id="key-1",
            client_bucket="opencode",
            project_key="proj-x",
        )
        provider = make_provider("A", ["m1"])
        event = relay.continuity_handoff.record_transition(
            turn,
            anchor={"provider": "B", "model": "m2"},
            routed=True,
            candidates=[(provider, "m1")],
        )
        assert event["reason"] == "failover"
        assert event["switch_count"] == 0
        assert turn.events == [event]


# ---------------------------------------------------------------------------
# CandidateBuilder anchor tiering
# ---------------------------------------------------------------------------


class _FakeSettings:
    def __init__(self, health_aware_routing=False):
        self.health_aware_routing = health_aware_routing


def _make_report(name, status, models):
    return ProviderHealth(
        name=name,
        status=status,
        latency_ms=5,
        last_checked="now",
        details="ok",
        connectivity=True,
        rate_limit_status="ok",
        last_successful_request=None,
        healthy_models=list(models) if status == HEALTHY else [],
        degraded_models=[],
        unavailable_models=list(models) if status == UNAVAILABLE else [],
        unsupported_models=[],
    )


class TestCandidateBuilderAnchorTiering:
    def test_anchor_tier_comes_first(self):
        # A hosts [m1, m2]; B hosts [m1]. Without an anchor the plan is
        # provider/model order, so m1 would be first.
        p_a = make_provider("A", ["m1", "m2"])
        p_b = make_provider("B", ["m1"])
        builder = CandidateBuilder(config=_FakeSettings(health_aware_routing=False))

        anchored = builder.build([p_a, p_b], anchor="m2")
        assert [(p.name, m) for p, m in anchored] == [
            ("A", "m2"),
            ("A", "m1"),
            ("B", "m1"),
        ]

    def test_no_anchor_is_unchanged_plan(self):
        p_a = make_provider("A", ["m1", "m2"])
        p_b = make_provider("B", ["m1"])
        builder = CandidateBuilder(config=_FakeSettings(health_aware_routing=False))

        assert [(p.name, m) for p, m in builder.build([p_a, p_b])] == [
            ("A", "m1"),
            ("A", "m2"),
            ("B", "m1"),
        ]

    def test_unhosted_anchor_falls_through_to_fallback(self):
        p_a = make_provider("A", ["m1"])
        p_b = make_provider("B", ["m1"])
        builder = CandidateBuilder(config=_FakeSettings(health_aware_routing=False))

        # Anchor "m2" is hosted by no provider: empty anchor tier, the plan
        # falls through to the fallback tier unchanged.
        assert [(p.name, m) for p, m in builder.build([p_a, p_b], anchor="m2")] == [
            ("A", "m1"),
            ("B", "m1"),
        ]

    def test_health_aware_unavailable_anchor_falls_through(self):
        # Anchor m2 lives only on A, which is UNAVAILABLE: the strict anchor
        # tier is empty, so the plan falls through to the fallback tier and
        # still serves B's m1.
        p_a = make_provider("A", ["m2"])
        p_b = make_provider("B", ["m1"])
        store = HealthStore()
        store.save(_make_report("A", UNAVAILABLE, ["m2"]))
        store.save(_make_report("B", HEALTHY, ["m1"]))
        builder = CandidateBuilder(
            health_store=store,
            config=_FakeSettings(health_aware_routing=True),
        )

        assert [(p.name, m) for p, m in builder.build([p_a, p_b], anchor="m2")] == [
            ("B", "m1"),
        ]

    def test_health_aware_anchor_tier_first_within_bands(self):
        # Both providers host m2 and are healthy; the anchor tier keeps both
        # m2 candidates first, then the fallback tier.
        p_a = make_provider("A", ["m2"])
        p_b = make_provider("B", ["m2", "m1"])
        store = HealthStore()
        store.save(_make_report("A", HEALTHY, ["m2"]))
        store.save(_make_report("B", HEALTHY, ["m2", "m1"]))
        builder = CandidateBuilder(
            health_store=store,
            config=_FakeSettings(health_aware_routing=True),
        )

        names = [(p.name, m) for p, m in builder.build([p_a, p_b], anchor="m2")]
        assert names[:2] == [("A", "m2"), ("B", "m2")]
        assert ("B", "m1") in names[2:]

    def test_anchor_tier_expands_across_providers(self):
        # Every provider carrying the anchor model joins the anchor tier,
        # ahead of unrelated fallback candidates on the same providers.
        p_a = make_provider("A", ["m1", "m2"])
        p_b = make_provider("B", ["m2", "m3"])
        p_c = make_provider("C", ["m1"])
        builder = CandidateBuilder(config=_FakeSettings(health_aware_routing=False))

        assert [(p.name, m) for p, m in builder.build([p_a, p_b, p_c], anchor="m2")] == [
            ("A", "m2"),
            ("B", "m2"),
            ("A", "m1"),
            ("B", "m3"),
            ("C", "m1"),
        ]

    def test_no_duplicate_candidates_across_tiers(self):
        # A model id listed more than once on a provider must never yield a
        # duplicate candidate in either tier (or the same candidate twice).
        p_a = make_provider("A", ["m1", "m2", "m2"])
        p_b = make_provider("B", ["m1", "m1", "m2"])
        builder = CandidateBuilder(config=_FakeSettings(health_aware_routing=False))

        names = [(p.name, m) for p, m in builder.build([p_a, p_b], anchor="m2")]
        assert len(names) == len(set(names))
        assert names == [
            ("A", "m2"),
            ("B", "m2"),
            ("A", "m1"),
            ("B", "m1"),
        ]

    def test_ranked_candidates_mirror_anchored_order(self):
        p_a = make_provider("A", ["m1", "m2"])
        p_b = make_provider("B", ["m1"])
        builder = CandidateBuilder(config=_FakeSettings(health_aware_routing=False))

        ranked = builder.ranked_candidates([p_a, p_b], anchor="m2")
        assert [(r.provider, r.model) for r in ranked] == [
            ("A", "m2"),
            ("A", "m1"),
            ("B", "m1"),
        ]

        rankables = builder.rankables([p_a, p_b], anchor="m2")
        assert [(r.provider, r.model) for r in rankables] == [
            ("A", "m2"),
            ("A", "m1"),
            ("B", "m1"),
        ]


# ---------------------------------------------------------------------------
# HTTP: a continued conversation stays on its anchor model
# ---------------------------------------------------------------------------


class TestAnchorTieringAtHttp:
    def test_resumed_conversation_stays_on_anchor_model(
        self, monkeypatch, fake_registry, store_auth, client, tmp_path
    ):
        # A hosts [m1, m2], B hosts [m1]. A bare no-model plan would serve
        # m1 first; the anchor must keep the resumed turn on m2.
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        fake_a = _register(
            relay,
            fake_registry,
            "A",
            ["m1", "m2"],
            {"m1": ["a-m1"], "m2": ["a-m2-1", "a-m2-2"]},
        )
        _register(
            relay,
            fake_registry,
            "B",
            ["m1"],
            {"m1": ["b-m1"]},
        )
        key_id, raw_key = _create_key(store_auth)
        cid = "1" * 32

        first = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                      ua="opencode/0.1.0", prompt="one", model="m2")
        assert first.status_code == 200
        raw1 = first.headers[_RESUME_HEADER]
        assert raw1
        _flush(relay)
        assert relay.conversation_store.turn_seqs(cid, key_id) == [1]

        # Anchor m2 is known to this process; the resumed no-model turn must
        # resolve to m2 first, not to A's m1.
        second = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                       ua="opencode/0.1.0", prompt="two", model="",
                       resume=raw1)
        assert second.status_code == 200

        sent = _payload_calls(fake_a)
        assert sent[-1][1]["model"] == "m2"

        # The actual-decision truth surface saw the anchored plan.
        record = relay.decision_record_store.most_recent()
        assert record is not None
        assert record.routed is True
        assert record.candidates[0].model == "m2"

        _flush(relay)
        assert relay.conversation_store.turn_seqs(cid, key_id) == [1, 2]

    def test_fresh_conversation_gets_no_anchor_tiering(
        self, monkeypatch, fake_registry, store_auth, client, tmp_path
    ):
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        fake_a = _register(
            relay,
            fake_registry,
            "A",
            ["m1", "m2"],
            {"m1": ["a-m1"], "m2": ["a-m2"]},
        )
        key_id, raw_key = _create_key(store_auth)
        cid = "2" * 32

        # First turn, no model: no anchor exists, so the plan is the
        # unchanged provider/model order (m1 first on A).
        response = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                         ua="opencode/0.1.0", prompt="first")
        assert response.status_code == 200
        sent = _payload_calls(fake_a)
        assert sent[-1][1]["model"] == "m1"

    def test_virtual_auto_model_stays_on_anchor(
        self, monkeypatch, fake_registry, store_auth, client, tmp_path
    ):
        # "auto" is a virtual name: it routes through Relay's candidate
        # machinery, so on resume it is anchored and keeps the conversation
        # on its last model (m2) instead of drifting to A:m1.
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        fake_a = _register(
            relay,
            fake_registry,
            "A",
            ["m1", "m2"],
            {"m1": ["a-m1"], "m2": ["a-m2"]},
        )
        _register(
            relay,
            fake_registry,
            "B",
            ["m1"],
            {"m1": ["b-m1"]},
        )
        key_id, raw_key = _create_key(store_auth)
        cid = "b" * 32

        first = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                      ua="opencode/0.1.0", prompt="one", model="m2")
        assert first.status_code == 200
        raw1 = first.headers[_RESUME_HEADER]
        _flush(relay)

        second = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                       ua="opencode/0.1.0", prompt="two", model="auto",
                       resume=raw1)
        assert second.status_code == 200
        sent = _payload_calls(fake_a)
        assert sent[-1][1]["model"] == "m2"


class TestAnchorSurvivesRestart:
    def test_restarted_relay_resumes_on_durable_anchor(
        self, monkeypatch, fake_registry, store_auth, client, tmp_path
    ):
        def register_pair(relay):
            fake_a = _register(
                relay,
                fake_registry,
                "A",
                ["m1", "m2"],
                {"m1": ["a-m1"], "m2": ["a-m2-1", "a-m2-2"]},
            )
            _register(
                relay,
                fake_registry,
                "B",
                ["m1"],
                {"m1": ["b-m1"]},
            )
            return fake_a

        relay1 = _build_continuity_relay(monkeypatch, tmp_path)
        register_pair(relay1)
        key_id, raw_key = _create_key(store_auth)
        cid = "3" * 32

        first = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                      ua="cline/1.2.3", prompt="one", model="m2")
        assert first.status_code == 200
        raw1 = first.headers[_RESUME_HEADER]
        _flush(relay1)
        assert relay1.conversation_store.turn_seqs(cid, key_id) == [1]
        relay1.conversation_store.close()

        # Fresh process over the same db: the anchor must come from the
        # durable store, so the resumed no-model turn stays on m2.
        relay2 = _build_continuity_relay(monkeypatch, tmp_path)
        fake_a2 = register_pair(relay2)

        second = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                       ua="opencode/0.1.0", prompt="two", model="",
                       resume=raw1)
        assert second.status_code == 200

        sent = _payload_calls(fake_a2)
        assert len(sent) == 1
        assert sent[0][1]["model"] == "m2"

        _flush(relay2)
        assert relay2.conversation_store.turn_seqs(cid, key_id) == [1, 2]

        # The durable per-turn chain survives the restart too: both turns
        # committed on the anchor model m2 under provider A.
        durable = relay2.conversation_store.turns(cid, key_id)
        assert [(t["seq"], t["provider"], t["model"]) for t in durable] == [
            (1, "A", "m2"),
            (2, "A", "m2"),
        ]


# ---------------------------------------------------------------------------
# HTTP: the annotated cross-turn transition on the wire
# ---------------------------------------------------------------------------


class TestTransitionEventsOnWire:
    def test_literal_model_change_emits_selection_transition(
        self, monkeypatch, fake_registry, store_auth, client, tmp_path
    ):
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        fake = _register(
            relay,
            fake_registry,
            "A",
            ["m1", "m2"],
            {"m1": ["a-m1"], "m2": ["a-m2"]},
        )
        key_id, raw_key = _create_key(store_auth)
        cid = "4" * 32

        first = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                      ua="opencode/0.1.0", prompt="one", model="m2",
                      stream=True)
        assert first.status_code == 200
        raw1 = first.headers[_RESUME_HEADER]
        _flush(relay)
        assert relay.conversation_store.turn_seqs(cid, key_id) == [1]

        # Second turn explicitly selects a different literal model.
        second = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                       ua="opencode/0.1.0", prompt="two", model="m1",
                       stream=True, resume=raw1)
        assert second.status_code == 200
        body = second.text

        assert body.count("event: relay:model_switched") == 1
        assert '"from_model": "m2"' in body
        assert '"to_model": "m1"' in body
        assert '"reason": "selection"' in body
        assert '"switch_count": 0' in body

        # The resumed conversation's last model is now m1.
        _flush(relay)
        assert relay.conversation_store.turn_seqs(cid, key_id) == [1, 2]

    def test_relay_fallback_emits_failover_transition(
        self, monkeypatch, fake_registry, store_auth, client, tmp_path
    ):
        # Task routing sends "coding" only to B:m1; the anchor is m2, so the
        # Relay-initiated plan's first model differs -> reason="failover".
        monkeypatch.setattr(settings, "task_routing_enabled", True)
        monkeypatch.setattr(settings, "task_coding", ["B:m1"])
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        _register(
            relay,
            fake_registry,
            "A",
            ["m2"],
            {"m2": ["a-m2"]},
        )
        fake_b = _register(
            relay,
            fake_registry,
            "B",
            ["m1"],
            {"m1": ["b-m1"]},
        )
        key_id, raw_key = _create_key(store_auth)
        cid = "5" * 32

        first = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                      ua="opencode/0.1.0", prompt="one", model="m2",
                      stream=True)
        assert first.status_code == 200
        raw1 = first.headers[_RESUME_HEADER]
        _flush(relay)
        assert relay.conversation_store.turn_seqs(cid, key_id) == [1]

        # Routed through the "coding" task: the anchored plan resolves to
        # B:m1, differing from the anchor m2.
        second = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                       ua="opencode/0.1.0", prompt="two", model="coding",
                       stream=True, resume=raw1)
        assert second.status_code == 200
        body = second.text

        assert body.count("event: relay:model_switched") == 1
        assert '"from_model": "m2"' in body
        assert '"to_model": "m1"' in body
        assert '"reason": "failover"' in body
        assert '"switch_count": 0' in body
        assert fake_b.chat_calls

    def test_unchanged_model_has_no_cross_turn_transition(
        self, monkeypatch, fake_registry, store_auth, client, tmp_path
    ):
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        _register(
            relay,
            fake_registry,
            "A",
            ["m2"],
            {"m2": ["a-m2"]},
        )
        key_id, raw_key = _create_key(store_auth)
        cid = "6" * 32

        first = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                      ua="opencode/0.1.0", prompt="one", model="m2",
                      stream=True)
        assert first.status_code == 200
        raw1 = first.headers[_RESUME_HEADER]
        _flush(relay)

        # Same model on resume: no cross-turn transition event.
        second = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                       ua="opencode/0.1.0", prompt="two", model="m2",
                       stream=True, resume=raw1)
        assert second.status_code == 200
        assert "event: relay:model_switched" not in second.text

    def test_within_turn_switch_not_duplicated_by_annotation(
        self, monkeypatch, fake_registry, store_auth, client, tmp_path
    ):
        # A and B both host m2. Turn 2's anchor is m2 (same logical model),
        # so annotate adds nothing; the within-turn A->B switch is the only
        # relay:model_switched event, emitted by on_switch (switch_count=1).
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        fake_a = _register(
            relay,
            fake_registry,
            "A",
            ["m2"],
            {"m2": ["a-m2-1", ProviderError("boom")]},
        )
        fake_b = _register(
            relay,
            fake_registry,
            "B",
            ["m2"],
            {"m2": ["b-m2-1", "b-m2-2"]},
        )
        key_id, raw_key = _create_key(store_auth)
        cid = "7" * 32

        first = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                      ua="opencode/0.1.0", prompt="one", model="m2",
                      stream=True)
        assert first.status_code == 200
        raw1 = first.headers[_RESUME_HEADER]
        _flush(relay)
        assert fake_a.chat_calls[-1][0] == "A"

        # Turn 2: same logical model m2, A fails mid-turn, B serves it.
        second = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                       ua="opencode/0.1.0", prompt="two", model="",
                       resume=raw1, stream=True)
        assert second.status_code == 200
        body = second.text

        # Exactly one model_switched event: the within-turn A->B switch
        # (switch_count=1, reason=failover). The annotation added nothing
        # because the model is unchanged, so no switch_count=0 event exists.
        assert body.count("event: relay:model_switched") == 1
        assert '"from_provider": "A"' in body
        assert '"to_provider": "B"' in body
        assert '"reason": "failover"' in body
        assert '"switch_count": 1' in body
        assert '"switch_count": 0' not in body
        assert fake_b.chat_calls[-1][0] == "B"

    def test_selection_then_within_turn_failover_emits_both_events_in_order(
        self, monkeypatch, fake_registry, store_auth, client, tmp_path
    ):
        # Turn 2 selects a different literal model (m1) AND the chosen
        # provider fails mid-turn. Exactly two events, in order: the
        # cross-turn annotation (selection, switch_count=0) first, then the
        # execution-time failover (switch_count=1).
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        _register(
            relay,
            fake_registry,
            "A",
            ["m1", "m2"],
            {"m1": [ProviderError("boom")], "m2": ["a-m2"]},
        )
        fake_b = _register(
            relay,
            fake_registry,
            "B",
            ["m1"],
            {"m1": ["b-m1"]},
        )
        key_id, raw_key = _create_key(store_auth)
        cid = "9" * 32

        first = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                      ua="opencode/0.1.0", prompt="one", model="m2",
                      stream=True)
        assert first.status_code == 200
        raw1 = first.headers[_RESUME_HEADER]
        _flush(relay)
        assert relay.conversation_store.turn_seqs(cid, key_id) == [1]

        second = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                       ua="opencode/0.1.0", prompt="two", model="m1",
                       stream=True, resume=raw1)
        assert second.status_code == 200
        body = second.text

        events = body.split("event: relay:model_switched")
        assert len(events) == 3
        annotated, failed_over = events[1], events[2]
        assert '"reason": "selection"' in annotated
        assert '"switch_count": 0' in annotated
        assert '"from_model": "m2"' in annotated
        assert '"to_model": "m1"' in annotated
        assert '"reason": "failover"' in failed_over
        assert '"switch_count": 1' in failed_over
        assert '"from_provider": "A"' in failed_over
        assert '"to_provider": "B"' in failed_over
        assert fake_b.chat_calls[-1][0] == "B"

    def test_unavailable_anchor_falls_through_with_failover_event(
        self, monkeypatch, fake_registry, store_auth, client, tmp_path
    ):
        # Case D at the HTTP surface: the anchor model m2 is unavailable at
        # routing time, so the anchored plan's anchor tier is empty, the
        # plan falls through to m1, and the cross-turn event classifies the
        # Relay-initiated change as reason="failover" (switch_count=0).
        monkeypatch.setattr(settings, "health_refresh_enabled", False)
        monkeypatch.setattr(settings, "health_aware_routing", True)
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        fake_a = _register(
            relay,
            fake_registry,
            "A",
            ["m1", "m2"],
            {"m2": ["a-m2"], "m1": ["a-m1"]},
        )
        _register(
            relay,
            fake_registry,
            "B",
            ["m1"],
            {"m1": ["b-m1"]},
        )
        key_id, raw_key = _create_key(store_auth)
        cid = "10" * 32

        # Turn 1: literal m2, A healthy with m2 -> committed anchor m2.
        relay.health_store.save(_make_report("A", HEALTHY, ["m2"]))
        relay.health_store.save(_make_report("B", HEALTHY, ["m1"]))
        first = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                      ua="opencode/0.1.0", prompt="one", model="m2",
                      stream=True)
        assert first.status_code == 200
        raw1 = first.headers[_RESUME_HEADER]
        _flush(relay)
        assert relay.conversation_store.turn_seqs(cid, key_id) == [1]

        # Turn 2: m2 is now unavailable on A; the anchor falls through to
        # the fallback tier and the plan serves A:m1.
        relay.health_store.save(
            ProviderHealth(
                name="A",
                status=HEALTHY,
                latency_ms=5,
                last_checked="now",
                details="ok",
                connectivity=True,
                rate_limit_status="ok",
                last_successful_request=None,
                healthy_models=["m1"],
                degraded_models=[],
                unavailable_models=["m2"],
                unsupported_models=[],
            )
        )
        relay.health_store.save(_make_report("B", HEALTHY, ["m1"]))

        second = _post(client, raw_key=raw_key, cid=cid, project="proj-x",
                       ua="opencode/0.1.0", prompt="two", model="",
                       stream=True, resume=raw1)
        assert second.status_code == 200
        body = second.text

        assert body.count("event: relay:model_switched") == 1
        assert '"from_model": "m2"' in body
        assert '"to_model": "m1"' in body
        assert '"reason": "failover"' in body
        assert '"switch_count": 0' in body
        assert _payload_calls(fake_a)[-1][1]["model"] == "m1"


# ---------------------------------------------------------------------------
# HTTP: /chat surface shares the anchor wiring
# ---------------------------------------------------------------------------


class TestChatSurface:
    def test_chat_resumed_turn_stays_on_anchor_model(
        self, monkeypatch, fake_registry, store_auth, client, tmp_path
    ):
        monkeypatch.setattr(settings, "task_routing_enabled", True)
        monkeypatch.setattr(settings, "task_coding", ["A:m2"])
        relay = _build_continuity_relay(monkeypatch, tmp_path)
        fake_a = _register(
            relay,
            fake_registry,
            "A",
            ["m1", "m2"],
            {"m1": ["a-m1"], "m2": ["a-m2-1", "a-m2-2"]},
        )
        _register(
            relay,
            fake_registry,
            "B",
            ["m1"],
            {"m1": ["b-m1"]},
        )
        key_id, raw_key = _create_key(store_auth)
        cid = "8" * 32

        # Turn 1: /chat routes task "coding" to A:m2. Last model: m2.
        first = _post_chat(client, raw_key=raw_key, cid=cid, project="proj-x",
                           ua="opencode/0.1.0", message="one", task="coding")
        assert first.status_code == 200
        assert first.json()["model"] == "m2"
        raw1 = first.headers[_RESUME_HEADER]
        assert raw1
        _flush(relay)
        assert relay.conversation_store.turn_seqs(cid, key_id) == [1]

        # Turn 2: default (no task) routing. The anchor m2 keeps the plan on
        # A:m2 even though a bare no-anchor plan would serve A:m1 first.
        second = _post_chat(client, raw_key=raw_key, cid=cid, project="proj-x",
                            ua="opencode/0.1.0", message="two", resume=raw1)
        assert second.status_code == 200
        assert second.json()["provider"] == "A"
        assert second.json()["model"] == "m2"

        _flush(relay)
        assert relay.conversation_store.turn_seqs(cid, key_id) == [1, 2]
