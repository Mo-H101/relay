import pytest

from app.core.config import settings
from app.core.relay import Relay
from app.providers.base import Provider
from app.providers.exceptions import ProviderHTTPError


def make_provider(name, models, priority=1):
    return Provider(
        name=name,
        base_url=f"https://{name.lower()}.invalid",
        api_key="test-key",
        enabled=True,
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
            raise ProviderHTTPError(500, f"no outcome for {model}")

        outcome = queue[0]

        if len(queue) > 1:
            queue.pop(0)

        if isinstance(outcome, Exception):
            raise outcome

        return outcome


@pytest.fixture(autouse=True)
def fake_registry(monkeypatch):
    from app.services import client_registry

    holder = {}

    def fake_get(self, provider_name):
        return holder[provider_name]

    monkeypatch.setattr(
        client_registry.ClientRegistry, "get", fake_get
    )
    return holder


class TestRelayFeedback:
    def test_chat_failure_updates_learned_state(
        self, monkeypatch, fake_registry
    ):
        monkeypatch.setattr(settings, "health_feedback_enabled", True)
        monkeypatch.setattr(settings, "health_aware_routing", True)

        relay = Relay()
        p_a = make_provider("A", ["a-1"], priority=10)
        p_b = make_provider("B", ["b-1"], priority=1)
        relay.provider_manager.register(p_a)
        relay.provider_manager.register(p_b)

        client_a = FakeClient()
        client_a.set_outcomes(
            "a-1", [ProviderHTTPError(401, "invalid api key")]
        )
        fake_registry["A"] = client_a

        client_b = FakeClient()
        client_b.set_outcomes("b-1", ["hello from b"])
        fake_registry["B"] = client_b

        result = relay.chat("hi")

        assert result["success"] is True
        assert result["provider"] == "B"

        state = relay.health_store.learned("A")
        assert state is not None
        assert state.provider_status == "unavailable"

    def test_success_attempt_clears_learned_state(
        self, monkeypatch, fake_registry
    ):
        monkeypatch.setattr(settings, "health_feedback_enabled", True)
        monkeypatch.setattr(settings, "health_aware_routing", True)

        relay = Relay()
        p_a = make_provider("A", ["a-1"], priority=1)
        relay.provider_manager.register(p_a)

        client_a = FakeClient()
        client_a.set_outcomes("a-1", ["ok"])
        fake_registry["A"] = client_a

        relay.health_store.record_failure("A", "a-1", "auth_error")
        result = relay.chat("hi")

        assert result["success"] is True
        assert relay.health_store.learned("A") is None

    def test_feedback_disabled_by_default(
        self, monkeypatch, fake_registry
    ):
        monkeypatch.setattr(settings, "health_feedback_enabled", False)
        monkeypatch.setattr(settings, "health_aware_routing", True)

        relay = Relay()
        p_a = make_provider("A", ["a-1"], priority=1)
        p_b = make_provider("B", ["b-1"], priority=10)
        relay.provider_manager.register(p_a)
        relay.provider_manager.register(p_b)

        client_a = FakeClient()
        client_a.set_outcomes(
            "a-1", [ProviderHTTPError(401, "invalid api key")]
        )
        fake_registry["A"] = client_a

        client_b = FakeClient()
        client_b.set_outcomes("b-1", ["hello from b"])
        fake_registry["B"] = client_b

        result = relay.chat("hi")

        assert result["success"] is True
        assert relay.health_store.learned("A") is None
