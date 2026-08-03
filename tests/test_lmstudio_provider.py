import pytest

from app.core.config import Settings
from app.providers.base import ModelProbe, Provider
from app.providers.exceptions import ProviderHTTPError
from app.providers.lmstudio import create_provider
from app.providers.lmstudio_client import LMStudioClient
from app.providers.openai_compat_client import OpenAICompatibleClient
from app.services.candidate_builder import CandidateBuilder
from app.services.chat_service import ChatService
from app.services.client_registry import ClientRegistry
from app.services.explanation import ExplanationService
from app.services.health_checker import HEALTHY, HealthChecker
from app.services.health_store import HealthStore
from app.services.provider_manager import ProviderManager
from app.services.scoring import CandidateScorer, Rankable
from app.services.telemetry import TelemetryStore


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        return self._json


class FakeClient:
    def __init__(self, probe_healthy=True):
        self.probe_healthy = probe_healthy
        self.chat_response = "local reply"

    def probe_model(self, provider, model):
        if self.probe_healthy:
            return ModelProbe(True, 10, 200, "")
        return ModelProbe(False, 10, 503, "boom")

    def chat(self, provider, model, message):
        return self.chat_response


class FailingClient:
    def chat(self, provider, model, message):
        raise ProviderHTTPError(500, "boom")


def make_lm_provider(models=("local-model",)):
    return Provider(
        name="LM Studio",
        base_url="http://localhost:1234/v1",
        models=list(models),
        requires_api_key=False,
        priority=1,
    )


def patch_discovery(monkeypatch, data, recorded=None):
    def handler(url, **kwargs):
        if recorded is not None:
            recorded["url"] = url
            recorded["headers"] = kwargs.get("headers", {})
        return FakeResponse(200, {"data": data})

    monkeypatch.setattr(
        "app.providers.openai_compat_client.httpx.get",
        handler,
    )


class TestCreateProvider:
    def test_keyless_registration_and_discovery(self, monkeypatch):
        recorded = {}
        patch_discovery(
            monkeypatch,
            [{"id": "qwen-7b"}, {"id": "llama-3b"}],
            recorded,
        )
        monkeypatch.setattr(
            "app.core.config.settings.lmstudio_base_url",
            "http://localhost:1234/v1",
        )
        monkeypatch.setattr(
            "app.core.config.settings.lmstudio_api_key",
            "",
        )
        monkeypatch.setattr(
            "app.core.config.settings.lmstudio_model_priority",
            ["llama-3b"],
        )
        monkeypatch.setattr(
            "app.core.config.settings.lmstudio_priority",
            3,
        )

        provider = create_provider()

        assert provider.name == "LM Studio"
        assert provider.requires_api_key is False
        assert provider.api_key == ""
        assert provider.priority == 3
        assert provider.models == ["llama-3b", "qwen-7b"]
        assert provider.priority_models == ["llama-3b"]
        assert recorded["url"] == "http://localhost:1234/v1/models"
        assert "Authorization" not in recorded["headers"]

    def test_discovery_failure_returns_provider_with_no_models(self, monkeypatch):
        def handler(url, **kwargs):
            raise ProviderHTTPError(0, "offline")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.httpx.get",
            handler,
        )
        monkeypatch.setattr(
            "app.core.config.settings.lmstudio_model_priority",
            [],
        )

        provider = create_provider()

        assert provider.name == "LM Studio"
        assert provider.requires_api_key is False
        assert provider.models == []

    def test_with_key_sends_auth_header(self, monkeypatch):
        recorded = {}
        patch_discovery(monkeypatch, [{"id": "m1"}], recorded)
        monkeypatch.setattr(
            "app.core.config.settings.lmstudio_api_key",
            "local-key",
        )
        monkeypatch.setattr(
            "app.core.config.settings.lmstudio_model_priority",
            [],
        )

        create_provider()

        assert recorded["headers"]["Authorization"] == "Bearer local-key"

    def test_lmstudio_client_is_shared_client(self):
        assert issubclass(LMStudioClient, OpenAICompatibleClient)
        assert LMStudioClient().name == "LM Studio"


class TestProviderManager:
    def test_ranked_includes_keyless_lmstudio(self):
        manager = ProviderManager()
        manager.register(
            Provider(
                name="LM Studio",
                base_url="http://localhost:1234/v1",
                priority=1,
                requires_api_key=False,
            )
        )
        manager.register(
            Provider(
                name="OpenAI",
                base_url="https://api.openai.com/v1",
                api_key="sk",
                priority=5,
            )
        )
        manager.register(
            Provider(
                name="KeylessCloud",
                base_url="https://x.invalid",
                priority=9,
            )
        )
        manager.register(
            Provider(
                name="Disabled",
                base_url="http://localhost:1",
                enabled=False,
                requires_api_key=False,
            )
        )

        ranked = manager.ranked()

        assert [p.name for p in ranked] == ["OpenAI", "LM Studio"]

    def test_best_falls_through_to_keyless(self):
        manager = ProviderManager()
        manager.register(
            Provider(
                name="LM Studio",
                base_url="http://localhost:1234/v1",
                requires_api_key=False,
            )
        )

        assert manager.best() is not None
        assert manager.best().name == "LM Studio"

    def test_keyless_cloud_provider_still_excluded(self):
        manager = ProviderManager()
        manager.register(
            Provider(
                name="NVIDIA",
                base_url="https://integrate.api.nvidia.com/v1",
            )
        )

        assert manager.ranked() == []
        assert manager.best() is None


class TestRegistry:
    def test_registry_resolves_lmstudio(self):
        client = ClientRegistry().get("LM Studio")

        assert isinstance(client, LMStudioClient)


class TestChatFlow:
    def test_chat_flow_uses_lmstudio_client(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.client_registry.ClientRegistry.get",
            lambda self, name: FakeClient(),
        )

        service = ChatService()
        provider = make_lm_provider()

        result = service.chat_across([(provider, "local-model")], "hello")

        assert result["success"] is True
        assert result["provider"] == "LM Studio"
        assert result["model"] == "local-model"
        assert result["response"] == "local reply"

    def test_failover_to_next_provider(self, monkeypatch):
        clients = {
            "LM Studio": FailingClient(),
            "OpenAI": FakeClient(),
        }
        monkeypatch.setattr(
            "app.services.client_registry.ClientRegistry.get",
            lambda self, name: clients[name],
        )

        service = ChatService()
        lm = make_lm_provider()
        oa = Provider(
            name="OpenAI",
            base_url="https://api.openai.com/v1",
            api_key="sk",
            models=["gpt"],
        )

        result = service.chat_across([(lm, "local-model"), (oa, "gpt")], "hello")

        assert result["success"] is True
        assert result["provider"] == "OpenAI"
        assert result["fallback_reason"] == "HTTP 500: boom"


class TestHealthCompatibility:
    def test_health_check_reports_lmstudio_healthy(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.health_checker.httpx.get",
            lambda url, **kwargs: FakeResponse(200, {"data": []}),
        )
        monkeypatch.setattr(
            "app.services.client_registry.ClientRegistry.get",
            lambda self, name: FakeClient(),
        )

        store = HealthStore()
        checker = HealthChecker(health_store=store)
        provider = make_lm_provider()

        report = checker.check(provider)

        assert report.status == HEALTHY
        assert report.healthy_models == ["local-model"]
        assert report.connectivity is True


class TestCandidatePipeline:
    def test_candidate_builder_includes_lmstudio(self):
        provider = make_lm_provider()
        builder = CandidateBuilder()

        candidates = builder.build([provider])

        assert candidates == [(provider, "local-model")]

    def test_telemetry_and_scoring_compatible(self):
        telemetry = TelemetryStore()
        telemetry.record_attempt(
            "LM Studio",
            "local-model",
            success=True,
            latency_ms=50,
        )

        stats = telemetry.get("LM Studio", "local-model")
        assert stats is not None
        assert stats.success_count == 1

        rankable = Rankable(
            provider="LM Studio",
            model="local-model",
            priority=1,
            health_band=0,
            telemetry=stats,
        )

        breakdown = CandidateScorer().breakdown(rankable)

        assert breakdown["total"] > 0
        assert breakdown["health_band"] == 0

    def test_explanation_compatible(self):
        from app.services.candidate_builder import RankedCandidate

        rankable = Rankable(
            provider="LM Studio",
            model="local-model",
            priority=1,
            health_band=0,
        )

        ranked = [
            RankedCandidate(
                provider="LM Studio",
                model="local-model",
                rank=1,
                health_band=0,
                health_status=HEALTHY,
                telemetry=None,
                preference=None,
                breakdown=CandidateScorer().breakdown(rankable),
            )
        ]

        explanation = ExplanationService().explain(ranked, task=None)

        assert explanation["selected"] == {
            "provider": "LM Studio",
            "model": "local-model",
        }


class TestConfig:
    def test_lmstudio_config_defaults(self, monkeypatch):
        for name in (
            "LMSTUDIO_ENABLED",
            "LMSTUDIO_API_KEY",
            "LMSTUDIO_PRIORITY",
            "LMSTUDIO_MODEL_PRIORITY",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(
            "LMSTUDIO_BASE_URL",
            "http://localhost:1234/v1",
        )

        cfg = Settings()

        assert cfg.lmstudio_enabled is False
        assert cfg.lmstudio_base_url == "http://localhost:1234/v1"
        assert cfg.lmstudio_api_key == ""
        assert cfg.lmstudio_priority == 1
        assert cfg.lmstudio_model_priority == []

    def test_lmstudio_base_url_strips_trailing_slash(self, monkeypatch):
        monkeypatch.setenv(
            "LMSTUDIO_BASE_URL",
            "http://localhost:1234/v1/",
        )

        assert Settings().lmstudio_base_url == "http://localhost:1234/v1"

    def test_lmstudio_base_url_rejects_non_http_scheme(self, monkeypatch):
        monkeypatch.setenv("LMSTUDIO_BASE_URL", "ftp://localhost:1234/v1")

        with pytest.raises(ValueError):
            Settings()
