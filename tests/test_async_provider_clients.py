import json

import httpx
import pytest

from app.providers.anthropic_client import AnthropicClient
from app.providers.base import Provider
from app.providers.exceptions import ProviderHTTPError, ProviderTimeout
from app.providers.gemini_client import GeminiClient
from app.providers.lmstudio_client import LMStudioClient
from app.providers.nvidia_client import NvidiaClient
from app.providers.ollama_client import OllamaClient
from app.providers.openai_client import OpenAIClient
from app.providers.openai_compat_client import OpenAICompatibleClient
from app.services.metrics import relay_metrics


def make_provider(key="sk-test", base_url="https://api.example.com/v1"):
    return Provider(name="Test", base_url=base_url, api_key=key)


class _SpyAsyncClient(httpx.AsyncClient):
    def __init__(self, handler, *args, **kwargs):
        self.init_kwargs = dict(kwargs)
        self.calls = []
        kwargs.pop("proxy", None)
        kwargs.pop("trust_env", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        super().__init__(*args, **kwargs)

    async def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return await super().post(url, **kwargs)

    async def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return await super().get(url, **kwargs)

    def stream(self, method, url, **kwargs):
        self.calls.append(("stream", method, url, kwargs))
        return super().stream(method, url, **kwargs)


def install_async_client(monkeypatch, module, handler):
    spies = []

    def factory(*args, **kwargs):
        spy = _SpyAsyncClient(handler, **kwargs)
        spies.append(spy)
        return spy

    monkeypatch.setattr(
        f"app.providers.{module}.httpx.AsyncClient",
        factory,
    )
    return spies


def request_json(request):
    return json.loads(request.content)


def request_url(request):
    return str(request.url)


class TestAsyncContractOnEveryClient:
    def test_all_six_clients_expose_the_four_async_methods(self):
        clients = [
            OpenAICompatibleClient(),
            NvidiaClient(),
            OpenAIClient(),
            LMStudioClient(),
            AnthropicClient(),
            GeminiClient(),
            OllamaClient(),
        ]

        for client in clients:
            assert callable(getattr(client, "achat", None)), client
            assert callable(getattr(client, "achat_stream", None)), client
            assert callable(getattr(client, "alist_models", None)), client
            assert callable(getattr(client, "aprobe_model", None)), client

    def test_openai_compatible_clients_inherit_async_methods(self):
        assert OpenAICompatibleClient.achat is NvidiaClient.achat
        assert OpenAICompatibleClient.achat is OpenAIClient.achat
        assert OpenAICompatibleClient.achat is LMStudioClient.achat
        assert OpenAICompatibleClient.achat_stream is NvidiaClient.achat_stream
        assert OpenAICompatibleClient.alist_models is NvidiaClient.alist_models
        assert OpenAICompatibleClient.aprobe_model is NvidiaClient.aprobe_model


class TestOpenAICompatibleAsyncChat:
    @pytest.mark.asyncio
    async def test_achat_returns_message_content(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["url"] = request_url(request)
            seen["headers"] = dict(request.headers)
            seen["json"] = request_json(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "hello"}}]},
                request=request,
            )

        install_async_client(monkeypatch, "openai_compat_client", handler)

        result = await OpenAICompatibleClient().achat(
            make_provider(), "m", "hi"
        )

        assert result == "hello"
        assert seen["url"] == "https://api.example.com/v1/chat/completions"
        assert seen["headers"]["authorization"] == "Bearer sk-test"
        assert seen["headers"]["content-type"] == "application/json"
        assert seen["json"]["model"] == "m"
        assert seen["json"]["temperature"] == 0.2
        assert seen["json"]["max_tokens"] == 512
        assert seen["json"]["messages"] == [
            {"role": "user", "content": "hi"}
        ]

    @pytest.mark.asyncio
    async def test_achat_messages_rejects_empty_choices(self, monkeypatch):
        def handler(request):
            return httpx.Response(200, json={"choices": []}, request=request)

        install_async_client(monkeypatch, "openai_compat_client", handler)

        with pytest.raises(ProviderHTTPError) as exc:
            await OpenAICompatibleClient().achat_messages(
                make_provider(),
                {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            )

        assert "empty provider response" in exc.value.message

    @pytest.mark.asyncio
    async def test_achat_messages_returns_message_payload(self, monkeypatch):
        def handler(request):
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "hi"}}]},
                request=request,
            )

        install_async_client(monkeypatch, "openai_compat_client", handler)

        data = await OpenAICompatibleClient().achat_messages(
            make_provider(),
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert data["choices"][0]["message"]["content"] == "hi"

    @pytest.mark.asyncio
    async def test_achat_passes_optional_generation_params(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["json"] = request_json(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "x"}}]},
                request=request,
            )

        install_async_client(monkeypatch, "openai_compat_client", handler)

        await OpenAICompatibleClient().achat(
            make_provider(),
            "m",
            "hi",
            temperature=0.7,
            top_p=0.9,
            max_tokens=100,
            stop=["END"],
            frequency_penalty=0.5,
            presence_penalty=0.5,
            seed=42,
        )

        payload = seen["json"]
        assert payload["temperature"] == 0.7
        assert payload["top_p"] == 0.9
        assert payload["max_tokens"] == 100
        assert payload["stop"] == ["END"]
        assert payload["frequency_penalty"] == 0.5
        assert payload["presence_penalty"] == 0.5
        assert payload["seed"] == 42

    @pytest.mark.asyncio
    async def test_achat_omits_bearer_header_without_key(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["headers"] = dict(request.headers)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "x"}}]},
                request=request,
            )

        install_async_client(monkeypatch, "openai_compat_client", handler)

        await OpenAICompatibleClient().achat(make_provider(key=""), "m", "hi")

        assert "Authorization" not in seen["headers"]

    @pytest.mark.asyncio
    async def test_achat_uses_configured_request_timeout(self, monkeypatch):
        def handler(request):
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "x"}}]},
                request=request,
            )

        spies = install_async_client(monkeypatch, "openai_compat_client", handler)

        await OpenAICompatibleClient().achat(make_provider(), "m", "hi")

        assert spies[0].calls[0][0] == "post"
        assert spies[0].calls[0][2]["timeout"] is not None

    @pytest.mark.asyncio
    async def test_achat_raises_provider_http_error_on_4xx(self, monkeypatch):
        def handler(request):
            return httpx.Response(400, text="bad request", request=request)

        install_async_client(monkeypatch, "openai_compat_client", handler)

        with pytest.raises(ProviderHTTPError) as exc:
            await OpenAICompatibleClient().achat(make_provider(), "m", "hi")

        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_achat_captures_retry_after_seconds(self, monkeypatch):
        def handler(request):
            return httpx.Response(
                429,
                text="slow down",
                headers={"Retry-After": "2"},
                request=request,
            )

        install_async_client(monkeypatch, "openai_compat_client", handler)

        with pytest.raises(ProviderHTTPError) as exc:
            await OpenAICompatibleClient().achat(make_provider(), "m", "hi")

        assert exc.value.status_code == 429
        assert exc.value.retry_after == 2.0

    @pytest.mark.asyncio
    async def test_achat_retry_after_none_when_absent(self, monkeypatch):
        def handler(request):
            return httpx.Response(429, text="slow down", request=request)

        install_async_client(monkeypatch, "openai_compat_client", handler)

        with pytest.raises(ProviderHTTPError) as exc:
            await OpenAICompatibleClient().achat(make_provider(), "m", "hi")

        assert exc.value.retry_after is None

    @pytest.mark.asyncio
    async def test_achat_raises_provider_timeout_on_read_timeout(self, monkeypatch):
        def handler(request):
            raise httpx.ReadTimeout("boom")

        install_async_client(monkeypatch, "openai_compat_client", handler)

        with pytest.raises(ProviderTimeout):
            await OpenAICompatibleClient().achat(make_provider(), "m", "hi")

    @pytest.mark.asyncio
    async def test_achat_raises_provider_timeout_on_generic_timeout(self, monkeypatch):
        def handler(request):
            raise httpx.TimeoutException("boom")

        install_async_client(monkeypatch, "openai_compat_client", handler)

        with pytest.raises(ProviderTimeout):
            await OpenAICompatibleClient().achat(make_provider(), "m", "hi")

    @pytest.mark.asyncio
    async def test_achat_raises_provider_http_error_on_transport_error(self, monkeypatch):
        def handler(request):
            raise httpx.HTTPError("boom")

        install_async_client(monkeypatch, "openai_compat_client", handler)

        with pytest.raises(ProviderHTTPError) as exc:
            await OpenAICompatibleClient().achat(make_provider(), "m", "hi")

        assert exc.value.status_code == 0

    @pytest.mark.asyncio
    async def test_achat_records_provider_metric(self, monkeypatch):
        def handler(request):
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "x"}}]},
                request=request,
            )

        install_async_client(monkeypatch, "openai_compat_client", handler)
        relay_metrics.reset()

        await OpenAICompatibleClient().achat(make_provider(), "m", "hi")

        assert (
            relay_metrics.provider_requests.value(
                provider="Test", operation="chat"
            )
            == 1.0
        )


class TestOpenAICompatibleAsyncStream:
    @pytest.mark.asyncio
    async def test_achat_stream_yields_content_deltas(self, monkeypatch):
        body = (
            'data: {"choices": [{"delta": {"content": "Hel"}}]}\n\n'
            'data: {"choices": [{"delta": {"content": "lo"}}]}\n\n'
            "data: [DONE]\n\n"
        )

        def handler(request):
            seen = request_json(request)
            assert seen["stream"] is True
            return httpx.Response(200, text=body, request=request)

        install_async_client(monkeypatch, "openai_compat_client", handler)

        stream = OpenAICompatibleClient().achat_stream(
            make_provider(), "m", "hi"
        )
        chunks = [chunk async for chunk in stream]

        assert chunks == ["Hel", "lo"]

    @pytest.mark.asyncio
    async def test_achat_stream_skips_malformed_chunks(self, monkeypatch):
        body = (
            "data: not-json\n\n"
            'data: {"choices": [{"delta": {"content": "Hi"}}]}\n\n'
            'data: {"choices": []}\n\n'
            "data: [DONE]\n\n"
        )

        def handler(request):
            return httpx.Response(200, text=body, request=request)

        install_async_client(monkeypatch, "openai_compat_client", handler)

        stream = OpenAICompatibleClient().achat_stream(
            make_provider(), "m", "hi"
        )
        chunks = [chunk async for chunk in stream]

        assert chunks == ["Hi"]

    @pytest.mark.asyncio
    async def test_achat_stream_uses_configured_timeout(self, monkeypatch):
        def handler(request):
            return httpx.Response(200, text="data: [DONE]\n\n", request=request)

        spies = install_async_client(monkeypatch, "openai_compat_client", handler)

        stream = OpenAICompatibleClient().achat_stream(
            make_provider(), "m", "hi"
        )
        [chunk async for chunk in stream]

        assert spies[0].calls[0][0] == "stream"
        assert spies[0].calls[0][3]["timeout"] is not None

    @pytest.mark.asyncio
    async def test_achat_stream_raises_redacted_http_error(self, monkeypatch):
        body = '{"error": "invalid key sk-test-123"}'

        def handler(request):
            return httpx.Response(
                401,
                text=body,
                headers={"Retry-After": "3"},
                request=request,
            )

        install_async_client(monkeypatch, "openai_compat_client", handler)

        stream = OpenAICompatibleClient().achat_stream(
            make_provider(key="sk-test-123"), "m", "hi"
        )

        with pytest.raises(ProviderHTTPError) as exc:
            await anext(stream)

        assert exc.value.status_code == 401
        assert exc.value.retry_after == 3.0
        assert "sk-test-123" not in exc.value.message
        assert "[REDACTED]" in exc.value.message

    @pytest.mark.asyncio
    async def test_achat_stream_raises_provider_timeout(self, monkeypatch):
        def handler(request):
            raise httpx.ReadTimeout("boom")

        install_async_client(monkeypatch, "openai_compat_client", handler)

        stream = OpenAICompatibleClient().achat_stream(
            make_provider(), "m", "hi"
        )

        with pytest.raises(ProviderTimeout):
            await anext(stream)


class TestOpenAICompatibleAsyncDiscovery:
    @pytest.mark.asyncio
    async def test_alist_models_parses_ids(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["url"] = request_url(request)
            seen["headers"] = dict(request.headers)
            return httpx.Response(
                200, json={"data": [{"id": "a"}, {"id": "b"}]}, request=request
            )

        install_async_client(monkeypatch, "openai_compat_client", handler)

        result = await OpenAICompatibleClient().alist_models(make_provider())

        assert result == ["a", "b"]
        assert seen["url"] == "https://api.example.com/v1/models"
        assert seen["headers"]["authorization"] == "Bearer sk-test"

    @pytest.mark.asyncio
    async def test_alist_models_omits_auth_when_keyless(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["headers"] = dict(request.headers)
            return httpx.Response(200, json={"data": []}, request=request)

        install_async_client(monkeypatch, "openai_compat_client", handler)

        await OpenAICompatibleClient().alist_models(make_provider(key=""))

        assert "Authorization" not in seen["headers"]

    @pytest.mark.asyncio
    async def test_alist_models_raises_on_http_error(self, monkeypatch):
        def handler(request):
            return httpx.Response(500, text="nope", request=request)

        install_async_client(monkeypatch, "openai_compat_client", handler)

        with pytest.raises(ProviderHTTPError) as exc:
            await OpenAICompatibleClient().alist_models(make_provider())

        assert exc.value.status_code == 500

    @pytest.mark.asyncio
    async def test_alist_models_raises_on_timeout(self, monkeypatch):
        def handler(request):
            raise httpx.TimeoutException("boom")

        install_async_client(monkeypatch, "openai_compat_client", handler)

        with pytest.raises(ProviderTimeout):
            await OpenAICompatibleClient().alist_models(make_provider())

    @pytest.mark.asyncio
    async def test_aprobe_model_healthy(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["json"] = request_json(request)
            return httpx.Response(200, json={"choices": []}, request=request)

        install_async_client(monkeypatch, "openai_compat_client", handler)

        probe = await OpenAICompatibleClient().aprobe_model(make_provider(), "m")

        assert probe.healthy is True
        assert probe.status_code == 200
        assert probe.error == ""
        assert seen["json"]["max_tokens"] == 1
        assert seen["json"]["messages"][0]["content"] == "ping"

    @pytest.mark.asyncio
    async def test_aprobe_model_unhealthy_with_status(self, monkeypatch):
        def handler(request):
            return httpx.Response(404, text="missing", request=request)

        install_async_client(monkeypatch, "openai_compat_client", handler)

        probe = await OpenAICompatibleClient().aprobe_model(make_provider(), "m")

        assert probe.healthy is False
        assert probe.status_code == 404
        assert probe.error == "missing"

    @pytest.mark.asyncio
    async def test_aprobe_model_timeout(self, monkeypatch):
        def handler(request):
            raise httpx.ReadTimeout("boom")

        install_async_client(monkeypatch, "openai_compat_client", handler)

        probe = await OpenAICompatibleClient().aprobe_model(make_provider(), "m")

        assert probe.healthy is False
        assert probe.status_code == 0
        assert probe.error == "timeout"


class TestAsyncProxyKwargs:
    @pytest.mark.asyncio
    async def test_achat_forces_explicit_proxy(self, monkeypatch):
        def handler(request):
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "x"}}]},
                request=request,
            )

        spies = install_async_client(monkeypatch, "openai_compat_client", handler)

        provider = make_provider()
        provider.proxy = "http://proxy.local:8080"
        await OpenAICompatibleClient().achat(provider, "m", "hi")

        assert spies[0].init_kwargs["trust_env"] is False
        assert spies[0].init_kwargs["proxy"] == "http://proxy.local:8080"

    @pytest.mark.asyncio
    async def test_achat_explicit_bypass_sends_no_proxy(self, monkeypatch):
        def handler(request):
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "x"}}]},
                request=request,
            )

        spies = install_async_client(monkeypatch, "openai_compat_client", handler)

        provider = make_provider()
        provider.proxy = ""
        await OpenAICompatibleClient().achat(provider, "m", "hi")

        assert spies[0].init_kwargs["trust_env"] is False
        assert spies[0].init_kwargs["proxy"] is None

    @pytest.mark.asyncio
    async def test_achat_error_body_redacts_key_and_bounds_length(self, monkeypatch):
        body = "denied sk-test-123 " + "y" * 500

        def handler(request):
            return httpx.Response(403, text=body, request=request)

        install_async_client(monkeypatch, "openai_compat_client", handler)

        with pytest.raises(ProviderHTTPError) as exc:
            await OpenAICompatibleClient().achat(
                make_provider(key="sk-test-123"), "m", "hi"
            )

        assert "sk-test-123" not in exc.value.message
        assert len(exc.value.message) <= 203
        assert exc.value.message.endswith("...")


class TestAnthropicAsync:
    @pytest.mark.asyncio
    async def test_achat_returns_assistant_text(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["url"] = request_url(request)
            seen["headers"] = dict(request.headers)
            seen["json"] = request_json(request)
            return httpx.Response(
                200,
                json={
                    "content": [
                        {"type": "text", "text": "Hello "},
                        {"type": "text", "text": "world"},
                    ]
                },
                request=request,
            )

        install_async_client(monkeypatch, "anthropic_client", handler)

        result = await AnthropicClient().achat(make_provider(), "m", "hi")

        assert result == "Hello world"
        assert seen["url"] == "https://api.example.com/v1/messages"
        assert seen["headers"]["x-api-key"] == "sk-test"
        assert seen["headers"]["anthropic-version"] == "2023-06-01"
        assert seen["json"]["model"] == "m"
        assert seen["json"]["max_tokens"] == 512
        assert seen["json"]["messages"] == [{"role": "user", "content": "hi"}]

    @pytest.mark.asyncio
    async def test_achat_passes_supported_params(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["json"] = request_json(request)
            return httpx.Response(
                200, json={"content": [{"type": "text", "text": "x"}]},
                request=request,
            )

        install_async_client(monkeypatch, "anthropic_client", handler)

        await AnthropicClient().achat(
            make_provider(),
            "m",
            "hi",
            temperature=0.3,
            top_p=0.8,
            max_tokens=100,
            stop="END",
        )

        payload = seen["json"]
        assert payload["temperature"] == 0.3
        assert payload["top_p"] == 0.8
        assert payload["max_tokens"] == 100
        assert payload["stop_sequences"] == ["END"]

    @pytest.mark.asyncio
    async def test_achat_raises_provider_http_error_with_redaction(self, monkeypatch):
        def handler(request):
            return httpx.Response(
                401,
                text='{"error": "bad key sk-test-123"}',
                headers={"Retry-After": "4"},
                request=request,
            )

        install_async_client(monkeypatch, "anthropic_client", handler)

        with pytest.raises(ProviderHTTPError) as exc:
            await AnthropicClient().achat(
                make_provider(key="sk-test-123"), "m", "hi"
            )

        assert exc.value.status_code == 401
        assert exc.value.retry_after == 4.0
        assert "sk-test-123" not in exc.value.message
        assert "[REDACTED]" in exc.value.message

    @pytest.mark.asyncio
    async def test_achat_raises_provider_timeout(self, monkeypatch):
        def handler(request):
            raise httpx.ReadTimeout("boom")

        install_async_client(monkeypatch, "anthropic_client", handler)

        with pytest.raises(ProviderTimeout):
            await AnthropicClient().achat(make_provider(), "m", "hi")

    @pytest.mark.asyncio
    async def test_achat_stream_yields_text_deltas(self, monkeypatch):
        body = (
            'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}\n\n'
            'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "!"}}\n\n'
            'data: {"type": "message_stop"}\n\n'
        )

        def handler(request):
            return httpx.Response(200, text=body, request=request)

        install_async_client(monkeypatch, "anthropic_client", handler)

        stream = AnthropicClient().achat_stream(make_provider(), "m", "hi")
        chunks = [chunk async for chunk in stream]

        assert chunks == ["Hi", "!"]

    @pytest.mark.asyncio
    async def test_achat_stream_skips_non_text_events(self, monkeypatch):
        body = (
            'data: {"type": "content_block_start"}\n\n'
            'data: {"type": "content_block_delta", "delta": {"type": "input_json_delta"}}\n\n'
            'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Y"}}\n\n'
            'data: {"type": "message_stop"}\n\n'
        )

        def handler(request):
            return httpx.Response(200, text=body, request=request)

        install_async_client(monkeypatch, "anthropic_client", handler)

        stream = AnthropicClient().achat_stream(make_provider(), "m", "hi")
        chunks = [chunk async for chunk in stream]

        assert chunks == ["Y"]

    @pytest.mark.asyncio
    async def test_achat_stream_raises_provider_timeout(self, monkeypatch):
        def handler(request):
            raise httpx.TimeoutException("boom")

        install_async_client(monkeypatch, "anthropic_client", handler)

        stream = AnthropicClient().achat_stream(make_provider(), "m", "hi")

        with pytest.raises(ProviderTimeout):
            await anext(stream)

    @pytest.mark.asyncio
    async def test_alist_models_parses_ids(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["headers"] = dict(request.headers)
            return httpx.Response(
                200, json={"data": [{"id": "claude-3"}, {"id": "claude-4"}]},
                request=request,
            )

        install_async_client(monkeypatch, "anthropic_client", handler)

        result = await AnthropicClient().alist_models(make_provider())

        assert result == ["claude-3", "claude-4"]
        assert seen["headers"]["x-api-key"] == "sk-test"

    @pytest.mark.asyncio
    async def test_aprobe_model_healthy(self, monkeypatch):
        def handler(request):
            payload = request_json(request)
            assert payload["messages"][0]["content"] == "ping"
            assert payload["max_tokens"] == 1
            return httpx.Response(
                200,
                json={"content": [{"type": "text", "text": "pong"}]},
                request=request,
            )

        install_async_client(monkeypatch, "anthropic_client", handler)

        probe = await AnthropicClient().aprobe_model(make_provider(), "m")

        assert probe.healthy is True
        assert probe.status_code == 200

    @pytest.mark.asyncio
    async def test_aprobe_model_timeout(self, monkeypatch):
        def handler(request):
            raise httpx.ReadTimeout("boom")

        install_async_client(monkeypatch, "anthropic_client", handler)

        probe = await AnthropicClient().aprobe_model(make_provider(), "m")

        assert probe.healthy is False
        assert probe.status_code == 0
        assert probe.error == "timeout"


class TestGeminiAsync:
    @pytest.mark.asyncio
    async def test_achat_sends_key_in_header_and_returns_text(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["url"] = request_url(request)
            seen["headers"] = dict(request.headers)
            seen["json"] = request_json(request)
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": "Hello "}, {"text": "Gemini"}]}}
                    ]
                },
                request=request,
            )

        install_async_client(monkeypatch, "gemini_client", handler)

        result = await GeminiClient().achat(make_provider(), "m", "hi")

        assert result == "Hello Gemini"
        assert ":generateContent" in seen["url"]
        assert "?key=" not in seen["url"]
        assert seen["headers"]["x-goog-api-key"] == "sk-test"
        assert "models/m:generateContent" in seen["url"]
        assert seen["json"]["contents"][0]["parts"] == [{"text": "hi"}]

    @pytest.mark.asyncio
    async def test_achat_quotes_model_segments_in_url(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["url"] = request_url(request)
            seen["headers"] = dict(request.headers)
            return httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": [{"text": "x"}]}}]},
                request=request,
            )

        install_async_client(monkeypatch, "gemini_client", handler)

        await GeminiClient().achat(make_provider(), "my model", "hi")

        assert "models/my%20model:generateContent" in seen["url"]

    @pytest.mark.asyncio
    async def test_achat_passes_generation_config(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["json"] = request_json(request)
            return httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": [{"text": "x"}]}}]},
                request=request,
            )

        install_async_client(monkeypatch, "gemini_client", handler)

        await GeminiClient().achat(
            make_provider(),
            "m",
            "hi",
            temperature=0.4,
            top_p=0.9,
            max_tokens=200,
            stop=["END"],
        )

        config = seen["json"]["generationConfig"]
        assert config["temperature"] == 0.4
        assert config["topP"] == 0.9
        assert config["maxOutputTokens"] == 200
        assert config["stopSequences"] == ["END"]

    @pytest.mark.asyncio
    async def test_achat_raises_provider_http_error_with_redaction(self, monkeypatch):
        def handler(request):
            return httpx.Response(
                403,
                text="forbidden sk-test-123",
                headers={"Retry-After": "1"},
                request=request,
            )

        install_async_client(monkeypatch, "gemini_client", handler)

        with pytest.raises(ProviderHTTPError) as exc:
            await GeminiClient().achat(
                make_provider(key="sk-test-123"), "m", "hi"
            )

        assert exc.value.status_code == 403
        assert exc.value.retry_after == 1.0
        assert "sk-test-123" not in exc.value.message
        assert "[REDACTED]" in exc.value.message

    @pytest.mark.asyncio
    async def test_achat_raises_provider_timeout(self, monkeypatch):
        def handler(request):
            raise httpx.ReadTimeout("boom")

        install_async_client(monkeypatch, "gemini_client", handler)

        with pytest.raises(ProviderTimeout):
            await GeminiClient().achat(make_provider(), "m", "hi")

    @pytest.mark.asyncio
    async def test_achat_stream_uses_stream_endpoint_and_yields_text(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["url"] = request_url(request)
            seen["headers"] = dict(request.headers)
            return httpx.Response(
                200,
                text=(
                    'data: {"candidates": [{"content": {"parts": [{"text": "Hel"}]}, "finishReason": "STOP"}]}\n\n'
                    'data: {"candidates": [{"content": {"parts": [{"text": "lo"}]}, "finishReason": "STOP"}]}\n\n'
                ),
                request=request,
            )

        install_async_client(monkeypatch, "gemini_client", handler)

        stream = GeminiClient().achat_stream(make_provider(), "m", "hi")
        chunks = [chunk async for chunk in stream]

        assert chunks == ["Hel", "lo"]
        assert ":streamGenerateContent?alt=sse" in seen["url"]
        assert "?key=" not in seen["url"]
        assert seen["headers"]["x-goog-api-key"] == "sk-test"

    @pytest.mark.asyncio
    async def test_achat_stream_skips_empty_chunks(self, monkeypatch):
        def handler(request):
            return httpx.Response(
                200,
                text=(
                    'data: {"candidates": [{"content": {"parts": []}}]}\n\n'
                    'data: {"candidates": [{"content": {"parts": [{"text": "A"}]}}]}\n\n'
                    'data: {"candidates": [{"content": {"parts": [{"text": "B"}]}}, {"finishReason": "STOP"}]}\n\n'
                ),
                request=request,
            )

        install_async_client(monkeypatch, "gemini_client", handler)

        stream = GeminiClient().achat_stream(make_provider(), "m", "hi")
        chunks = [chunk async for chunk in stream]

        assert chunks == ["A", "B"]

    @pytest.mark.asyncio
    async def test_achat_stream_raises_provider_timeout(self, monkeypatch):
        def handler(request):
            raise httpx.TimeoutException("boom")

        install_async_client(monkeypatch, "gemini_client", handler)

        stream = GeminiClient().achat_stream(make_provider(), "m", "hi")

        with pytest.raises(ProviderTimeout):
            await anext(stream)

    @pytest.mark.asyncio
    async def test_alist_models_strips_models_prefix(self, monkeypatch):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "models/gemini-pro"},
                        {"name": "models/gemini-ultra"},
                    ]
                },
                request=request,
            )

        install_async_client(monkeypatch, "gemini_client", handler)

        result = await GeminiClient().alist_models(make_provider())

        assert result == ["gemini-pro", "gemini-ultra"]

    @pytest.mark.asyncio
    async def test_aprobe_model_healthy(self, monkeypatch):
        def handler(request):
            assert request_json(request)["contents"][0]["parts"] == [
                {"text": "ping"}
            ]
            return httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": [{"text": "pong"}]}}]},
                request=request,
            )

        install_async_client(monkeypatch, "gemini_client", handler)

        probe = await GeminiClient().aprobe_model(make_provider(), "m")

        assert probe.healthy is True
        assert probe.status_code == 200

    @pytest.mark.asyncio
    async def test_aprobe_model_timeout(self, monkeypatch):
        def handler(request):
            raise httpx.ReadTimeout("boom")

        install_async_client(monkeypatch, "gemini_client", handler)

        probe = await GeminiClient().aprobe_model(make_provider(), "m")

        assert probe.healthy is False
        assert probe.status_code == 0
        assert probe.error == "timeout"


class TestOllamaAsync:
    @pytest.mark.asyncio
    async def test_achat_returns_message_content(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["url"] = request_url(request)
            seen["json"] = request_json(request)
            return httpx.Response(
                200,
                json={"message": {"role": "assistant", "content": "Yo"}},
                request=request,
            )

        install_async_client(monkeypatch, "ollama_client", handler)

        result = await OllamaClient().achat(make_provider(), "m", "hi")

        assert result == "Yo"
        assert seen["url"] == "https://api.example.com/v1/api/chat"
        assert seen["json"]["model"] == "m"
        assert seen["json"]["stream"] is False
        assert seen["json"]["messages"] == [{"role": "user", "content": "hi"}]

    @pytest.mark.asyncio
    async def test_achat_passes_options(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["json"] = request_json(request)
            return httpx.Response(
                200,
                json={"message": {"role": "assistant", "content": "x"}},
                request=request,
            )

        install_async_client(monkeypatch, "ollama_client", handler)

        await OllamaClient().achat(
            make_provider(),
            "m",
            "hi",
            temperature=0.6,
            top_p=0.8,
            max_tokens=150,
            stop="END",
        )

        options = seen["json"]["options"]
        assert options["temperature"] == 0.6
        assert options["top_p"] == 0.8
        assert options["num_predict"] == 150
        assert options["stop"] == ["END"]

    @pytest.mark.asyncio
    async def test_achat_raises_provider_http_error_with_redaction(self, monkeypatch):
        def handler(request):
            return httpx.Response(
                500,
                text="boom sk-test-123",
                headers={"Retry-After": "2"},
                request=request,
            )

        install_async_client(monkeypatch, "ollama_client", handler)

        with pytest.raises(ProviderHTTPError) as exc:
            await OllamaClient().achat(make_provider(key="sk-test-123"), "m", "hi")

        assert exc.value.status_code == 500
        assert exc.value.retry_after == 2.0
        assert "sk-test-123" not in exc.value.message
        assert "[REDACTED]" in exc.value.message

    @pytest.mark.asyncio
    async def test_achat_raises_provider_timeout(self, monkeypatch):
        def handler(request):
            raise httpx.ReadTimeout("boom")

        install_async_client(monkeypatch, "ollama_client", handler)

        with pytest.raises(ProviderTimeout):
            await OllamaClient().achat(make_provider(), "m", "hi")

    @pytest.mark.asyncio
    async def test_achat_stream_yields_ndjson_deltas(self, monkeypatch):
        body = (
            '{"message": {"role": "assistant", "content": "Hel"}}\n'
            '{"message": {"role": "assistant", "content": "lo"}}\n'
            '{"message": {"role": "assistant", "content": ""}, "done": true}\n'
        )

        def handler(request):
            assert request_json(request)["stream"] is True
            return httpx.Response(200, text=body, request=request)

        install_async_client(monkeypatch, "ollama_client", handler)

        stream = OllamaClient().achat_stream(make_provider(), "m", "hi")
        chunks = [chunk async for chunk in stream]

        assert chunks == ["Hel", "lo"]

    @pytest.mark.asyncio
    async def test_achat_stream_skips_malformed_lines(self, monkeypatch):
        body = (
            "not-json\n"
            '{"message": {"role": "assistant", "content": "A"}}\n'
            '{"done": true}\n'
        )

        def handler(request):
            return httpx.Response(200, text=body, request=request)

        install_async_client(monkeypatch, "ollama_client", handler)

        stream = OllamaClient().achat_stream(make_provider(), "m", "hi")
        chunks = [chunk async for chunk in stream]

        assert chunks == ["A"]

    @pytest.mark.asyncio
    async def test_achat_stream_raises_provider_timeout(self, monkeypatch):
        def handler(request):
            raise httpx.ReadTimeout("boom")

        install_async_client(monkeypatch, "ollama_client", handler)

        stream = OllamaClient().achat_stream(make_provider(), "m", "hi")

        with pytest.raises(ProviderTimeout):
            await anext(stream)

    @pytest.mark.asyncio
    async def test_alist_models_parses_tag_names(self, monkeypatch):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "llama3"},
                        {"name": "mistral"},
                    ]
                },
                request=request,
            )

        install_async_client(monkeypatch, "ollama_client", handler)

        result = await OllamaClient().alist_models(make_provider())

        assert result == ["llama3", "mistral"]

    @pytest.mark.asyncio
    async def test_aprobe_model_healthy(self, monkeypatch):
        def handler(request):
            assert request_json(request)["stream"] is False
            return httpx.Response(
                200,
                json={"message": {"role": "assistant", "content": "pong"}},
                request=request,
            )

        install_async_client(monkeypatch, "ollama_client", handler)

        probe = await OllamaClient().aprobe_model(make_provider(), "m")

        assert probe.healthy is True
        assert probe.status_code == 200

    @pytest.mark.asyncio
    async def test_aprobe_model_timeout(self, monkeypatch):
        def handler(request):
            raise httpx.ReadTimeout("boom")

        install_async_client(monkeypatch, "ollama_client", handler)

        probe = await OllamaClient().aprobe_model(make_provider(), "m")

        assert probe.healthy is False
        assert probe.status_code == 0
        assert probe.error == "timeout"
