"""
Focused runtime tests for Anthropic (P4.2.2).

Covers the full runtime surface Anthropic needs to join RUNTIME_READY:
registry promotion and registry-driven factory construction, config
loading, x-api-key auth, sync/async single-message and full-payload chat,
SSE streaming translation, health connectivity via the client's
connectivity probe, failover through the chat services, hot reload, and
wizard deferral.
"""

import json
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings, settings
from app.providers.anthropic_client import AnthropicClient
from app.providers.base import Provider
from app.providers.exceptions import ProviderHTTPError, ProviderTimeout
from app.providers.factory import build_runtime_provider
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


def make_provider(base_url="https://api.anthropic.com/v1"):
    return Provider(
        name="Anthropic",
        base_url=base_url,
        api_key="sk-test",
    )


def anthropic_defn():
    return PROVIDER_REGISTRY["anthropic"]


def patch_post(monkeypatch, response, recorded=None):
    def handler(url, **kwargs):
        if recorded is not None:
            recorded["url"] = url
            recorded["headers"] = kwargs.get("headers", {})
            recorded["json"] = kwargs.get("json")
            recorded["timeout"] = kwargs.get("timeout")
            recorded["proxy"] = kwargs.get("proxy")
        return response

    monkeypatch.setattr("app.providers.anthropic_client.httpx.post", handler)


def patch_get(monkeypatch, response, recorded=None):
    def handler(url, **kwargs):
        if recorded is not None:
            recorded["url"] = url
            recorded["headers"] = kwargs.get("headers", {})
        return response

    monkeypatch.setattr("app.providers.anthropic_client.httpx.get", handler)


def patch_stream(monkeypatch, response, recorded=None):
    def handler(method, url, **kwargs):
        if recorded is not None:
            recorded["url"] = url
            recorded["json"] = kwargs.get("json")
        return response

    monkeypatch.setattr(
        "app.providers.anthropic_client.httpx.stream", handler
    )


class _SpyAsyncClient(httpx.AsyncClient):
    def __init__(self, handler, *args, **kwargs):
        self.init_kwargs = dict(kwargs)
        kwargs.pop("proxy", None)
        kwargs.pop("trust_env", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        super().__init__(*args, **kwargs)


def install_async_client(monkeypatch, handler):
    def factory(*args, **kwargs):
        return _SpyAsyncClient(handler, **kwargs)

    monkeypatch.setattr(
        "app.providers.anthropic_client.httpx.AsyncClient",
        factory,
    )


def request_json(request):
    return json.loads(request.content)


def request_url(request):
    return str(request.url)


class TestRuntimeRegistry:
    def test_anthropic_is_runtime_ready(self):
        assert "anthropic" in RUNTIME_READY

    def test_client_registry_resolves_anthropic(self):
        registry = ClientRegistry()

        assert isinstance(registry.get("anthropic"), AnthropicClient)
        assert isinstance(registry.get("Anthropic"), AnthropicClient)

    def test_factory_builds_anthropic_provider(self, monkeypatch):
        monkeypatch.setattr(
            "app.providers.anthropic_client.AnthropicClient.list_models",
            lambda self, provider: ["claude-3-5-sonnet", "claude-4"],
        )
        monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")

        provider = build_runtime_provider(anthropic_defn())

        assert provider.name == "Anthropic"
        assert provider.id == "anthropic"
        assert provider.identity() == "anthropic"
        assert provider.base_url == "https://api.anthropic.com/v1"
        assert provider.enabled is True
        assert provider.requires_api_key is True
        assert provider.has_api_key() is True
        assert provider.priority == 8
        assert provider.health_endpoint == "/models"
        assert provider.models == ["claude-3-5-sonnet", "claude-4"]

    def test_factory_applies_anthropic_model_priority(self, monkeypatch):
        monkeypatch.setattr(
            "app.providers.anthropic_client.AnthropicClient.list_models",
            lambda self, provider: ["claude-3-5-sonnet", "claude-4", "other"],
        )
        monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
        monkeypatch.setattr(settings, "anthropic_model_priority", ["claude-4"])

        provider = build_runtime_provider(anthropic_defn())

        assert provider.priority_models == ["claude-4"]
        assert provider.models == ["claude-4", "claude-3-5-sonnet", "other"]

    def test_factory_without_key_skips_discovery(self, monkeypatch):
        monkeypatch.setattr(settings, "anthropic_api_key", "")

        provider = build_runtime_provider(anthropic_defn())

        assert provider.models == []
        assert provider.priority_models == []

    def test_built_provider_appears_in_enabled_and_v1_candidates(
        self, monkeypatch
    ):
        from app.services.provider_manager import ProviderManager

        monkeypatch.setattr(
            "app.providers.anthropic_client.AnthropicClient.list_models",
            lambda self, provider: ["claude-3", "claude-4"],
        )
        monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")

        provider = build_runtime_provider(anthropic_defn())
        manager = ProviderManager()
        manager.register(provider)

        assert any(
            p.identity() == "anthropic" for p in manager.enabled()
        )

        candidates = [
            (p, "claude-3")
            for p in manager.all()
            if "claude-3" in p.models
        ]
        assert any(p.identity() == "anthropic" for p, _ in candidates)


class TestConfig:
    def test_anthropic_model_priority_defaults_empty(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_MODEL_PRIORITY", raising=False)
        cfg = Settings()

        assert cfg.anthropic_model_priority == []

    def test_anthropic_model_priority_parsed_from_env(self, monkeypatch):
        monkeypatch.setenv(
            "ANTHROPIC_MODEL_PRIORITY", "claude-4,claude-3-5-sonnet"
        )
        cfg = Settings()

        assert cfg.anthropic_model_priority == ["claude-4", "claude-3-5-sonnet"]


class TestAuthHeaders:
    def test_chat_sends_x_api_key_headers(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(
                200, {"content": [{"type": "text", "text": "Yo"}]}
            ),
            recorded,
        )

        AnthropicClient().chat(make_provider(), "m", "hi")

        headers = recorded["headers"]
        assert headers["x-api-key"] == "sk-test"
        assert headers["anthropic-version"] == "2023-06-01"
        assert headers["Content-Type"] == "application/json"
        assert "Authorization" not in headers

    def test_chat_messages_sends_x_api_key_headers(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(
                200, {"content": [{"type": "text", "text": "Yo"}]}
            ),
            recorded,
        )

        AnthropicClient().chat_messages(
            make_provider(), {"model": "m", "messages": []}
        )

        headers = recorded["headers"]
        assert headers["x-api-key"] == "sk-test"
        assert headers["anthropic-version"] == "2023-06-01"
        assert "Authorization" not in headers

    def test_list_models_sends_x_api_key_headers(self, monkeypatch):
        recorded = {}
        patch_get(
            monkeypatch,
            FakeResponse(200, {"data": [{"id": "claude-3"}]}),
            recorded,
        )

        AnthropicClient().list_models(make_provider())

        assert recorded["url"] == "https://api.anthropic.com/v1/models"
        headers = recorded["headers"]
        assert headers["x-api-key"] == "sk-test"
        assert headers["anthropic-version"] == "2023-06-01"


class TestChatSync:
    def test_chat_returns_concatenated_text(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(
                200,
                {
                    "content": [
                        {"type": "text", "text": "Hello "},
                        {"type": "text", "text": "world"},
                    ]
                },
            ),
            recorded,
        )

        result = AnthropicClient().chat(make_provider(), "m", "hi")

        assert result == "Hello world"
        assert recorded["url"] == "https://api.anthropic.com/v1/messages"
        assert recorded["json"]["model"] == "m"
        assert recorded["json"]["max_tokens"] == 512
        assert recorded["json"]["stream"] is False
        assert recorded["json"]["messages"] == [
            {"role": "user", "content": "hi"}
        ]

    def test_chat_maps_generation_params(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(
                200, {"content": [{"type": "text", "text": "x"}]}
            ),
            recorded,
        )

        AnthropicClient().chat(
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

        body = recorded["json"]
        assert body["temperature"] == 0.6
        assert body["top_p"] == 0.8
        assert body["max_tokens"] == 150
        assert body["stop_sequences"] == ["END"]
        assert "frequency_penalty" not in body
        assert "presence_penalty" not in body

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
            AnthropicClient().chat(make_provider(), "m", "hi")

        assert exc.value.status_code == 404
        assert exc.value.retry_after == 2.0

    def test_chat_raises_provider_timeout(self, monkeypatch):
        def handler(url, **kwargs):
            raise httpx.ReadTimeout("boom")

        monkeypatch.setattr(
            "app.providers.anthropic_client.httpx.post", handler
        )

        with pytest.raises(ProviderTimeout):
            AnthropicClient().chat(make_provider(), "m", "hi")

    def test_chat_stream_yields_sse_deltas(self, monkeypatch):
        recorded = {}
        body = (
            'data: {"type": "content_block_delta", "index": 0, '
            '"delta": {"type": "text_delta", "text": "Hel"}}\n'
            'data: {"type": "content_block_delta", "index": 0, '
            '"delta": {"type": "text_delta", "text": "lo"}}\n'
            'data: {"type": "message_stop"}\n'
        )
        patch_stream(
            monkeypatch, FakeStreamResponse(body.splitlines()), recorded
        )

        chunks = list(
            AnthropicClient().chat_stream(make_provider(), "m", "hi")
        )

        assert chunks == ["Hel", "lo"]
        assert recorded["json"]["stream"] is True


class TestMessagesSync:
    def test_chat_messages_returns_openai_shaped_response(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(
                200,
                {
                    "model": "claude-3",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "hello"}],
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                },
            ),
            recorded,
        )

        response = AnthropicClient().chat_messages(
            make_provider(),
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 100,
            },
        )

        assert recorded["json"]["max_tokens"] == 100
        assert recorded["json"]["stream"] is False

        choice = response["choices"][0]
        assert choice["index"] == 0
        assert choice["message"] == {"role": "assistant", "content": "hello"}
        assert choice["finish_reason"] == "stop"
        assert response["model"] == "claude-3"
        assert response["usage"] == {
            "prompt_tokens": 5,
            "completion_tokens": 3,
            "total_tokens": 8,
        }

    def test_chat_messages_translates_tool_use_to_json_string(
        self, monkeypatch
    ):
        patch_post(
            monkeypatch,
            FakeResponse(
                200,
                {
                    "model": "claude-3",
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_01",
                            "name": "get_weather",
                            "input": {"city": "Paris"},
                        }
                    ],
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                },
            ),
        )

        response = AnthropicClient().chat_messages(
            make_provider(), {"model": "m", "messages": []}
        )

        message = response["choices"][0]["message"]
        assert message["content"] is None
        tool_call = message["tool_calls"][0]
        assert tool_call["type"] == "function"
        assert tool_call["id"] == "toolu_01"
        assert tool_call["function"]["name"] == "get_weather"
        assert json.loads(tool_call["function"]["arguments"]) == {
            "city": "Paris"
        }
        assert response["choices"][0]["finish_reason"] == "tool_calls"

    def test_chat_messages_translates_openai_payload(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(
                200, {"content": [{"type": "text", "text": "ok"}]}
            ),
            recorded,
        )

        payload = {
            "model": "claude-3",
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city": "Paris"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "22C",
                },
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "and now?"}],
                },
            ],
            "temperature": 0.6,
            "top_p": 0.9,
            "max_tokens": 200,
            "stop": ["END"],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    },
                }
            ],
            "tool_choice": "auto",
            "user": "u-1",
            "frequency_penalty": 0.5,
            "presence_penalty": 0.5,
        }

        AnthropicClient().chat_messages(make_provider(), payload)

        body = recorded["json"]
        assert body["model"] == "claude-3"
        assert body["max_tokens"] == 200
        assert body["stream"] is False
        assert body["system"] == "You are helpful."
        assert body["temperature"] == 0.6
        assert body["top_p"] == 0.9
        assert body["stop_sequences"] == ["END"]
        assert body["tool_choice"] == {"type": "auto"}
        assert body["metadata"] == {"user_id": "u-1"}
        assert "frequency_penalty" not in body
        assert "presence_penalty" not in body
        assert body["tools"] == [
            {
                "name": "get_weather",
                "description": "Get weather",
                "input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            }
        ]
        assert body["messages"] == [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "get_weather",
                        "input": {"city": "Paris"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_1",
                        "content": "22C",
                    }
                ],
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": "and now?"}],
            },
        ]


class TestStreamMessagesSync:
    def test_chat_stream_messages_yields_translated_chunks(self, monkeypatch):
        body = (
            'data: {"type": "message_start", "message": {"usage": '
            '{"input_tokens": 5, "output_tokens": 0}}}\n'
            'data: {"type": "content_block_start", "index": 0, '
            '"content_block": {"type": "text", "text": ""}}\n'
            'data: {"type": "content_block_delta", "index": 0, '
            '"delta": {"type": "text_delta", "text": "Hel"}}\n'
            'data: {"type": "content_block_delta", "index": 0, '
            '"delta": {"type": "text_delta", "text": "lo"}}\n'
            'data: {"type": "content_block_stop", "index": 0}\n'
            'data: {"type": "message_delta", '
            '"delta": {"stop_reason": "end_turn", "stop_sequence": null}, '
            '"usage": {"output_tokens": 3}}\n'
            'data: {"type": "message_stop"}\n'
        )
        patch_stream(
            monkeypatch, FakeStreamResponse(body.splitlines())
        )

        chunks = list(
            AnthropicClient().chat_stream_messages(
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

    def test_chat_stream_messages_translates_tool_use_stream(
        self, monkeypatch
    ):
        body = (
            'data: {"type": "message_start", "message": {"usage": '
            '{"input_tokens": 5, "output_tokens": 0}}}\n'
            'data: {"type": "content_block_start", "index": 0, '
            '"content_block": {"type": "tool_use", "id": "toolu_01", '
            '"name": "get_weather", "input": {}}}\n'
            'data: {"type": "content_block_delta", "index": 0, '
            '"delta": {"type": "input_json_delta", '
            '"partial_json": "{\\"city\\":"}}\n'
            'data: {"type": "content_block_delta", "index": 0, '
            '"delta": {"type": "input_json_delta", '
            '"partial_json": " \\"Paris\\"}"}}\n'
            'data: {"type": "content_block_stop", "index": 0}\n'
            'data: {"type": "message_delta", '
            '"delta": {"stop_reason": "tool_use", "stop_sequence": null}, '
            '"usage": {"output_tokens": 3}}\n'
            'data: {"type": "message_stop"}\n'
        )
        patch_stream(
            monkeypatch, FakeStreamResponse(body.splitlines())
        )

        chunks = list(
            AnthropicClient().chat_stream_messages(
                make_provider(), {"model": "m", "messages": []}
            )
        )

        tool_start = chunks[0]["choices"][0]["delta"]["tool_calls"][0]
        assert tool_start["index"] == 0
        assert tool_start["id"] == "toolu_01"
        assert tool_start["type"] == "function"
        assert tool_start["function"]["name"] == "get_weather"
        assert tool_start["function"]["arguments"] == ""

        assert chunks[1]["choices"][0]["delta"]["tool_calls"][0][
            "function"
        ]["arguments"] == '{"city":'
        assert chunks[2]["choices"][0]["delta"]["tool_calls"][0][
            "function"
        ]["arguments"] == ' "Paris"}'

        assert chunks[3]["choices"][0]["finish_reason"] == "tool_calls"
        assert chunks[4]["usage"]["total_tokens"] == 8

    def test_chat_stream_messages_raises_on_inline_error(self, monkeypatch):
        body = (
            'data: {"type": "error", '
            '"error": {"message": "overloaded"}}\n'
        )
        patch_stream(
            monkeypatch, FakeStreamResponse(body.splitlines())
        )

        with pytest.raises(ProviderHTTPError) as exc:
            list(
                AnthropicClient().chat_stream_messages(
                    make_provider(), {"model": "m", "messages": []}
                )
            )

        assert exc.value.status_code == 0
        assert "overloaded" in exc.value.message

    def test_chat_stream_messages_skips_metadata_lines(self, monkeypatch):
        body = (
            "event: message_start\n"
            "data: not-json\n"
            'data: {"type": "content_block_delta", "index": 0, '
            '"delta": {"type": "text_delta", "text": "A"}}\n'
            'data: {"type": "message_stop"}\n'
        )
        patch_stream(
            monkeypatch, FakeStreamResponse(body.splitlines())
        )

        chunks = list(
            AnthropicClient().chat_stream_messages(
                make_provider(), {"model": "m", "messages": []}
            )
        )

        assert len(chunks) == 1
        assert chunks[0]["choices"][0]["delta"]["content"] == "A"


class TestProxySupport:
    def test_proxy_request_kwargs_matches_openai_compatible(self):
        from app.providers.openai_compat_client import (
            proxy_request_kwargs as occ_proxy,
        )

        provider = make_provider()
        url = "https://api.anthropic.com/v1/messages"

        assert (
            AnthropicClient().proxy_request_kwargs(provider, url)
            == occ_proxy(provider, url)
        )

    def test_chat_forwards_forced_proxy(self, monkeypatch):
        recorded = {}

        def handler(url, **kwargs):
            recorded["proxy"] = kwargs.get("proxy")
            return FakeResponse(
                200, {"content": [{"type": "text", "text": "x"}]}
            )

        monkeypatch.setattr(
            "app.providers.anthropic_client.httpx.post", handler
        )

        provider = Provider(
            name="Anthropic",
            base_url="https://api.anthropic.com/v1",
            api_key="sk-test",
            proxy="http://proxy.local:8080",
        )

        AnthropicClient().chat(provider, "m", "hi")

        assert recorded["proxy"] == "http://proxy.local:8080"


class TestAsyncSurface:
    @pytest.mark.asyncio
    async def test_achat_messages_returns_openai_shaped_response(
        self, monkeypatch
    ):
        seen = {}

        def handler(request):
            seen["url"] = request_url(request)
            seen["json"] = request_json(request)
            return httpx.Response(
                200,
                json={
                    "model": "claude-3",
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "hello"}],
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                },
                request=request,
            )

        install_async_client(monkeypatch, handler)

        response = await AnthropicClient().achat_messages(
            make_provider(),
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert seen["url"] == "https://api.anthropic.com/v1/messages"
        assert seen["json"]["model"] == "m"
        assert seen["json"]["stream"] is False
        assert response["choices"][0]["message"]["content"] == "hello"
        assert response["usage"]["total_tokens"] == 8

    @pytest.mark.asyncio
    async def test_achat_stream_messages_yields_translated_chunks(
        self, monkeypatch
    ):
        body = (
            'data: {"type": "message_start", "message": {"usage": '
            '{"input_tokens": 5, "output_tokens": 0}}}\n'
            'data: {"type": "content_block_delta", "index": 0, '
            '"delta": {"type": "text_delta", "text": "Hel"}}\n'
            'data: {"type": "content_block_delta", "index": 0, '
            '"delta": {"type": "text_delta", "text": "lo"}}\n'
            'data: {"type": "message_delta", '
            '"delta": {"stop_reason": "end_turn", "stop_sequence": null}, '
            '"usage": {"output_tokens": 3}}\n'
            'data: {"type": "message_stop"}\n'
        )

        def handler(request):
            assert request_json(request)["stream"] is True
            return httpx.Response(200, text=body, request=request)

        install_async_client(monkeypatch, handler)

        stream = AnthropicClient().achat_stream_messages(
            make_provider(),
            {"model": "m", "messages": [], "stream": True},
        )
        chunks = [chunk async for chunk in stream]

        assert chunks[0]["choices"][0]["delta"]["content"] == "Hel"
        assert chunks[1]["choices"][0]["delta"]["content"] == "lo"
        assert chunks[2]["choices"][0]["finish_reason"] == "stop"
        assert chunks[3]["usage"]["total_tokens"] == 8

    @pytest.mark.asyncio
    async def test_achat_stream_messages_raises_on_inline_error(
        self, monkeypatch
    ):
        body = (
            'data: {"type": "error", '
            '"error": {"message": "overloaded"}}\n'
        )

        def handler(request):
            return httpx.Response(200, text=body, request=request)

        install_async_client(monkeypatch, handler)

        stream = AnthropicClient().achat_stream_messages(
            make_provider(), {"model": "m", "messages": []}
        )

        with pytest.raises(ProviderHTTPError):
            async for _ in stream:
                pass

    @pytest.mark.asyncio
    async def test_achat_messages_matches_sync_on_mock_response(
        self, monkeypatch
    ):
        response_body = {
            "model": "claude-3",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "hello"}],
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }

        patch_post(
            monkeypatch, FakeResponse(200, response_body), {}
        )
        sync_result = AnthropicClient().chat_messages(
            make_provider(),
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )

        def handler(request):
            return httpx.Response(200, json=response_body, request=request)

        install_async_client(monkeypatch, handler)

        async_result = await AnthropicClient().achat_messages(
            make_provider(),
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert sync_result == async_result

    @pytest.mark.asyncio
    async def test_achat_matches_sync_on_mock_response(self, monkeypatch):
        response_body = {
            "content": [{"type": "text", "text": "Hello world"}]
        }

        patch_post(monkeypatch, FakeResponse(200, response_body), {})
        sync_text = AnthropicClient().chat(make_provider(), "m", "hi")

        def handler(request):
            return httpx.Response(200, json=response_body, request=request)

        install_async_client(monkeypatch, handler)

        async_text = await AnthropicClient().achat(make_provider(), "m", "hi")

        assert sync_text == async_text == "Hello world"


class TestHealthCheck:
    def test_health_check_healthy_with_x_api_key(self, monkeypatch):
        recorded = {}

        def get_handler(url, **kwargs):
            recorded["url"] = url
            recorded["headers"] = kwargs.get("headers", {})
            return httpx.Response(
                200,
                json={"data": [{"id": "claude-3"}]},
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(
            "app.providers.anthropic_client.httpx.get", get_handler
        )
        monkeypatch.setattr(
            "app.providers.anthropic_client.AnthropicClient.probe_model",
            lambda self, provider, model: SimpleNamespace(
                healthy=True, latency_ms=1, status_code=200, error=""
            ),
        )

        provider = anthropic_defn().build_provider(api_key="sk-test")
        provider.models = ["claude-3"]

        report = HealthChecker().check(provider)

        assert report.connectivity is True
        assert report.status == "healthy"
        assert report.healthy_models == ["claude-3"]
        assert recorded["url"] == "https://api.anthropic.com/v1/models"
        headers = recorded["headers"]
        assert headers["x-api-key"] == "sk-test"
        assert headers["anthropic-version"] == "2023-06-01"
        assert "Authorization" not in headers

    def test_health_check_unavailable_when_connection_refused(
        self, monkeypatch
    ):
        def get_handler(url, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(
            "app.providers.anthropic_client.httpx.get", get_handler
        )

        provider = anthropic_defn().build_provider(api_key="sk-test")
        provider.models = ["claude-3"]

        report = HealthChecker().check(provider)

        assert report.connectivity is False
        assert report.status == "unavailable"

    def test_health_check_unavailable_without_key(self, monkeypatch):
        recorded = {}

        def get_handler(url, **kwargs):
            recorded["headers"] = kwargs.get("headers", {})
            return httpx.Response(
                401,
                text="invalid x-api-key",
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(
            "app.providers.anthropic_client.httpx.get", get_handler
        )

        provider = anthropic_defn().build_provider(api_key="")
        provider.models = ["claude-3"]

        report = HealthChecker().check(provider)

        assert report.connectivity is False
        assert report.status == "unavailable"


class TestFailover:
    def test_chat_service_fails_over_from_anthropic(self, monkeypatch):
        service = ChatService()

        class FakeAnthropicClient:
            def chat(self, provider, model, message, **kwargs):
                raise ProviderHTTPError(503, "anthropic down")

        class FakeBackupClient:
            def chat(self, provider, model, message, **kwargs):
                return "backup response"

        clients = {
            "anthropic": FakeAnthropicClient(),
            "Backup": FakeBackupClient(),
        }
        monkeypatch.setattr(service.registry, "get", lambda key: clients[key])

        anthropic = anthropic_defn().build_provider(api_key="sk-test")
        backup = Provider(name="Backup", base_url="http://localhost:9000/v1")

        result = service.chat_across(
            [(anthropic, "claude-3"), (backup, "m2")], "hello"
        )

        assert result["success"] is True
        assert result["provider"] == "Backup"
        assert result["response"] == "backup response"

    @pytest.mark.asyncio
    async def test_async_chat_service_fails_over_from_anthropic(
        self, monkeypatch
    ):
        service = AsyncChatService()

        class FakeAnthropicClient:
            async def achat(self, provider, model, message, **kwargs):
                raise ProviderHTTPError(503, "anthropic down")

        class FakeBackupClient:
            async def achat(self, provider, model, message, **kwargs):
                return "backup response"

        clients = {
            "anthropic": FakeAnthropicClient(),
            "Backup": FakeBackupClient(),
        }
        monkeypatch.setattr(service.registry, "get", lambda key: clients[key])

        anthropic = anthropic_defn().build_provider(api_key="sk-test")
        backup = Provider(name="Backup", base_url="http://localhost:9000/v1")

        result = await service.achat_across(
            [(anthropic, "claude-3"), (backup, "m2")], "hello"
        )

        assert result["success"] is True
        assert result["provider"] == "Backup"
        assert result["response"] == "backup response"


class TestReload:
    def test_reload_enables_anthropic_and_applies_model_priority(
        self, monkeypatch
    ):
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
            id="anthropic",
            name="Anthropic",
            base_url="https://api.anthropic.com/v1",
            api_key="sk-test",
            enabled=False,
            models=["claude-3-5-sonnet", "claude-4"],
        )
        relay.provider_manager.register(provider)

        monkeypatch.setattr(
            "app.providers.anthropic_client.AnthropicClient.list_models",
            lambda self, provider: ["claude-3-5-sonnet", "claude-4"],
        )

        try:
            result = reload_config(
                relay,
                env=SimpleNamespace(
                    anthropic_enabled=True,
                    anthropic_api_key="sk-test",
                    anthropic_model_priority=["claude-4"],
                ),
            )

            assert result["reloaded"] is True
            assert "anthropic_enabled" in result["applied"]
            assert "anthropic_api_key" in result["applied"]
            assert "anthropic_model_priority" in result["applied"]
            assert provider.enabled is True
            assert provider.priority_models == ["claude-4"]
            assert provider.models[0] == "claude-4"
            assert settings.anthropic_model_priority == ["claude-4"]
        finally:
            for field, value in snapshot.items():
                setattr(settings, field, value)

    def test_anthropic_spec_is_reloadable(self):
        import app.services.reload as reload_module

        prefixes = {spec["prefix"] for spec in reload_module._PROVIDER_SPECS}

        assert "anthropic" in prefixes
        assert "anthropic_enabled" in reload_module._RELOADABLE_FIELDS
        assert "anthropic_api_key" in reload_module._RELOADABLE_FIELDS
        assert "anthropic_model_priority" in reload_module._RELOADABLE_FIELDS


class TestWizard:
    def test_wizard_no_longer_defers_anthropic(self, monkeypatch):
        from app.setup import wizard

        notices = []
        monkeypatch.setattr(
            "app.setup.wizard.setup_state.write_setup_state",
            lambda *args, **kwargs: None,
        )

        result = wizard._finish(
            SimpleNamespace(notice=notices.append),
            configured={"anthropic": True},
            completed=True,
        )

        assert result.usable is True
        assert result.deferred == []
        assert not any("not wired" in notice for notice in notices)
