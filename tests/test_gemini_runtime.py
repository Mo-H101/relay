"""
Focused runtime tests for Google Gemini (P4.2.3).

Covers the full runtime surface Gemini needs to join RUNTIME_READY:
registry promotion and registry-driven factory construction, config
loading, query-key auth, sync/async single-message and full-payload chat,
SSE streaming translation, health connectivity via the client's
connectivity probe, failover through the chat services, hot reload, and
wizard deferral.
"""

import json
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings, settings
from app.providers.base import Provider
from app.providers.exceptions import ProviderHTTPError, ProviderTimeout
from app.providers.factory import build_runtime_provider
from app.providers.gemini_client import GeminiClient
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


def make_provider(
    base_url="https://generativelanguage.googleapis.com/v1beta",
    key="sk-test",
):
    return Provider(
        name="Google Gemini",
        base_url=base_url,
        api_key=key,
    )


def gemini_defn():
    return PROVIDER_REGISTRY["gemini"]


def patch_post(monkeypatch, response, recorded=None):
    def handler(url, **kwargs):
        if recorded is not None:
            recorded["url"] = url
            recorded["headers"] = kwargs.get("headers", {})
            recorded["json"] = kwargs.get("json")
            recorded["timeout"] = kwargs.get("timeout")
            recorded["proxy"] = kwargs.get("proxy")
        return response

    monkeypatch.setattr("app.providers.gemini_client.bounded_post", handler)


def patch_get(monkeypatch, response, recorded=None):
    def handler(url, **kwargs):
        if recorded is not None:
            recorded["url"] = url
            recorded["headers"] = kwargs.get("headers", {})
        return response

    monkeypatch.setattr("app.providers.gemini_client.bounded_get", handler)


def patch_stream(monkeypatch, response, recorded=None):
    def handler(method, url, **kwargs):
        if recorded is not None:
            recorded["url"] = url
            recorded["json"] = kwargs.get("json")
        return response

    monkeypatch.setattr(
        "app.providers.gemini_client.bounded_stream", handler
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
        "app.providers.gemini_client.httpx.AsyncClient",
        factory,
    )


def request_json(request):
    return json.loads(request.content)


def request_url(request):
    return str(request.url)


class TestRuntimeRegistry:
    def test_gemini_is_runtime_ready(self):
        assert "gemini" in RUNTIME_READY

    def test_client_registry_resolves_gemini(self):
        registry = ClientRegistry()

        assert isinstance(registry.get("gemini"), GeminiClient)
        assert isinstance(registry.get("Google Gemini"), GeminiClient)

    def test_factory_builds_gemini_provider(self, monkeypatch):
        monkeypatch.setattr(
            "app.providers.gemini_client.GeminiClient.list_models",
            lambda self, provider: ["gemini-pro", "gemini-ultra"],
        )
        monkeypatch.setattr(settings, "gemini_api_key", "sk-test")

        provider = build_runtime_provider(gemini_defn())

        assert provider.name == "Google Gemini"
        assert provider.id == "gemini"
        assert provider.identity() == "gemini"
        assert (
            provider.base_url
            == "https://generativelanguage.googleapis.com/v1beta"
        )
        assert provider.enabled is True
        assert provider.requires_api_key is True
        assert provider.has_api_key() is True
        assert provider.priority == 7
        assert provider.health_endpoint == "/models"
        assert provider.models == ["gemini-pro", "gemini-ultra"]

    def test_factory_applies_gemini_model_priority(self, monkeypatch):
        monkeypatch.setattr(
            "app.providers.gemini_client.GeminiClient.list_models",
            lambda self, provider: ["gemini-pro", "gemini-ultra", "other"],
        )
        monkeypatch.setattr(settings, "gemini_api_key", "sk-test")
        monkeypatch.setattr(
            settings, "gemini_model_priority", ["gemini-ultra"]
        )

        provider = build_runtime_provider(gemini_defn())

        assert provider.priority_models == ["gemini-ultra"]
        assert provider.models == ["gemini-ultra", "gemini-pro", "other"]

    def test_factory_without_key_skips_discovery(self, monkeypatch):
        monkeypatch.setattr(settings, "gemini_api_key", "")

        provider = build_runtime_provider(gemini_defn())

        assert provider.models == []
        assert provider.priority_models == []

    def test_built_provider_appears_in_enabled_and_v1_candidates(
        self, monkeypatch
    ):
        from app.services.provider_manager import ProviderManager

        monkeypatch.setattr(
            "app.providers.gemini_client.GeminiClient.list_models",
            lambda self, provider: ["gemini-pro", "gemini-ultra"],
        )
        monkeypatch.setattr(settings, "gemini_api_key", "sk-test")

        provider = build_runtime_provider(gemini_defn())
        manager = ProviderManager()
        manager.register(provider)

        assert any(
            p.identity() == "gemini" for p in manager.enabled()
        )

        candidates = [
            (p, "gemini-pro")
            for p in manager.all()
            if "gemini-pro" in p.models
        ]
        assert any(p.identity() == "gemini" for p, _ in candidates)


class TestConfig:
    def test_gemini_model_priority_defaults_empty(self, monkeypatch):
        monkeypatch.delenv("GEMINI_MODEL_PRIORITY", raising=False)
        cfg = Settings()

        assert cfg.gemini_model_priority == []

    def test_gemini_model_priority_parsed_from_env(self, monkeypatch):
        monkeypatch.setenv(
            "GEMINI_MODEL_PRIORITY", "gemini-ultra,gemini-pro"
        )
        cfg = Settings()

        assert cfg.gemini_model_priority == ["gemini-ultra", "gemini-pro"]


class TestAuth:
    def test_chat_sends_key_in_header(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(
                200,
                {"candidates": [{"content": {"parts": [{"text": "Yo"}]}}]},
            ),
            recorded,
        )

        GeminiClient().chat(make_provider(), "m", "hi")

        assert recorded["url"].endswith(":generateContent")
        headers = recorded["headers"]
        assert headers["Content-Type"] == "application/json"
        assert headers["x-goog-api-key"] == "sk-test"
        assert "Authorization" not in headers
        assert "x-api-key" not in headers

    def test_chat_messages_sends_key_in_header(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(
                200,
                {"candidates": [{"content": {"parts": [{"text": "Yo"}]}}]},
            ),
            recorded,
        )

        GeminiClient().chat_messages(
            make_provider(), {"model": "m", "messages": []}
        )

        assert recorded["url"].endswith(":generateContent")
        headers = recorded["headers"]
        assert headers["Content-Type"] == "application/json"
        assert headers["x-goog-api-key"] == "sk-test"
        assert "Authorization" not in headers
        assert "x-api-key" not in headers

    def test_list_models_sends_key_in_header(self, monkeypatch):
        recorded = {}
        patch_get(
            monkeypatch,
            FakeResponse(200, {"models": [{"name": "models/gemini-pro"}]}),
            recorded,
        )

        GeminiClient().list_models(make_provider())

        assert (
            recorded["url"]
            == "https://generativelanguage.googleapis.com/v1beta"
            "/models"
        )
        assert recorded["headers"]["x-goog-api-key"] == "sk-test"


class TestChatSync:
    def test_chat_returns_concatenated_text(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(
                200,
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": "Hello "},
                                    {"text": "world"},
                                ]
                            }
                        }
                    ]
                },
            ),
            recorded,
        )

        result = GeminiClient().chat(make_provider(), "m", "hi")

        assert result == "Hello world"
        assert recorded["url"].endswith(":generateContent")
        assert recorded["json"]["contents"] == [
            {"role": "user", "parts": [{"text": "hi"}]}
        ]

    def test_chat_maps_generation_params(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(
                200,
                {"candidates": [{"content": {"parts": [{"text": "x"}]}}]},
            ),
            recorded,
        )

        GeminiClient().chat(
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

        config = recorded["json"]["generationConfig"]
        assert config["temperature"] == 0.6
        assert config["topP"] == 0.8
        assert config["maxOutputTokens"] == 150
        assert config["stopSequences"] == ["END"]
        assert "frequency_penalty" not in config
        assert "presence_penalty" not in config

    def test_chat_raises_provider_http_error(self, monkeypatch):
        patch_post(
            monkeypatch,
            FakeResponse(
                404,
                json_data=None,
                text="model not found sk-test-123",
                headers={"Retry-After": "2"},
            ),
        )

        with pytest.raises(ProviderHTTPError) as exc:
            GeminiClient().chat(make_provider(key="sk-test-123"), "m", "hi")

        assert exc.value.status_code == 404
        assert exc.value.retry_after == 2.0
        assert "sk-test-123" not in exc.value.message
        assert "[REDACTED]" in exc.value.message

    def test_chat_raises_provider_timeout(self, monkeypatch):
        def handler(url, **kwargs):
            raise httpx.ReadTimeout("boom")

        monkeypatch.setattr(
            "app.providers.gemini_client.bounded_post", handler
        )

        with pytest.raises(ProviderTimeout):
            GeminiClient().chat(make_provider(), "m", "hi")

    def test_chat_stream_yields_sse_deltas(self, monkeypatch):
        recorded = {}
        body = (
            'data: {"candidates": [{"content": {"parts": [{"text": "Hel"}]}}]}\n\n'
            'data: {"candidates": [{"content": {"parts": [{"text": "lo"}]}}]}\n\n'
        )
        patch_stream(
            monkeypatch, FakeStreamResponse(body.splitlines()), recorded
        )

        chunks = list(
            GeminiClient().chat_stream(make_provider(), "m", "hi")
        )

        assert chunks == ["Hel", "lo"]
        assert ":streamGenerateContent?alt=sse" in recorded["url"]
        assert recorded["json"]["contents"] == [
            {"role": "user", "parts": [{"text": "hi"}]}
        ]


class TestMessagesSync:
    def test_chat_messages_returns_openai_shaped_response(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(
                200,
                {
                    "model": "gemini-2.5-pro",
                    "candidates": [
                        {
                            "content": {"parts": [{"text": "hello"}]},
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 5,
                        "candidatesTokenCount": 3,
                    },
                },
            ),
            recorded,
        )

        response = GeminiClient().chat_messages(
            make_provider(),
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 100,
            },
        )

        assert recorded["json"]["contents"] == [
            {"role": "user", "parts": [{"text": "hi"}]}
        ]
        assert recorded["json"]["generationConfig"]["maxOutputTokens"] == 100

        choice = response["choices"][0]
        assert choice["index"] == 0
        assert choice["message"] == {"role": "assistant", "content": "hello"}
        assert choice["finish_reason"] == "stop"
        assert response["model"] == "gemini-2.5-pro"
        assert response["usage"] == {
            "prompt_tokens": 5,
            "completion_tokens": 3,
            "total_tokens": 8,
        }

    def test_chat_messages_translates_function_call_to_tool_calls(
        self, monkeypatch
    ):
        patch_post(
            monkeypatch,
            FakeResponse(
                200,
                {
                    "model": "gemini-2.5-pro",
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": "get_weather",
                                            "args": {"city": "Paris"},
                                        }
                                    }
                                ]
                            },
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 5,
                        "candidatesTokenCount": 3,
                    },
                },
            ),
        )

        response = GeminiClient().chat_messages(
            make_provider(), {"model": "m", "messages": []}
        )

        message = response["choices"][0]["message"]
        assert message["content"] is None
        tool_call = message["tool_calls"][0]
        assert tool_call["type"] == "function"
        assert tool_call["id"].startswith("call_")
        assert tool_call["function"]["name"] == "get_weather"
        assert json.loads(tool_call["function"]["arguments"]) == {
            "city": "Paris"
        }

    def test_chat_messages_translates_openai_payload(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(
                200,
                {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
            ),
            recorded,
        )

        payload = {
            "model": "gemini-2.5-pro",
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

        GeminiClient().chat_messages(make_provider(), payload)

        body = recorded["json"]
        assert body["systemInstruction"] == {
            "parts": [{"text": "You are helpful."}]
        }
        assert body["generationConfig"] == {
            "temperature": 0.6,
            "topP": 0.9,
            "maxOutputTokens": 200,
            "stopSequences": ["END"],
        }
        assert body["tools"] == [
            {
                "functionDeclarations": [
                    {
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    }
                ]
            }
        ]
        assert body["toolConfig"] == {
            "functionCallingConfig": {"mode": "AUTO"}
        }
        assert body["contents"] == [
            {"role": "user", "parts": [{"text": "hi"}]},
            {
                "role": "model",
                "parts": [
                    {
                        "functionCall": {
                            "name": "get_weather",
                            "args": {"city": "Paris"},
                        }
                    }
                ],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": "get_weather",
                            "response": {"result": "22C"},
                        }
                    }
                ],
            },
            {"role": "user", "parts": [{"text": "and now?"}]},
        ]
        assert "frequency_penalty" not in body["generationConfig"]
        assert "presence_penalty" not in body["generationConfig"]


class TestStreamMessagesSync:
    def test_chat_stream_messages_yields_translated_chunks(
        self, monkeypatch
    ):
        recorded = {}
        body = (
            'data: {"candidates": [{"content": {"parts": [{"text": "Hel"}]}}]}\n\n'
            'data: {"candidates": [{"content": {"parts": [{"text": "lo"}]}, '
            '"finishReason": "STOP"}], "usageMetadata": '
            '{"promptTokenCount": 5, "candidatesTokenCount": 3}}\n\n'
        )
        patch_stream(
            monkeypatch, FakeStreamResponse(body.splitlines()), recorded
        )

        chunks = list(
            GeminiClient().chat_stream_messages(
                make_provider(), {"model": "m", "messages": []}
            )
        )

        assert ":streamGenerateContent?alt=sse" in recorded["url"]
        assert chunks[0]["choices"][0]["delta"]["content"] == "Hel"
        assert chunks[1]["choices"][0]["delta"]["content"] == "lo"
        finish = chunks[2]
        assert finish["choices"][0]["delta"] == {}
        assert finish["choices"][0]["finish_reason"] == "stop"
        usage = chunks[3]
        assert usage["choices"] == []
        assert usage["usage"]["total_tokens"] == 8

    def test_chat_stream_messages_translates_function_call(
        self, monkeypatch
    ):
        body = (
            'data: {"candidates": [{"content": {"parts": [{"functionCall": '
            '{"name": "get_weather", "args": {"city": "Paris"}}}]}, '
            '"finishReason": "STOP"}], "usageMetadata": '
            '{"promptTokenCount": 5, "candidatesTokenCount": 3}}\n\n'
        )
        patch_stream(monkeypatch, FakeStreamResponse(body.splitlines()))

        chunks = list(
            GeminiClient().chat_stream_messages(
                make_provider(), {"model": "m", "messages": []}
            )
        )

        tool_start = chunks[0]["choices"][0]["delta"]["tool_calls"][0]
        assert tool_start["index"] == 0
        assert tool_start["id"].startswith("call_")
        assert tool_start["type"] == "function"
        assert tool_start["function"]["name"] == "get_weather"
        assert tool_start["function"]["arguments"] == ""

        args = chunks[1]["choices"][0]["delta"]["tool_calls"][0]["function"]
        assert args["arguments"] == '{"city": "Paris"}'

        assert chunks[2]["choices"][0]["delta"] == {}
        assert chunks[2]["choices"][0]["finish_reason"] == "stop"
        assert chunks[3]["usage"]["total_tokens"] == 8

    def test_chat_stream_messages_raises_on_inline_error(self, monkeypatch):
        body = (
            'data: {"error": {"message": "overloaded"}}\n\n'
        )
        patch_stream(
            monkeypatch, FakeStreamResponse(body.splitlines())
        )

        with pytest.raises(ProviderHTTPError) as exc:
            list(
                GeminiClient().chat_stream_messages(
                    make_provider(), {"model": "m", "messages": []}
                )
            )

        assert exc.value.status_code == 0
        assert "overloaded" in exc.value.message

    def test_chat_stream_messages_skips_metadata_lines(self, monkeypatch):
        body = (
            "event: promptFeedback\n"
            "data: not-json\n"
            'data: {"candidates": [{"content": {"parts": [{"text": "A"}]}}]}\n\n'
            'data: {"candidates": [{"content": {"parts": [{"text": "B"}]}}]}\n\n'
        )
        patch_stream(
            monkeypatch, FakeStreamResponse(body.splitlines())
        )

        chunks = list(
            GeminiClient().chat_stream_messages(
                make_provider(), {"model": "m", "messages": []}
            )
        )

        assert len(chunks) == 2
        assert chunks[0]["choices"][0]["delta"]["content"] == "A"
        assert chunks[1]["choices"][0]["delta"]["content"] == "B"

    def test_chat_stream_messages_emits_usage_once(self, monkeypatch):
        body = (
            'data: {"candidates": [{"content": {"parts": [{"text": "A"}]}, '
            '"finishReason": "STOP"}], "usageMetadata": '
            '{"promptTokenCount": 5, "candidatesTokenCount": 3}}\n\n'
            'data: {"candidates": [{"content": {"parts": [{"text": "B"}]}, '
            '"finishReason": "STOP"}], "usageMetadata": '
            '{"promptTokenCount": 9, "candidatesTokenCount": 7}}\n\n'
        )
        patch_stream(
            monkeypatch, FakeStreamResponse(body.splitlines())
        )

        chunks = list(
            GeminiClient().chat_stream_messages(
                make_provider(), {"model": "m", "messages": []}
            )
        )

        usage_chunks = [chunk for chunk in chunks if chunk["choices"] == []]
        assert len(usage_chunks) == 1
        assert usage_chunks[0]["usage"]["total_tokens"] == 8


class TestProxySupport:
    def test_proxy_request_kwargs_matches_openai_compatible(self):
        from app.providers.openai_compat_client import (
            proxy_request_kwargs as occ_proxy,
        )

        provider = make_provider()
        url = (
            "https://generativelanguage.googleapis.com/v1beta"
            "/models/gemini-pro:generateContent"
        )

        assert (
            GeminiClient().proxy_request_kwargs(provider, url)
            == occ_proxy(provider, url)
        )

    def test_chat_forwards_forced_proxy(self, monkeypatch):
        recorded = {}

        def handler(url, **kwargs):
            recorded["proxy"] = kwargs.get("proxy")
            return FakeResponse(
                200,
                {"candidates": [{"content": {"parts": [{"text": "x"}]}}]},
            )

        monkeypatch.setattr(
            "app.providers.gemini_client.bounded_post", handler
        )

        provider = Provider(
            name="Google Gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key="sk-test",
            proxy="http://proxy.local:8080",
        )

        GeminiClient().chat(provider, "m", "hi")

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
                    "model": "gemini-2.5-pro",
                    "candidates": [
                        {
                            "content": {"parts": [{"text": "hello"}]},
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 5,
                        "candidatesTokenCount": 3,
                    },
                },
                request=request,
            )

        install_async_client(monkeypatch, handler)

        response = await GeminiClient().achat_messages(
            make_provider(),
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert (
            seen["url"]
            == "https://generativelanguage.googleapis.com/v1beta"
            "/models/m:generateContent"
        )
        assert seen["json"]["contents"] == [
            {"role": "user", "parts": [{"text": "hi"}]}
        ]
        assert response["choices"][0]["message"]["content"] == "hello"
        assert response["usage"]["total_tokens"] == 8

    @pytest.mark.asyncio
    async def test_achat_stream_messages_yields_translated_chunks(
        self, monkeypatch
    ):
        seen = {}
        body = (
            'data: {"candidates": [{"content": {"parts": [{"text": "Hel"}]}}]}\n\n'
            'data: {"candidates": [{"content": {"parts": [{"text": "lo"}]}, '
            '"finishReason": "STOP"}], "usageMetadata": '
            '{"promptTokenCount": 5, "candidatesTokenCount": 3}}\n\n'
        )

        def handler(request):
            seen["url"] = request_url(request)
            return httpx.Response(200, text=body, request=request)

        install_async_client(monkeypatch, handler)

        stream = GeminiClient().achat_stream_messages(
            make_provider(),
            {"model": "m", "messages": [], "stream": True},
        )
        chunks = [chunk async for chunk in stream]

        assert ":streamGenerateContent?alt=sse" in seen["url"]
        assert chunks[0]["choices"][0]["delta"]["content"] == "Hel"
        assert chunks[1]["choices"][0]["delta"]["content"] == "lo"
        assert chunks[2]["choices"][0]["finish_reason"] == "stop"
        assert chunks[3]["usage"]["total_tokens"] == 8

    @pytest.mark.asyncio
    async def test_achat_stream_messages_raises_on_inline_error(
        self, monkeypatch
    ):
        body = (
            'data: {"error": {"message": "overloaded"}}\n\n'
        )

        def handler(request):
            return httpx.Response(200, text=body, request=request)

        install_async_client(monkeypatch, handler)

        stream = GeminiClient().achat_stream_messages(
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
            "model": "gemini-2.5-pro",
            "candidates": [
                {
                    "content": {"parts": [{"text": "hello"}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 3,
            },
        }

        patch_post(
            monkeypatch, FakeResponse(200, response_body), {}
        )
        sync_result = GeminiClient().chat_messages(
            make_provider(),
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )

        def handler(request):
            return httpx.Response(200, json=response_body, request=request)

        install_async_client(monkeypatch, handler)

        async_result = await GeminiClient().achat_messages(
            make_provider(),
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert sync_result == async_result

    @pytest.mark.asyncio
    async def test_achat_matches_sync_on_mock_response(self, monkeypatch):
        response_body = {
            "candidates": [{"content": {"parts": [{"text": "Hello world"}]}}]
        }

        patch_post(monkeypatch, FakeResponse(200, response_body), {})
        sync_text = GeminiClient().chat(make_provider(), "m", "hi")

        def handler(request):
            return httpx.Response(200, json=response_body, request=request)

        install_async_client(monkeypatch, handler)

        async_text = await GeminiClient().achat(make_provider(), "m", "hi")

        assert sync_text == async_text == "Hello world"


class TestHealthCheck:
    def test_health_check_healthy_with_header_key(self, monkeypatch):
        recorded = {}

        def get_handler(url, **kwargs):
            recorded["url"] = url
            recorded["headers"] = kwargs.get("headers", {})
            return httpx.Response(
                200,
                json={"models": [{"name": "models/gemini-pro"}]},
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(
            "app.providers.gemini_client.bounded_get", get_handler
        )
        monkeypatch.setattr(
            "app.providers.gemini_client.GeminiClient.probe_model",
            lambda self, provider, model: SimpleNamespace(
                healthy=True, latency_ms=1, status_code=200, error=""
            ),
        )

        provider = gemini_defn().build_provider(api_key="sk-test")
        provider.models = ["gemini-pro"]

        report = HealthChecker().check(provider)

        assert report.connectivity is True
        assert report.status == "healthy"
        assert report.healthy_models == ["gemini-pro"]
        assert (
            recorded["url"]
            == "https://generativelanguage.googleapis.com/v1beta"
            "/models"
        )
        assert recorded["headers"]["x-goog-api-key"] == "sk-test"

    def test_health_check_unavailable_when_connection_refused(
        self, monkeypatch
    ):
        def get_handler(url, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(
            "app.providers.gemini_client.bounded_get", get_handler
        )

        provider = gemini_defn().build_provider(api_key="sk-test")
        provider.models = ["gemini-pro"]

        report = HealthChecker().check(provider)

        assert report.connectivity is False
        assert report.status == "unavailable"

    def test_health_check_unavailable_without_key(self, monkeypatch):
        def get_handler(url, **kwargs):
            return httpx.Response(
                401,
                text="invalid key",
                request=httpx.Request("GET", url),
            )

        monkeypatch.setattr(
            "app.providers.gemini_client.bounded_get", get_handler
        )

        provider = gemini_defn().build_provider(api_key="")
        provider.models = ["gemini-pro"]

        report = HealthChecker().check(provider)

        assert report.connectivity is False
        assert report.status == "unavailable"


class TestConnectivityProbe:
    def test_probe_returns_tuple_with_header_key(self, monkeypatch):
        recorded = {}

        def get_handler(url, **kwargs):
            recorded["url"] = url
            recorded["headers"] = kwargs.get("headers", {})
            return httpx.Response(
                200, json={}, request=httpx.Request("GET", url)
            )

        monkeypatch.setattr(
            "app.providers.gemini_client.bounded_get", get_handler
        )

        provider = gemini_defn().build_provider(api_key="sk-test")

        ok, details, latency = GeminiClient().connectivity_probe(provider)

        assert ok is True
        assert details == "HTTP 200"
        assert isinstance(latency, int)
        assert (
            recorded["url"]
            == "https://generativelanguage.googleapis.com/v1beta"
            "/models"
        )
        assert recorded["headers"]["x-goog-api-key"] == "sk-test"
        assert "Authorization" not in recorded["headers"]


class TestFailover:
    def test_chat_service_fails_over_from_gemini(self, monkeypatch):
        service = ChatService()

        class FakeGeminiClient:
            def chat(self, provider, model, message, **kwargs):
                raise ProviderHTTPError(503, "gemini down")

        class FakeBackupClient:
            def chat(self, provider, model, message, **kwargs):
                return "backup response"

        clients = {
            "gemini": FakeGeminiClient(),
            "Backup": FakeBackupClient(),
        }
        monkeypatch.setattr(service.registry, "get", lambda key: clients[key])

        gemini = gemini_defn().build_provider(api_key="sk-test")
        backup = Provider(name="Backup", base_url="http://localhost:9000/v1")

        result = service.chat_across(
            [(gemini, "gemini-pro"), (backup, "m2")], "hello"
        )

        assert result["success"] is True
        assert result["provider"] == "Backup"
        assert result["response"] == "backup response"

    @pytest.mark.asyncio
    async def test_async_chat_service_fails_over_from_gemini(
        self, monkeypatch
    ):
        service = AsyncChatService()

        class FakeGeminiClient:
            async def achat(self, provider, model, message, **kwargs):
                raise ProviderHTTPError(503, "gemini down")

        class FakeBackupClient:
            async def achat(self, provider, model, message, **kwargs):
                return "backup response"

        clients = {
            "gemini": FakeGeminiClient(),
            "Backup": FakeBackupClient(),
        }
        monkeypatch.setattr(service.registry, "get", lambda key: clients[key])

        gemini = gemini_defn().build_provider(api_key="sk-test")
        backup = Provider(name="Backup", base_url="http://localhost:9000/v1")

        result = await service.achat_across(
            [(gemini, "gemini-pro"), (backup, "m2")], "hello"
        )

        assert result["success"] is True
        assert result["provider"] == "Backup"
        assert result["response"] == "backup response"


class TestReload:
    def test_reload_enables_gemini_and_applies_model_priority(
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
            id="gemini",
            name="Google Gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key="sk-test",
            enabled=False,
            models=["gemini-pro", "gemini-ultra"],
        )
        relay.provider_manager.register(provider)

        monkeypatch.setattr(
            "app.providers.gemini_client.GeminiClient.list_models",
            lambda self, provider: ["gemini-pro", "gemini-ultra"],
        )

        try:
            result = reload_config(
                relay,
                env=SimpleNamespace(
                    gemini_enabled=True,
                    gemini_api_key="sk-test",
                    gemini_model_priority=["gemini-ultra"],
                ),
            )

            assert result["reloaded"] is True
            assert "gemini_enabled" in result["applied"]
            assert "gemini_api_key" in result["applied"]
            assert "gemini_model_priority" in result["applied"]
            assert provider.enabled is True
            assert provider.priority_models == ["gemini-ultra"]
            assert provider.models[0] == "gemini-ultra"
            assert settings.gemini_model_priority == ["gemini-ultra"]
        finally:
            for field, value in snapshot.items():
                setattr(settings, field, value)

    def test_gemini_spec_is_reloadable(self):
        import app.services.reload as reload_module

        prefixes = {spec["prefix"] for spec in reload_module._PROVIDER_SPECS}

        assert "gemini" in prefixes
        assert "gemini_enabled" in reload_module._RELOADABLE_FIELDS
        assert "gemini_api_key" in reload_module._RELOADABLE_FIELDS
        assert "gemini_model_priority" in reload_module._RELOADABLE_FIELDS


class TestWizard:
    def test_wizard_no_longer_defers_gemini(self, monkeypatch):
        from app.setup import wizard

        notices = []
        monkeypatch.setattr(
            "app.setup.wizard.setup_state.write_setup_state",
            lambda *args, **kwargs: None,
        )

        result = wizard._finish(
            SimpleNamespace(notice=notices.append),
            configured={"gemini": True},
            completed=True,
        )

        assert result.usable is True
        assert result.deferred == []
        assert not any("not wired" in notice for notice in notices)
