import pytest

from app.providers.base import ModelProbe, Provider
from app.services.capabilities import (
    ModelCapability,
    detect_capability,
    is_chat_testable,
)
from app.services.health_checker import (
    DEGRADED,
    HEALTHY,
    UNAVAILABLE,
    UNSUPPORTED,
    _DEFAULT_PROBE_COUNT,
    HealthChecker,
)


def make_provider(models, priority=None, name="Test"):
    provider = Provider(
        name=name,
        base_url="https://example.invalid",
        api_key="test-key",
        models=list(models),
        priority_models=list(priority or []),
    )
    return provider


class FakeClient:
    """Deterministic probe client for health-check tests."""

    def __init__(self, probes):
        self.probes = probes
        self.calls = []

    def probe_model(self, provider, model):
        self.calls.append(model)
        if model not in self.probes:
            return ModelProbe(False, 0, 404, "missing probe")
        return self.probes[model]


@pytest.fixture
def checker():
    return HealthChecker()


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


def set_connectivity(monkeypatch, checker, ok, details="ok", latency=5):
    monkeypatch.setattr(
        checker,
        "_check_connectivity",
        lambda provider: (ok, details, latency),
    )


class TestConnectivityDispatch:
    class _Resp:
        status_code = 200

    def test_client_probe_is_used_when_present(self, fake_registry):
        calls = []

        class ProbingClient:
            def connectivity_probe(self, provider):
                calls.append(provider.name)
                return (True, "HTTP 200", 7)

        fake_registry["Test"] = ProbingClient()
        checker = HealthChecker()
        provider = make_provider([])

        ok, details, latency = checker._check_connectivity(provider)

        assert (ok, details, latency) == (True, "HTTP 200", 7)
        assert calls == ["Test"]

    def test_fallback_used_for_unknown_client(self, monkeypatch):
        captured = {}

        def fake_get(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers", {})
            captured["timeout"] = kwargs.get("timeout")
            return self._Resp()

        monkeypatch.setattr(
            "app.services.health_checker.bounded_get", fake_get
        )

        provider = Provider(
            name="Ghost", base_url="https://ghost.invalid", api_key="k"
        )

        ok, details, latency = HealthChecker()._check_connectivity(provider)

        assert ok is True
        assert details == "HTTP 200"
        assert isinstance(latency, int)
        assert captured["url"] == "https://ghost.invalid/models"
        assert captured["headers"]["Authorization"] == "Bearer k"
        assert captured["timeout"] == 10

    def test_client_probe_matches_fallback_for_same_response(self, monkeypatch):
        from app.providers.openai_compat_client import OpenAICompatibleClient

        calls = []

        def shared_get(url, **kwargs):
            calls.append(url)
            return self._Resp()

        monkeypatch.setattr(
            "app.services.health_checker.bounded_get", shared_get
        )
        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_get", shared_get
        )

        provider = make_provider([])

        fallback = HealthChecker()._check_connectivity(provider)
        probe = OpenAICompatibleClient().connectivity_probe(provider)

        assert probe == fallback
        assert len(calls) == 2


class TestCapabilities:
    def test_embedding_is_not_chat_testable(self):
        for model in [
            "nvidia/nim-embedding",
            "BAAI/bge-large-en",
            "retriever/multi-qa",
        ]:
            assert detect_capability(model) == ModelCapability.EMBEDDING
            assert not is_chat_testable(model)

    def test_safety_is_not_chat_testable(self):
        for model in [
            "nvidia/nim-guard",
            "meta-llama-guard-2",
            "topic-control-model",
        ]:
            assert detect_capability(model) == ModelCapability.SAFETY
            assert not is_chat_testable(model)

    def test_translation_is_not_chat_testable(self):
        assert not is_chat_testable("nvidia/seamless-translate")

    def test_reward_is_not_chat_testable(self):
        assert not is_chat_testable("nvidia/reward-model")

    def test_parser_is_not_chat_testable(self):
        assert not is_chat_testable("microsoft/parsercat")

    def test_plain_chat_model_is_testable(self):
        assert is_chat_testable("meta/llama-3-70b")
        assert detect_capability("meta/llama-3-70b") == ModelCapability.CHAT

    def test_vision_models_are_testable(self):
        assert is_chat_testable("nvidia/llava-vl")
        assert detect_capability("nvidia/llava-vl") == ModelCapability.VISION


class TestHealthChecker:
    def test_healthy_models(self, checker, fake_registry, monkeypatch):
        set_connectivity(monkeypatch, checker, True)
        client = FakeClient(
            {
                "meta/llama-3-70b": ModelProbe(True, 120, 200, ""),
                "meta/llama-3-8b": ModelProbe(True, 90, 200, ""),
            }
        )
        fake_registry["Test"] = client
        provider = make_provider(
            ["meta/llama-3-70b", "meta/llama-3-8b"],
            priority=["meta/llama-3-70b", "meta/llama-3-8b"],
        )

        report = checker.check(provider)

        assert report.status == HEALTHY
        assert report.connectivity is True
        assert report.healthy_models == [
            "meta/llama-3-70b",
            "meta/llama-3-8b",
        ]
        assert report.unavailable_models == []
        assert len(client.calls) == 2

    def test_priority_models_only_when_not_deep(
        self, checker, fake_registry, monkeypatch
    ):
        set_connectivity(monkeypatch, checker, True)
        client = FakeClient(
            {
                "meta/llama-3-70b": ModelProbe(True, 120, 200, ""),
                "meta/llama-3-8b": ModelProbe(True, 90, 200, ""),
                "meta/llama-3-13b": ModelProbe(True, 60, 200, ""),
            }
        )
        fake_registry["Test"] = client
        provider = make_provider(
            ["meta/llama-3-70b", "meta/llama-3-8b", "meta/llama-3-13b"],
            priority=["meta/llama-3-70b"],
        )

        report = checker.check(provider, deep=False)

        assert client.calls == ["meta/llama-3-70b"]
        assert len(report.models) == 1

    def test_overloaded_model_is_degraded(
        self, checker, fake_registry, monkeypatch
    ):
        set_connectivity(monkeypatch, checker, True)
        client = FakeClient(
            {
                "meta/llama-3-70b": ModelProbe(
                    False, 100, 529, "overloaded"
                ),
                "meta/llama-3-8b": ModelProbe(True, 90, 200, ""),
            }
        )
        fake_registry["Test"] = client
        provider = make_provider(
            ["meta/llama-3-70b", "meta/llama-3-8b"],
            priority=["meta/llama-3-70b", "meta/llama-3-8b"],
        )

        report = checker.check(provider)

        assert report.status == HEALTHY
        assert report.degraded_models == ["meta/llama-3-70b"]
        assert report.healthy_models == ["meta/llama-3-8b"]
        assert report.models[0].status == DEGRADED
        assert report.models[0].status_code == 529

    def test_timeout_model_is_unavailable(
        self, checker, fake_registry, monkeypatch
    ):
        set_connectivity(monkeypatch, checker, True)
        client = FakeClient(
            {
                "meta/llama-3-70b": ModelProbe(
                    False, 10000, 0, "timeout"
                ),
            }
        )
        fake_registry["Test"] = client
        provider = make_provider(
            ["meta/llama-3-70b"],
            priority=["meta/llama-3-70b"],
        )

        report = checker.check(provider)

        assert report.status == DEGRADED
        assert report.degraded_models == ["meta/llama-3-70b"]
        assert report.unavailable_models == []
        assert report.models[0].status == DEGRADED
        assert report.models[0].error == "timeout"

    def test_unsupported_models_reported_without_probe(
        self, checker, fake_registry, monkeypatch
    ):
        set_connectivity(monkeypatch, checker, True)
        client = FakeClient(
            {
                "meta/llama-3-70b": ModelProbe(True, 120, 200, ""),
                "nvidia/nim-embedding": ModelProbe(False, 0, 0, ""),
                "meta-llama-guard-2": ModelProbe(False, 0, 0, ""),
            }
        )
        fake_registry["Test"] = client
        provider = make_provider(
            [
                "meta/llama-3-70b",
                "nvidia/nim-embedding",
                "meta-llama-guard-2",
            ],
            priority=["meta/llama-3-70b"],
        )

        report = checker.check(provider, deep=True)

        assert client.calls == ["meta/llama-3-70b"]
        unsupported = report.unsupported_models
        assert sorted(unsupported) == [
            "meta-llama-guard-2",
            "nvidia/nim-embedding",
        ]
        assert report.status == HEALTHY

    def test_provider_failure_is_unavailable(
        self, checker, fake_registry, monkeypatch
    ):
        set_connectivity(monkeypatch, checker, False, "connection refused")
        fake_registry["Test"] = FakeClient({})
        provider = make_provider(["meta/llama-3-70b"])

        report = checker.check(provider)

        assert report.status == UNAVAILABLE
        assert report.connectivity is False
        assert report.healthy_models == []
        assert report.models == []

    def test_no_chat_models_provider_is_unsupported(
        self, checker, fake_registry, monkeypatch
    ):
        set_connectivity(monkeypatch, checker, True)
        fake_registry["Test"] = FakeClient({})
        provider = make_provider(["nvidia/nim-embedding"])

        report = checker.check(provider)

        assert report.status == UNSUPPORTED

    def test_many_unavailable_models_but_some_healthy_is_healthy(
        self, checker, fake_registry, monkeypatch
    ):
        set_connectivity(monkeypatch, checker, True)
        probes = {
            f"meta/llama-3-{size}": ModelProbe(False, 100, 500, "boom")
            for size in ("8b", "13b", "70b", "90b")
        }
        probes["meta/llama-3-7b"] = ModelProbe(True, 50, 200, "")
        client = FakeClient(probes)
        fake_registry["Test"] = client
        provider = make_provider(
            list(probes.keys()),
            priority=list(probes.keys()),
        )

        report = checker.check(provider)

        assert report.status == HEALTHY
        assert len(report.unavailable_models) == 4
        assert report.healthy_models == ["meta/llama-3-7b"]

    def test_all_models_unavailable_provider_is_unavailable(
        self, checker, fake_registry, monkeypatch
    ):
        set_connectivity(monkeypatch, checker, True)
        client = FakeClient(
            {
                "meta/llama-3-70b": ModelProbe(False, 100, 500, "boom"),
                "meta/llama-3-8b": ModelProbe(False, 120, 503, "down"),
            }
        )
        fake_registry["Test"] = client
        provider = make_provider(
            ["meta/llama-3-70b", "meta/llama-3-8b"],
            priority=["meta/llama-3-70b", "meta/llama-3-8b"],
        )

        report = checker.check(provider)

        assert report.status == UNAVAILABLE
        assert sorted(report.unavailable_models) == [
            "meta/llama-3-70b",
            "meta/llama-3-8b",
        ]
        assert report.healthy_models == []

    def test_no_priority_falls_back_to_default_set(
        self, checker, fake_registry, monkeypatch
    ):
        set_connectivity(monkeypatch, checker, True)
        models = [
            "meta/llama-3-8b",
            "meta/llama-3-13b",
            "meta/llama-3-70b",
            "meta/llama-3-90b",
            "meta/llama-3-7b",
            "meta/llama-3-1b",
        ]
        probes = {model: ModelProbe(True, 50, 200, "") for model in models}
        client = FakeClient(probes)
        fake_registry["Test"] = client
        provider = make_provider(models)

        report = checker.check(provider)

        assert report.status == HEALTHY
        assert report.connectivity is True
        assert report.healthy_models == models[:_DEFAULT_PROBE_COUNT]

    def test_no_priority_and_all_defaults_unavailable_is_unavailable(
        self, checker, fake_registry, monkeypatch
    ):
        set_connectivity(monkeypatch, checker, True)
        client = FakeClient(
            {
                "meta/llama-3-8b": ModelProbe(False, 100, 500, "boom"),
                "meta/llama-3-13b": ModelProbe(False, 100, 503, "down"),
                "meta/llama-3-70b": ModelProbe(False, 100, 500, "boom"),
                "meta/llama-3-90b": ModelProbe(False, 100, 502, "bad"),
                "meta/llama-3-7b": ModelProbe(False, 100, 503, "down"),
            }
        )
        fake_registry["Test"] = client
        provider = make_provider(
            [
                "meta/llama-3-8b",
                "meta/llama-3-13b",
                "meta/llama-3-70b",
                "meta/llama-3-90b",
                "meta/llama-3-7b",
            ]
        )

        report = checker.check(provider)

        assert report.status == UNAVAILABLE
        assert len(report.unavailable_models) == _DEFAULT_PROBE_COUNT
        assert report.healthy_models == []

    def test_priority_overrides_fallback(
        self, checker, fake_registry, monkeypatch
    ):
        set_connectivity(monkeypatch, checker, True)
        client = FakeClient(
            {
                "meta/llama-3-8b": ModelProbe(True, 50, 200, ""),
                "meta/llama-3-13b": ModelProbe(True, 50, 200, ""),
                "meta/llama-3-70b": ModelProbe(True, 50, 200, ""),
            }
        )
        fake_registry["Test"] = client
        provider = make_provider(
            ["meta/llama-3-8b", "meta/llama-3-13b", "meta/llama-3-70b"],
            priority=["meta/llama-3-70b"],
        )

        report = checker.check(provider)

        assert client.calls == ["meta/llama-3-70b"]
        assert report.status == HEALTHY
        assert report.healthy_models == ["meta/llama-3-70b"]

    def test_no_probe_no_chat_models_is_not_unavailable(
        self, checker, fake_registry, monkeypatch
    ):
        set_connectivity(monkeypatch, checker, True)
        fake_registry["Test"] = FakeClient({})
        provider = make_provider(["nvidia/nim-embedding"])

        report = checker.check(provider)

        assert report.status == UNSUPPORTED
