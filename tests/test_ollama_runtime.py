"""
Focused runtime tests for Ollama (P4.2.1).

Covers the full runtime surface Ollama needs to join RUNTIME_READY:
registry promotion and registry-driven factory construction, config
loading, keyless auth, sync/async single-message and full-payload chat,
NDJSON streaming translation, health checking, failover through the chat
services, hot reload, and wizard deferral.
"""

import json
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings, settings
from app.providers.base import Provider
from app.providers.exceptions import ProviderHTTPError, ProviderTimeout
from app.providers.factory import build_runtime_provider
from app.providers.ollama_client import OllamaClient
from app.providers.registry import PROVIDER_REGISTRY, RUNTIME_READY
from app.services.async_chat_service import AsyncChatService
from app.services.chat_service import ChatService
from app.services.client_registry import ClientRegistry
from app.services.health_checker import HealthChecker


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json

    def read(self):
        return self.text


class FakeStreamResponse:
    def __init__(self, lines, status_code=200, text=""):
        self.lines = lines
        self.status_code = status_code
        self.text = text
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_lines(self):
        yield from self.lines

    def read(self):
        return self.text


def make_provider(base_url="http://localhost:11434"):
    return Provider(name="Ollama", base_url=base_url)


class TestConnectivityProbe:
    def test_success_is_keyless_and_hits_health_endpoint(self, monkeypatch):
        recorded = {}

        def handler(url, **kwargs):
            recorded["url"] = url
            recorded["headers"] = kwargs.get("headers", {})
            recorded["timeout"] = kwargs.get("timeout")
            return FakeResponse(status_code=200)

        monkeypatch.setattr("app.providers.ollama_client.httpx.get", handler)

        provider = make_provider()
        provider.health_endpoint = "/api/tags"

        ok, details, latency = OllamaClient().connectivity_probe(provider)

        assert ok is True
        assert details == "HTTP 200"
        assert isinstance(latency, int)
        assert recorded["url"] == "http://localhost:11434/api/tags"
        assert recorded["headers"] == {}
        assert recorded["timeout"] == 10

    def test_http_error_status_is_failure(self, monkeypatch):
        monkeypatch.setattr(
            "app.providers.ollama_client.httpx.get",
            lambda *args, **kwargs: FakeResponse(status_code=500),
        )

        ok, details, _ = OllamaClient().connectivity_probe(make_provider())

        assert ok is False
        assert details == "HTTP 500"

    def test_connection_exception_returns_failure(self, monkeypatch):
        def handler(url, **kwargs):
            raise httpx.ConnectError("server offline")

        monkeypatch.setattr("app.providers.ollama_client.httpx.get", handler)

        ok, details, _ = OllamaClient().connectivity_probe(make_provider())

        assert ok is False
        assert details == "provider unavailable"


def ollama_defn():
    return PROVIDER_REGISTRY["ollama"]


def patch_post(monkeypatch, response, recorded=None):
    def handler(url, **kwargs):
        if recorded is not None:
            recorded["url"] = url
            recorded["headers"] = kwargs.get("headers", {})
            recorded["json"] = kwargs.get("json")
            recorded["timeout"] = kwargs.get("timeout")
        return response

    monkeypatch.setattr("app.providers.ollama_client.httpx.post", handler)


def patch_get(monkeypatch, response, recorded=None):
    def handler(url, **kwargs):
        if recorded is not None:
            recorded["url"] = url
            recorded["headers"] = kwargs.get("headers", {})
        return response

    monkeypatch.setattr("app.providers.ollama_client.httpx.get", handler)


def patch_stream(monkeypatch, response, recorded=None):
    def handler(method, url, **kwargs):
        if recorded is not None:
            recorded["url"] = url
            recorded["json"] = kwargs.get("json")
        return response

    monkeypatch.setattr("app.providers.ollama_client.httpx.stream", handler)


class _SpyAsyncClient(httpx.AsyncClient):
    def __init__(self, handler, *args, **kwargs):
        self.init_kwargs = dict(kwargs)
        kwargs.pop("proxy", None)
        kwargs.pop("trust_env", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        super().__init__(*args, **kwargs)

    async def post(self, url, **kwargs):
        return await super().post(url, **kwargs)

    def stream(self, method, url, **kwargs):
        return super().stream(method, url, **kwargs)


def install_async_client(monkeypatch, handler):
    def factory(*args, **kwargs):
        return _SpyAsyncClient(handler, **kwargs)

    monkeypatch.setattr(
        "app.providers.ollama_client.httpx.AsyncClient",
        factory,
    )


def request_json(request):
    return json.loads(request.content)


def request_url(request):
    return str(request.url)


class TestRuntimeRegistry:
    def test_ollama_is_runtime_ready(self):
        assert "ollama" in RUNTIME_READY

    def test_client_registry_resolves_ollama(self):
        registry = ClientRegistry()

        assert isinstance(registry.get("ollama"), OllamaClient)
        assert isinstance(registry.get("Ollama"), OllamaClient)

    def test_factory_builds_keyless_ollama_provider(self, monkeypatch):
        monkeypatch.setattr(
            "app.providers.ollama_client.OllamaClient.list_models",
            lambda self, provider: ["llama3", "mistral"],
        )

        provider = build_runtime_provider(ollama_defn())

        assert provider.name == "Ollama"
        assert provider.id == "ollama"
        assert provider.identity() == "ollama"
        assert provider.base_url == "http://localhost:11434"
        assert provider.enabled is True
        assert provider.requires_api_key is False
        assert provider.has_api_key() is False
        assert provider.priority == 2
        assert provider.health_endpoint == "/api/tags"
        assert provider.models == ["llama3", "mistral"]

    def test_factory_applies_ollama_model_priority(self, monkeypatch):
        monkeypatch.setattr(
            "app.providers.ollama_client.OllamaClient.list_models",
            lambda self, provider: ["llama3", "mistral", "other"],
        )
        monkeypatch.setattr(settings, "ollama_model_priority", ["mistral"])

        provider = build_runtime_provider(ollama_defn())

        assert provider.priority_models == ["mistral"]
        assert provider.models == ["mistral", "llama3", "other"]


class TestConfig:
    def test_ollama_model_priority_defaults_empty(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_MODEL_PRIORITY", raising=False)
        cfg = Settings()

        assert cfg.ollama_model_priority == []

    def test_ollama_model_priority_parsed_from_env(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_MODEL_PRIORITY", "llama3,mistral")
        cfg = Settings()

        assert cfg.ollama_model_priority == ["llama3", "mistral"]


class TestKeylessAuth:
    def test_key_check_requires_no_key(self):
        ok, message = OllamaClient().key_check(make_provider())

        assert ok is None
        assert message == "no api key required"

    def test_chat_sends_no_bearer_header(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(
                200, {"message": {"role": "assistant", "content": "Yo"}}
            ),
            recorded,
        )

        OllamaClient().chat(make_provider(), "m", "hi")

        assert recorded["headers"] == {"Content-Type": "application/json"}
        assert "Authorization" not in recorded["headers"]

    def test_chat_messages_sends_no_bearer_header(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(
                200,
                {
                    "model": "llama3",
                    "message": {"role": "assistant", "content": "Yo"},
                },
            ),
            recorded,
        )

        OllamaClient().chat_messages(
            make_provider(), {"model": "m", "messages": []}
        )

        assert "Authorization" not in recorded["headers"]

    def test_list_models_sends_no_auth_header(self, monkeypatch):
        recorded = {}
        patch_get(
            monkeypatch,
            FakeResponse(200, {"models": [{"name": "llama3"}]}),
            recorded,
        )

        OllamaClient().list_models(make_provider())

        assert recorded["url"] == "http://localhost:11434/api/tags"
        assert recorded["headers"] == {}
        assert "Authorization" not in recorded["headers"]

    def test_health_connectivity_sends_no_bearer(self, monkeypatch):
        recorded = {}

        def handler(url, **kwargs):
            recorded["url"] = url
            recorded["headers"] = kwargs.get("headers", {})
            return httpx.Response(
                200, json={"models": [{"name": "llama3"}]}, request=httpx.Request("GET", url)
            )

        monkeypatch.setattr(
            "app.services.health_checker.httpx.get", handler
        )
        monkeypatch.setattr(
            "app.providers.ollama_client.OllamaClient.probe_model",
            lambda self, provider, model: SimpleNamespace(
                healthy=True, latency_ms=1, status_code=200, error=""
            ),
        )

        provider = ollama_defn().build_provider()
        provider.models = ["llama3"]

        HealthChecker().check(provider)

        assert recorded["url"] == "http://localhost:11434/api/tags"
        assert recorded["headers"] == {}


class TestChatSync:
    def test_chat_returns_message_content(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(
                200, {"message": {"role": "assistant", "content": "Yo"}}
            ),
            recorded,
        )

        result = OllamaClient().chat(make_provider(), "m", "hi")

        assert result == "Yo"
        assert recorded["url"] == "http://localhost:11434/api/chat"
        assert recorded["json"]["model"] == "m"
        assert recorded["json"]["stream"] is False
        assert recorded["json"]["messages"] == [
            {"role": "user", "content": "hi"}
        ]

    def test_chat_maps_generation_params_into_options(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(
                200, {"message": {"role": "assistant", "content": "x"}}
            ),
            recorded,
        )

        OllamaClient().chat(
            make_provider(),
            "m",
            "hi",
            temperature=0.6,
            top_p=0.8,
            max_tokens=150,
            stop="END",
            frequency_penalty=0.5,
            presence_penalty=0.5,
        )

        options = recorded["json"]["options"]
        assert options["temperature"] == 0.6
        assert options["top_p"] == 0.8
        assert options["num_predict"] == 150
        assert options["stop"] == ["END"]
        assert "frequency_penalty" not in options
        assert "presence_penalty" not in options

    def test_chat_raises_provider_http_error(self, monkeypatch):
        patch_post(
            monkeypatch,
            FakeResponse(
                404,
                json_data=None,
                text="model not found",
                headers={"Retry-After": "2"},
            ),
        )

        with pytest.raises(ProviderHTTPError) as exc:
            OllamaClient().chat(make_provider(), "m", "hi")

        assert exc.value.status_code == 404
        assert exc.value.retry_after == 2.0

    def test_chat_raises_provider_timeout(self, monkeypatch):
        def handler(url, **kwargs):
            raise httpx.ReadTimeout("boom")

        monkeypatch.setattr(
            "app.providers.ollama_client.httpx.post", handler
        )

        with pytest.raises(ProviderTimeout):
            OllamaClient().chat(make_provider(), "m", "hi")

    def test_chat_stream_yields_ndjson_deltas(self, monkeypatch):
        recorded = {}
        body = (
            '{"message": {"role": "assistant", "content": "Hel"}}\n'
            '{"message": {"role": "assistant", "content": "lo"}}\n'
            '{"message": {"role": "assistant", "content": ""}, "done": true}\n'
        )
        patch_stream(monkeypatch, FakeStreamResponse(body.splitlines()), recorded)

        chunks = list(
            OllamaClient().chat_stream(make_provider(), "m", "hi")
        )

        assert chunks == ["Hel", "lo"]
        assert recorded["json"]["stream"] is True

    def test_chat_stream_raises_on_inline_error(self, monkeypatch):
        body = '{"error": "model not found"}\n'
        patch_stream(monkeypatch, FakeStreamResponse(body.splitlines()))

        with pytest.raises(ProviderHTTPError) as exc:
            list(OllamaClient().chat_stream(make_provider(), "m", "hi"))

        assert exc.value.status_code == 0
        assert "model not found" in exc.value.message

    def test_chat_stream_yields_content_before_error(self, monkeypatch):
        body = (
            '{"message": {"role": "assistant", "content": "partial"}}\n'
            '{"error": "interrupted"}\n'
        )
        patch_stream(monkeypatch, FakeStreamResponse(body.splitlines()))

        with pytest.raises(ProviderHTTPError):
            list(OllamaClient().chat_stream(make_provider(), "m", "hi"))


class TestMessagesSync:
    def test_chat_messages_returns_openai_shaped_response(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(
                200,
                {
                    "model": "llama3",
                    "message": {"role": "assistant", "content": "hello"},
                    "prompt_eval_count": 5,
                    "eval_count": 3,
                },
            ),
            recorded,
        )

        response = OllamaClient().chat_messages(
            make_provider(),
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 100,
            },
        )

        assert recorded["json"]["options"]["num_predict"] == 100
        assert recorded["json"]["stream"] is False

        choice = response["choices"][0]
        assert choice["index"] == 0
        assert choice["message"] == {"role": "assistant", "content": "hello"}
        assert choice["finish_reason"] == "stop"
        assert response["model"] == "llama3"
        assert response["usage"] == {
            "prompt_tokens": 5,
            "completion_tokens": 3,
            "total_tokens": 8,
        }

    def test_chat_messages_translates_tool_calls_to_json_string(self, monkeypatch):
        patch_post(
            monkeypatch,
            FakeResponse(
                200,
                {
                    "model": "llama3",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "get_weather",
                                    "arguments": {"city": "Paris"},
                                }
                            }
                        ],
                    },
                },
            ),
        )

        response = OllamaClient().chat_messages(
            make_provider(), {"model": "m", "messages": []}
        )

        tool_call = response["choices"][0]["message"]["tool_calls"][0]
        assert tool_call["type"] == "function"
        assert tool_call["function"]["name"] == "get_weather"
        assert json.loads(tool_call["function"]["arguments"]) == {
            "city": "Paris"
        }
        assert tool_call["id"].startswith("call_")

    def test_chat_messages_drops_unsupported_params(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(
                200,
                {"model": "m", "message": {"role": "assistant", "content": "x"}},
            ),
            recorded,
        )

        OllamaClient().chat_messages(
            make_provider(),
            {
                "model": "m",
                "messages": [],
                "frequency_penalty": 0.5,
                "presence_penalty": 0.5,
                "temperature": 0.2,
            },
        )

        assert recorded["json"]["options"]["temperature"] == 0.2
        assert "frequency_penalty" not in recorded["json"]
        assert "presence_penalty" not in recorded["json"]


class TestStreamMessagesSync:
    def test_chat_stream_messages_yields_translated_chunks(self, monkeypatch):
        body = (
            '{"model": "llama3", "message": {"role": "assistant", "content": "Hel"}}\n'
            '{"message": {"role": "assistant", "content": "lo"}}\n'
            '{"message": {"role": "assistant", "content": ""}, "done": true, '
            '"prompt_eval_count": 5, "eval_count": 3}\n'
        )
        patch_stream(monkeypatch, FakeStreamResponse(body.splitlines()))

        chunks = list(
            OllamaClient().chat_stream_messages(
                make_provider(), {"model": "m", "messages": []}
            )
        )

        assert chunks[0]["choices"][0]["delta"]["content"] == "Hel"
        assert chunks[1]["choices"][0]["delta"]["content"] == "lo"
        finish = chunks[2]
        assert finish["choices"][0]["delta"] == {}
        assert finish["choices"][0]["finish_reason"] == "stop"
        usage = chunks[3]
        assert usage["choices"] == []
        assert usage["usage"]["total_tokens"] == 8

    def test_chat_stream_messages_translates_done_tool_calls(self, monkeypatch):
        body = (
            '{"message": {"role": "assistant", "content": "", '
            '"tool_calls": [{"function": {"name": "f", '
            '"arguments": {"a": 1}}}]}, "done": true}\n'
        )
        patch_stream(monkeypatch, FakeStreamResponse(body.splitlines()))

        chunks = list(
            OllamaClient().chat_stream_messages(
                make_provider(), {"model": "m", "messages": []}
            )
        )

        tool_chunk = chunks[0]
        assert tool_chunk["choices"][0]["delta"]["tool_calls"][0]["function"][
            "name"
        ] == "f"
        assert chunks[1]["choices"][0]["finish_reason"] == "stop"

    def test_chat_stream_messages_raises_on_inline_error(self, monkeypatch):
        body = '{"error": "model not found"}\n'
        patch_stream(monkeypatch, FakeStreamResponse(body.splitlines()))

        with pytest.raises(ProviderHTTPError) as exc:
            list(
                OllamaClient().chat_stream_messages(
                    make_provider(), {"model": "m", "messages": []}
                )
            )

        assert exc.value.status_code == 0
        assert "model not found" in exc.value.message

    def test_chat_stream_messages_skips_malformed_lines(self, monkeypatch):
        body = (
            "not-json\n"
            '{"message": {"role": "assistant", "content": "A"}}\n'
        )
        patch_stream(monkeypatch, FakeStreamResponse(body.splitlines()))

        chunks = list(
            OllamaClient().chat_stream_messages(
                make_provider(), {"model": "m", "messages": []}
            )
        )

        assert len(chunks) == 1
        assert chunks[0]["choices"][0]["delta"]["content"] == "A"


class TestAsyncSurface:
    @pytest.mark.asyncio
    async def test_achat_messages_returns_openai_shaped_response(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["url"] = request_url(request)
            seen["json"] = request_json(request)
            return httpx.Response(
                200,
                json={
                    "model": "llama3",
                    "message": {"role": "assistant", "content": "hello"},
                    "prompt_eval_count": 5,
                    "eval_count": 3,
                },
                request=request,
            )

        install_async_client(monkeypatch, handler)

        response = await OllamaClient().achat_messages(
            make_provider(),
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert seen["url"] == "http://localhost:11434/api/chat"
        assert seen["json"]["model"] == "m"
        assert seen["json"]["stream"] is False
        assert response["choices"][0]["message"]["content"] == "hello"
        assert response["usage"]["total_tokens"] == 8

    @pytest.mark.asyncio
    async def test_achat_stream_messages_yields_translated_chunks(self, monkeypatch):
        body = (
            '{"model": "llama3", "message": {"role": "assistant", "content": "Hel"}}\n'
            '{"message": {"role": "assistant", "content": "lo"}}\n'
            '{"message": {"role": "assistant", "content": ""}, "done": true, '
            '"prompt_eval_count": 5, "eval_count": 3}\n'
        )

        def handler(request):
            assert request_json(request)["stream"] is True
            return httpx.Response(200, text=body, request=request)

        install_async_client(monkeypatch, handler)

        stream = OllamaClient().achat_stream_messages(
            make_provider(),
            {"model": "m", "messages": [], "stream": True},
        )
        chunks = [chunk async for chunk in stream]

        assert chunks[0]["choices"][0]["delta"]["content"] == "Hel"
        assert chunks[1]["choices"][0]["delta"]["content"] == "lo"
        assert chunks[2]["choices"][0]["finish_reason"] == "stop"
        assert chunks[3]["usage"]["total_tokens"] == 8

    @pytest.mark.asyncio
    async def test_achat_stream_messages_raises_on_inline_error(self, monkeypatch):
        body = '{"error": "model not found"}\n'

        def handler(request):
            return httpx.Response(200, text=body, request=request)

        install_async_client(monkeypatch, handler)

        stream = OllamaClient().achat_stream_messages(
            make_provider(), {"model": "m", "messages": []}
        )

        with pytest.raises(ProviderHTTPError):
            async for _ in stream:
                pass

    @pytest.mark.asyncio
    async def test_achat_stream_raises_on_inline_error(self, monkeypatch):
        body = '{"error": "model not found"}\n'

        def handler(request):
            return httpx.Response(200, text=body, request=request)

        install_async_client(monkeypatch, handler)

        stream = OllamaClient().achat_stream(make_provider(), "m", "hi")

        with pytest.raises(ProviderHTTPError) as exc:
            async for _ in stream:
                pass

        assert exc.value.status_code == 0
        assert "model not found" in exc.value.message


class TestHealthCheck:
    def test_health_check_healthy(self, monkeypatch):
        def get_handler(url, **kwargs):
            return httpx.Response(
                200, json={"models": [{"name": "llama3"}]},
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(
            "app.services.health_checker.httpx.get", get_handler
        )
        monkeypatch.setattr(
            "app.providers.ollama_client.OllamaClient.probe_model",
            lambda self, provider, model: SimpleNamespace(
                healthy=True, latency_ms=1, status_code=200, error=""
            ),
        )

        provider = ollama_defn().build_provider()
        provider.models = ["llama3"]

        report = HealthChecker().check(provider)

        assert report.connectivity is True
        assert report.status == "healthy"
        assert report.healthy_models == ["llama3"]

    def test_health_check_unavailable_when_connection_refused(self, monkeypatch):
        def get_handler(url, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(
            "app.services.health_checker.httpx.get", get_handler
        )

        provider = ollama_defn().build_provider()
        provider.models = ["llama3"]

        report = HealthChecker().check(provider)

        assert report.connectivity is False
        assert report.status == "unavailable"
        assert report.models == []

    def test_deep_check_probes_every_chat_model(self, monkeypatch):
        def get_handler(url, **kwargs):
            return httpx.Response(
                200, json={"models": [{"name": "llama3"}]},
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(
            "app.services.health_checker.httpx.get", get_handler
        )
        monkeypatch.setattr(
            "app.providers.ollama_client.OllamaClient.probe_model",
            lambda self, provider, model: SimpleNamespace(
                healthy=True, latency_ms=1, status_code=200, error=""
            ),
        )

        provider = ollama_defn().build_provider()
        provider.models = ["llama3", "mistral"]

        report = HealthChecker().check(provider, deep=True)

        assert report.healthy_models == ["llama3", "mistral"]


class TestFailover:
    def test_chat_service_fails_over_from_ollama(self, monkeypatch):
        service = ChatService()

        class FakeOllamaClient:
            def chat(self, provider, model, message, **kwargs):
                raise ProviderHTTPError(503, "ollama down")

        class FakeBackupClient:
            def chat(self, provider, model, message, **kwargs):
                return "backup response"

        clients = {"ollama": FakeOllamaClient(), "Backup": FakeBackupClient()}
        monkeypatch.setattr(service.registry, "get", lambda key: clients[key])

        ollama = ollama_defn().build_provider()
        backup = Provider(name="Backup", base_url="http://localhost:9000/v1")

        result = service.chat_across(
            [(ollama, "llama3"), (backup, "m2")], "hello"
        )

        assert result["success"] is True
        assert result["provider"] == "Backup"
        assert result["response"] == "backup response"

    @pytest.mark.asyncio
    async def test_async_chat_service_fails_over_from_ollama(self, monkeypatch):
        service = AsyncChatService()

        class FakeOllamaClient:
            async def achat(self, provider, model, message, **kwargs):
                raise ProviderHTTPError(503, "ollama down")

        class FakeBackupClient:
            async def achat(self, provider, model, message, **kwargs):
                return "backup response"

        clients = {"ollama": FakeOllamaClient(), "Backup": FakeBackupClient()}
        monkeypatch.setattr(service.registry, "get", lambda key: clients[key])

        ollama = ollama_defn().build_provider()
        backup = Provider(name="Backup", base_url="http://localhost:9000/v1")

        result = await service.achat_across(
            [(ollama, "llama3"), (backup, "m2")], "hello"
        )

        assert result["success"] is True
        assert result["provider"] == "Backup"
        assert result["response"] == "backup response"


class TestReload:
    def test_reload_enables_ollama_and_applies_model_priority(self, monkeypatch):
        import app.services.reload as reload_module
        from app.services.reload import reload_config

        fields = [
            field
            for field in reload_module._RELOADABLE_FIELDS
            if hasattr(settings, field)
        ]
        snapshot = {field: getattr(settings, field) for field in fields}

        class FakeProviderManager:
            def __init__(self):
                self.providers = {}

            def get(self, name):
                return self.providers.get(name)

            def register(self, provider):
                self.providers[provider.identity()] = provider

        class FakeRefreshable:
            def refresh(self):
                pass

            def refresh_thresholds(self):
                pass

            def refresh_scorer(self):
                pass

            def set_ewma_alpha(self, alpha):
                pass

            def set_alpha(self, alpha):
                pass

            def set_min_samples(self, n):
                pass

            def set_retention_limit(self, n):
                pass

            def set_retention_days(self, n):
                pass

        relay = SimpleNamespace(
            provider_manager=FakeProviderManager(),
            routing=FakeRefreshable(),
            health_store=FakeRefreshable(),
            candidate_builder=FakeRefreshable(),
            telemetry=FakeRefreshable(),
            quality_store=FakeRefreshable(),
            decision_engine=FakeRefreshable(),
            state_flusher=FakeRefreshable(),
        )

        provider = Provider(
            id="ollama",
            name="Ollama",
            base_url="http://localhost:11434",
            enabled=False,
            models=["llama3", "mistral"],
        )
        relay.provider_manager.register(provider)

        try:
            result = reload_config(
                relay,
                env=SimpleNamespace(
                    ollama_enabled=True,
                    ollama_model_priority=["mistral"],
                ),
            )

            assert result["reloaded"] is True
            assert "ollama_enabled" in result["applied"]
            assert "ollama_model_priority" in result["applied"]
            assert provider.enabled is True
            assert provider.priority_models == ["mistral"]
            assert provider.models[0] == "mistral"
            assert settings.ollama_model_priority == ["mistral"]
        finally:
            for field, value in snapshot.items():
                setattr(settings, field, value)

    def test_ollama_spec_is_reloadable(self):
        import app.services.reload as reload_module

        prefixes = {spec["prefix"] for spec in reload_module._PROVIDER_SPECS}

        assert "ollama" in prefixes
        assert "ollama_enabled" in reload_module._RELOADABLE_FIELDS
        assert "ollama_model_priority" in reload_module._RELOADABLE_FIELDS


class TestWizard:
    def test_wizard_no_longer_defers_ollama(self, monkeypatch):
        from app.setup import wizard

        notices = []
        monkeypatch.setattr(
            "app.setup.wizard.setup_state.write_setup_state",
            lambda *args, **kwargs: None,
        )

        result = wizard._finish(
            SimpleNamespace(notice=notices.append),
            configured={"ollama": True},
            completed=True,
        )

        assert result.usable is True
        assert result.deferred == []
        assert not any("not wired" in notice for notice in notices)
