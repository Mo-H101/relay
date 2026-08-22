import pytest

from fastapi.testclient import TestClient

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

    def chat(self, provider, model, message, timeout=None, max_tokens=None):
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

    def chat_stream(self, provider, model, message, timeout=None, max_tokens=None):
        self.chat_calls.append((provider.name, model))

        queue = self._outcomes.get(model)

        if not queue:
            raise ProviderError(f"no outcome configured for {model}")

        while queue:
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome:
                yield outcome

    async def achat(self, provider, model, message, timeout=None, max_tokens=None):
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

    async def achat_stream(self, provider, model, message, timeout=None, max_tokens=None):
        """Async version of chat_stream()."""
        self.chat_calls.append((provider.name, model))

        queue = self._outcomes.get(model)

        if not queue:
            raise ProviderError(f"no outcome configured for {model}")

        while queue:
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome:
                yield outcome

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

        # Return a default response similar to test_openai_api.py
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 1700000000,
            "model": payload["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": outcome},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

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

        # Return a default response similar to test_openai_api.py
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 1700000000,
            "model": payload["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": outcome},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

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

        relays[id(relay)] = relay
        return relay

    yield _build

    for relay in relays.values():
        monkeypatch.setattr(app.api.chat, "relay", relay)
        monkeypatch.setattr(app.api.decision, "relay", relay)
        monkeypatch.setattr(app.api.health, "relay", relay)
        monkeypatch.setattr(app.api.providers, "relay", relay)


@pytest.fixture
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


class TestChatSuccess:
    def test_chat_success_returns_200_with_response(
        self, wired_relay, fake_registry, client
    ):
        provider = make_provider("A", ["a-1", "a-2"])
        make_client(
            fake_registry,
            "A",
            {"a-1": ["hello world"], "a-2": ["nope"]},
        )
        wired_relay(providers=[provider])

        response = client.post(
            "/chat",
            json={"message": "hello"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["provider"] == "A"
        assert payload["model"] == "a-1"
        assert payload["response"] == "hello world"

    def test_chat_uses_routed_candidates(self, wired_relay, fake_registry, client):
        """
        With routing disabled (default), the highest-priority chat-testable
        candidate is used first.
        """
        p1 = make_provider("A", ["a-1"], priority=10)
        p2 = make_provider("B", ["b-1"], priority=1)
        make_client(fake_registry, "A", {"a-1": ["from-a"]})
        make_client(fake_registry, "B", {"b-1": ["from-b"]})
        wired_relay(providers=[p1, p2])

        response = client.post("/chat", json={"message": "hi"})

        assert response.status_code == 200
        assert response.json()["provider"] == "A"
        assert response.json()["model"] == "a-1"

    def test_chat_accepts_valid_task(self, wired_relay, fake_registry, client):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["ok"]})
        wired_relay(providers=[provider])

        response = client.post(
            "/chat",
            json={"message": "hi", "task": "coding"},
        )

        assert response.status_code == 200
        assert response.json()["response"] == "ok"


class TestChatFailure:
    def test_chat_failure_maps_to_502(self, wired_relay, fake_registry, client):
        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(400, "bad request")]},
        )
        wired_relay(providers=[provider])

        response = client.post("/chat", json={"message": "hi"})

        assert response.status_code == 502
        assert response.json()["detail"] == "Provider rejected the request."

    def test_chat_no_providers_maps_to_503(self, wired_relay, client):
        wired_relay(providers=[])

        response = client.post("/chat", json={"message": "hi"})

        assert response.status_code == 503
        assert response.json()["detail"] == "No provider available."

    def test_chat_unknown_task_maps_to_400(self, wired_relay, fake_registry, client):
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["ok"]})
        wired_relay(providers=[provider])

        response = client.post(
            "/chat",
            json={"message": "hi", "task": "not-a-task"},
        )

        assert response.status_code == 400
        assert "not-a-task" in response.json()["detail"]


class TestProviderEndpoint:
    def test_provider_returns_best_provider(self, wired_relay, fake_registry, client):
        p1 = make_provider("A", ["a-1"], priority=10)
        p2 = make_provider("B", ["b-1"], priority=1)
        wired_relay(providers=[p1, p2])

        response = client.get("/provider")

        assert response.status_code == 200
        payload = response.json()
        assert payload["name"] == "A"
        assert payload["priority"] == 10
        assert payload["enabled"] is True
        assert payload["models"] == ["a-1"]

    def test_provider_returns_none_when_empty(self, wired_relay, client):
        wired_relay(providers=[])

        response = client.get("/provider")

        assert response.status_code == 200
        payload = response.json()
        assert payload["provider"] is None
        assert payload["message"] == "No provider available."

    def test_provider_skips_disabled_and_keyless(self, wired_relay, fake_registry, client):
        p1 = make_provider("A", ["a-1"], priority=10, enabled=False)
        p2 = make_provider("B", ["b-1"], priority=5, api_key="  ")
        p3 = make_provider("C", ["c-1"], priority=1, api_key="key")
        wired_relay(providers=[p1, p2, p3])

        response = client.get("/provider")

        assert response.json()["name"] == "C"

    def test_provider_schema_is_stable(self, wired_relay, fake_registry, client):
        provider = make_provider("A", ["a-1"], priority=10)
        wired_relay(providers=[provider])

        response = client.get("/provider")

        assert response.status_code == 200
        assert set(response.json().keys()) == {
            "name",
            "priority",
            "enabled",
            "models",
        }


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


class TestProviderHealthAwareSelection:
    def test_healthy_lower_priority_beats_degraded_high_priority(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "health_aware_routing", True)

        p_a = make_provider("A", ["a-1"], priority=10)
        p_b = make_provider("B", ["b-1"], priority=1)
        relay = wired_relay(providers=[p_a, p_b])

        relay.health_store.save(
            make_report("A", DEGRADED, degraded=("a-1",))
        )
        relay.health_store.save(
            make_report("B", HEALTHY, healthy=("b-1",))
        )

        response = client.get("/provider")

        assert response.status_code == 200
        assert response.json()["name"] == "B"

    def test_telemetry_influences_same_health_choice(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "health_aware_routing", True)

        p_a = make_provider("A", ["a-1"], priority=10)
        p_b = make_provider("B", ["b-1"], priority=1)
        relay = wired_relay(providers=[p_a, p_b])

        relay.health_store.save(
            make_report("A", HEALTHY, healthy=("a-1",))
        )
        relay.health_store.save(
            make_report("B", HEALTHY, healthy=("b-1",))
        )

        relay.telemetry.record_attempt("A", "a-1", True, 1000)
        relay.telemetry.record_attempt("B", "b-1", True, 50)

        response = client.get("/provider")

        assert response.status_code == 200
        assert response.json()["name"] == "B"

    def test_no_intelligence_data_keeps_priority_selection(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "health_aware_routing", True)

        p_a = make_provider("A", ["a-1"], priority=10)
        p_b = make_provider("B", ["b-1"], priority=1)
        wired_relay(providers=[p_a, p_b])

        response = client.get("/provider")

        assert response.status_code == 200
        assert response.json()["name"] == "A"


class TestDecisionExplainEndpoint:
    def test_explain_disabled_by_default(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "decision_explanations_enabled", False)
        p1 = make_provider("A", ["a-1"], priority=10)
        wired_relay(providers=[p1])

        response = client.get("/decision/explain")

        assert response.status_code == 200
        assert response.json() == {
            "enabled": False,
            "message": "Decision explanations are disabled.",
        }

    def test_explain_returns_selected_and_ranking(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "decision_explanations_enabled", True)
        monkeypatch.setattr(settings, "health_aware_routing", True)
        p_a = make_provider("A", ["a-1"], priority=10)
        p_b = make_provider("B", ["b-1"], priority=1)
        relay = wired_relay(providers=[p_a, p_b])
        relay.health_store.save(
            make_report("A", DEGRADED, degraded=("a-1",))
        )
        relay.health_store.save(
            make_report("B", HEALTHY, healthy=("b-1",))
        )

        response = client.get("/decision/explain")

        assert response.status_code == 200
        payload = response.json()
        assert payload["selected"] == {"provider": "B", "model": "b-1"}
        assert [c["provider"] for c in payload["candidates"]] == ["B", "A"]
        assert [c["rank"] for c in payload["candidates"]] == [1, 2]
        assert payload["generated_at"]
        assert set(payload["candidates"][0]["score_breakdown"].keys()) == {
            "health_band",
            "priority",
            "success",
            "latency",
            "failure",
            "preference",
            "task_compatibility",
            "adaptive_reliability",
            "adaptive_latency",
            "quality",
            "cost",
            "total",
        }

    def test_explain_health_influence_reason(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "decision_explanations_enabled", True)
        monkeypatch.setattr(settings, "health_aware_routing", True)
        p_a = make_provider("A", ["a-1"], priority=10)
        p_b = make_provider("B", ["b-1"], priority=1)
        relay = wired_relay(providers=[p_a, p_b])
        relay.health_store.save(
            make_report("A", DEGRADED, degraded=("a-1",))
        )
        relay.health_store.save(
            make_report("B", HEALTHY, healthy=("b-1",))
        )

        response = client.get("/decision/explain")

        payload = response.json()
        lower = payload["candidates"][1]
        assert "worse health band (degraded vs healthy)" in " ".join(
            lower["reasons"]
        )

    def test_explain_no_providers(
        self, wired_relay, client, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "decision_explanations_enabled", True)
        wired_relay(providers=[])

        response = client.get("/decision/explain")

        assert response.status_code == 200
        payload = response.json()
        assert payload["selected"] is None
        assert payload["candidates"] == []


class TestHealthEndpoint:
    def _wire_connectivity(self, relay, ok=True):
        import types

        def fake_check_connectivity(self_provider, provider):
            return (ok, "ok", 5)

        relay.health_checker._check_connectivity = types.MethodType(
            fake_check_connectivity, relay.health_checker
        )

    def test_health_reports_provider_status(
        self, wired_relay, fake_registry, client
    ):
        provider = make_provider(
            "A",
            ["meta/llama-3-70b", "nvidia/nim-embedding"],
            priority=10,
        )
        make_client(
            fake_registry,
            "A",
            probes={
                "meta/llama-3-70b": ModelProbe(True, 50, 200, ""),
                "nvidia/nim-embedding": ModelProbe(True, 30, 200, ""),
            },
        )
        relay = wired_relay(providers=[provider])
        self._wire_connectivity(relay)

        response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

        report = relay.health(deep=False)
        assert report["deep"] is False
        assert len(report["providers"]) == 1
        provider_report = report["providers"][0]
        assert provider_report["name"] == "A"
        assert provider_report["status"] == "healthy"
        assert provider_report["connectivity"] is True
        assert provider_report["healthy_models"] == ["meta/llama-3-70b"]
        assert provider_report["unsupported_models"] == ["nvidia/nim-embedding"]

    def test_health_deep_probes_every_chat_model(
        self, wired_relay, fake_registry, client
    ):
        provider = make_provider(
            "A",
            ["meta/llama-3-70b", "meta/llama-3-8b"],
            priority=10,
        )
        client_fake = make_client(
            fake_registry,
            "A",
            probes={
                "meta/llama-3-70b": ModelProbe(True, 50, 200, ""),
                "meta/llama-3-8b": ModelProbe(False, 90, 500, "boom"),
            },
        )
        relay = wired_relay(providers=[provider])
        self._wire_connectivity(relay)

        response = client.get("/health/deep")

        assert response.status_code == 200
        payload = response.json()
        assert payload["deep"] is True
        report = payload["providers"][0]
        assert client_fake.probe_calls == [
            ("A", "meta/llama-3-70b"),
            ("A", "meta/llama-3-8b"),
        ]
        assert report["healthy_models"] == ["meta/llama-3-70b"]
        assert report["unavailable_models"] == ["meta/llama-3-8b"]

    def test_health_unreachable_provider(self, wired_relay, fake_registry, client):
        provider = make_provider("A", ["meta/llama-3-70b"])
        make_client(
            fake_registry,
            "A",
            probes={"meta/llama-3-70b": ModelProbe(True, 50, 200, "")},
        )
        relay = wired_relay(providers=[provider])
        self._wire_connectivity(relay, ok=False)

        response = client.get("/health")

        assert response.json() == {"status": "unavailable"}

        report = relay.health(deep=False)["providers"][0]
        assert report["status"] == "unavailable"
        assert report["connectivity"] is False
        assert report["healthy_models"] == []


class TestRoutingThroughAPI:
    def _enable_routing(self, monkeypatch, task_coding=None):
        from app.core.config import settings

        monkeypatch.setattr(settings, "task_routing_enabled", True)
        monkeypatch.setattr(
            settings,
            "task_coding",
            task_coding or ["a-1"],
        )
        monkeypatch.setattr(settings, "task_vision", [])
        monkeypatch.setattr(settings, "task_reasoning", [])
        monkeypatch.setattr(settings, "task_general", [])
        monkeypatch.setattr(settings, "task_creative", [])
        monkeypatch.setattr(settings, "task_translation", [])

    def test_routing_enabled_uses_task_refs(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        self._enable_routing(monkeypatch, task_coding=["b-1"])
        p1 = make_provider("A", ["a-1"], priority=10)
        p2 = make_provider("B", ["b-1"], priority=1)
        make_client(fake_registry, "A", {"a-1": ["from-a"]})
        make_client(fake_registry, "B", {"b-1": ["from-b"]})
        wired_relay(providers=[p1, p2])

        response = client.post(
            "/chat",
            json={"message": "hi", "task": "coding"},
        )

        assert response.status_code == 200
        assert response.json()["provider"] == "B"
        assert response.json()["model"] == "b-1"

    def test_routing_disabled_ignores_task_refs(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "task_routing_enabled", False)
        monkeypatch.setattr(settings, "task_coding", ["b-1"])
        p1 = make_provider("A", ["a-1"], priority=10)
        p2 = make_provider("B", ["b-1"], priority=1)
        make_client(fake_registry, "A", {"a-1": ["from-a"]})
        make_client(fake_registry, "B", {"b-1": ["from-b"]})
        wired_relay(providers=[p1, p2])

        response = client.post(
            "/chat",
            json={"message": "hi", "task": "coding"},
        )

        assert response.status_code == 200
        assert response.json()["provider"] == "A"

    def test_routing_enabled_falls_back_without_refs(
        self, wired_relay, fake_registry, client, monkeypatch
    ):
        self._enable_routing(monkeypatch, task_coding=[])
        p1 = make_provider("A", ["a-1"], priority=10)
        make_client(fake_registry, "A", {"a-1": ["from-a"]})
        wired_relay(providers=[p1])

        response = client.post(
            "/chat",
            json={"message": "hi", "task": "coding"},
        )

        assert response.status_code == 200
        assert response.json()["provider"] == "A"


class TestFailoverThroughRelay:
    def test_model_level_failover(self, wired_relay, fake_registry):
        provider = make_provider("A", ["a-1", "a-2", "a-3"])
        client = make_client(
            fake_registry,
            "A",
            {
                "a-1": [ProviderTimeout("slow")],
                "a-2": [ProviderError("boom")],
                "a-3": ["ok-from-a-3"],
            },
        )
        relay = wired_relay(providers=[provider])

        result = relay.chat("hello")

        assert result["success"] is True
        assert result["provider"] == "A"
        assert result["model"] == "a-3"
        assert result["response"] == "ok-from-a-3"
        assert client.chat_calls == [
            ("A", "a-1"),
            ("A", "a-1"),
            ("A", "a-2"),
            ("A", "a-2"),
            ("A", "a-3"),
        ]
        assert result["fallback_reason"] == "Provider request failed."

    def test_provider_level_failover_skips_provider(
        self, wired_relay, fake_registry
    ):
        p_a = make_provider("A", ["a-1", "a-2"], priority=10)
        p_b = make_provider("B", ["b-1"], priority=1)
        client_a = make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(401, "auth")]},
        )
        make_client(fake_registry, "B", {"b-1": ["ok-from-b"]})
        relay = wired_relay(providers=[p_a, p_b])

        result = relay.chat("hello")

        assert result["success"] is True
        assert result["provider"] == "B"
        assert result["response"] == "ok-from-b"
        assert client_a.chat_calls == [("A", "a-1")]
        assert result["fallback_reason"] == "Provider authentication failed."

    def test_all_candidates_fail_returns_failure(self, wired_relay, fake_registry):
        provider = make_provider("A", ["a-1"])
        make_client(
            fake_registry,
            "A",
            {"a-1": [ProviderHTTPError(500, "down")]},
        )
        relay = wired_relay(providers=[provider])

        result = relay.chat("hello")

        assert result["success"] is False
        assert result["provider"] == "A"
        assert result["fallback_reason"] is None
        assert "Provider returned a server error." in result["error"]
