"""
Focused tests for the NVIDIA provider integration.

Covers provider creation, API-key loading, model discovery, the
OpenAI-compatible non-stream chat path, the streaming path (sync and
async), and timeout/error propagation. All network access is mocked;
nothing here touches the real NVIDIA endpoint.
"""

import json

import httpx
import pytest

from app.core.config import settings
from app.providers.base import Provider, apply_model_priority
from app.providers.exceptions import ProviderHTTPError, ProviderTimeout
from app.providers.nvidia import create_provider
from app.providers.nvidia_client import NvidiaClient
from app.providers.registry import PROVIDER_REGISTRY


def make_provider(key="sk-test", base_url="https://api.example.com/v1"):
    return Provider(name="NVIDIA", base_url=base_url, api_key=key)


def nvidia_client():
    return NvidiaClient()


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json


class FakeStreamResponse:
    """
    Minimal stand-in for the object returned by ``httpx.stream``.

    Supports ``iter_lines``, ``status_code``, ``headers``, ``read`` and
    ``text``, matching what chat_stream_messages() touches on the error
    and success paths.
    """

    def __init__(self, body, status_code=200, headers=None, text=None):
        self._body = body
        self.status_code = status_code
        self.headers = headers or {}
        self._text = text if text is not None else body

    def iter_lines(self):
        return iter(self._body.splitlines())

    def read(self):
        return self._body

    @property
    def text(self):
        return self._text


class FakeStreamCtx:
    """
    Context manager that yields a FakeStreamResponse so tests can use
    ``with httpx.stream(...) as response``.
    """

    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, *exc):
        return False


def patch_post(monkeypatch, response, recorded=None):
    def handler(url, **kwargs):
        if recorded is not None:
            recorded["url"] = url
            recorded["headers"] = kwargs.get("headers", {})
            recorded["json"] = kwargs.get("json")
            recorded["timeout"] = kwargs.get("timeout")
        return response

    monkeypatch.setattr(
        "app.providers.openai_compat_client.httpx.post",
        handler,
    )


def patch_get(monkeypatch, response, recorded=None):
    def handler(url, **kwargs):
        if recorded is not None:
            recorded["url"] = url
            recorded["headers"] = kwargs.get("headers", {})
        return response

    monkeypatch.setattr(
        "app.providers.openai_compat_client.httpx.get",
        handler,
    )


def patch_stream(monkeypatch, response, recorded=None):
    def handler(method, url, **kwargs):
        if recorded is not None:
            recorded["method"] = method
            recorded["url"] = url
            recorded["headers"] = kwargs.get("headers", {})
            recorded["json"] = kwargs.get("json")
            recorded["timeout"] = kwargs.get("timeout")
        return FakeStreamCtx(response)

    monkeypatch.setattr(
        "app.providers.openai_compat_client.httpx.stream",
        handler,
    )


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


class TestProviderCreation:
    def test_registry_entry_has_nvidia_config(self):
        defn = PROVIDER_REGISTRY["nvidia"]

        assert defn.id == "nvidia"
        assert defn.display_name == "NVIDIA NIM"
        assert defn.provider_name == "NVIDIA"
        assert defn.kind == "cloud"
        assert defn.requires_api_key is True
        assert defn.key_env == "NVIDIA_API_KEY"
        assert defn.enabled_env == "NVIDIA_ENABLED"
        assert defn.base_url_default == "https://integrate.api.nvidia.com/v1"
        assert defn.health_endpoint == "/models"
        assert defn.client_class is NvidiaClient

    def test_registry_build_provider(self):
        provider = PROVIDER_REGISTRY["nvidia"].build_provider(
            api_key="nvapi-test"
        )

        assert provider.name == "NVIDIA"
        assert provider.api_key == "nvapi-test"
        assert provider.base_url == "https://integrate.api.nvidia.com/v1"
        assert provider.enabled is True
        assert provider.has_api_key() is True

    def test_create_provider_without_key_stays_registered(self, monkeypatch):
        monkeypatch.setattr(settings, "nvidia_api_key", "")
        monkeypatch.setattr(settings, "nvidia_model_priority", [])

        provider = create_provider()

        assert provider.name == "NVIDIA"
        assert provider.base_url == "https://integrate.api.nvidia.com/v1"
        assert provider.has_api_key() is False
        assert provider.models == []
        assert provider.priority_models == []

    def test_create_provider_discovers_and_applies_priority(
        self, monkeypatch
    ):
        monkeypatch.setattr(settings, "nvidia_api_key", "nvapi-test")
        monkeypatch.setattr(
            settings,
            "nvidia_model_priority",
            ["meta/llama-3.1-8b-instruct"],
        )
        catalog = {
            "data": [
                {"id": "nvidia/nemotron-3-super-120b-a12b"},
                {"id": "meta/llama-3.1-8b-instruct"},
            ]
        }
        patch_get(monkeypatch, FakeResponse(200, catalog))

        provider = create_provider()

        assert provider.models[0] == "meta/llama-3.1-8b-instruct"
        assert set(provider.models) == {
            "meta/llama-3.1-8b-instruct",
            "nvidia/nemotron-3-super-120b-a12b",
        }
        assert provider.priority_models == ["meta/llama-3.1-8b-instruct"]

    def test_create_provider_discovery_failure_yields_no_models(
        self, monkeypatch
    ):
        monkeypatch.setattr(settings, "nvidia_api_key", "nvapi-test")
        monkeypatch.setattr(settings, "nvidia_model_priority", [])

        def handler(url, **kwargs):
            raise httpx.ConnectError("boom")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.httpx.get",
            handler,
        )

        provider = create_provider()

        assert provider.name == "NVIDIA"
        assert provider.models == []
        assert provider.priority_models == []

    def test_nvidia_client_reuses_openai_compatible_path(self):
        from app.providers.openai_compat_client import OpenAICompatibleClient

        assert issubclass(NvidiaClient, OpenAICompatibleClient)
        assert nvidia_client().name == "NVIDIA"


class TestApiKeyLoading:
    def test_settings_loads_key_from_environment(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-env-key")
        from app.core.config import Settings

        loaded = Settings()
        assert loaded.nvidia_api_key == "nvapi-env-key"

    def test_settings_defaults_key_to_empty(self, monkeypatch):
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        from app.core.config import Settings

        loaded = Settings()
        assert loaded.nvidia_api_key == ""

    def test_provider_uses_settings_key(self, monkeypatch):
        monkeypatch.setattr(settings, "nvidia_api_key", "nvapi-cfg")
        provider = create_provider()
        assert provider.api_key == "nvapi-cfg"


class TestModelDiscovery:
    def test_list_models_parses_ids(self, monkeypatch):
        recorded = {}
        patch_get(
            monkeypatch,
            FakeResponse(
                200,
                {
                    "data": [
                        {"id": "nvidia/nemotron-3-super-120b-a12b"},
                        {"id": "meta/llama-3.1-8b-instruct"},
                    ]
                },
            ),
            recorded,
        )

        models = nvidia_client().list_models(make_provider())

        assert models == [
            "nvidia/nemotron-3-super-120b-a12b",
            "meta/llama-3.1-8b-instruct",
        ]
        assert recorded["url"] == "https://api.example.com/v1/models"
        assert recorded["headers"]["Authorization"] == "Bearer sk-test"

    def test_list_models_omits_auth_without_key(self, monkeypatch):
        recorded = {}
        patch_get(monkeypatch, FakeResponse(200, {"data": []}), recorded)

        nvidia_client().list_models(make_provider(key=""))

        assert "Authorization" not in recorded["headers"]

    def test_list_models_raises_on_http_error(self, monkeypatch):
        patch_get(monkeypatch, FakeResponse(400, text="bad request"))

        with pytest.raises(ProviderHTTPError) as exc:
            nvidia_client().list_models(make_provider())

        assert exc.value.status_code == 400

    def test_list_models_raises_timeout(self, monkeypatch):
        def handler(url, **kwargs):
            raise httpx.TimeoutException("boom")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.httpx.get",
            handler,
        )

        with pytest.raises(ProviderTimeout) as exc:
            nvidia_client().list_models(make_provider())

        assert str(exc.value) == "NVIDIA model discovery timed out."

    @pytest.mark.asyncio
    async def test_alist_models_parses_ids(self, monkeypatch):
        catalog = {
            "data": [
                {"id": "nvidia/nemotron-3-super-120b-a12b"},
                {"id": "meta/llama-3.1-8b-instruct"},
            ]
        }

        def handler(request):
            return httpx.Response(200, json=catalog, request=request)

        install_async_client(monkeypatch, "openai_compat_client", handler)

        models = await nvidia_client().alist_models(make_provider())

        assert models == [
            "nvidia/nemotron-3-super-120b-a12b",
            "meta/llama-3.1-8b-instruct",
        ]

    @pytest.mark.asyncio
    async def test_alist_models_raises_on_http_error(self, monkeypatch):
        def handler(request):
            return httpx.Response(401, text="invalid key", request=request)

        install_async_client(monkeypatch, "openai_compat_client", handler)

        with pytest.raises(ProviderHTTPError) as exc:
            await nvidia_client().alist_models(make_provider())

        assert exc.value.status_code == 401

    def test_apply_model_priority_orders_without_dropping(self):
        models = [
            "nvidia/nemotron-3-super-120b-a12b",
            "meta/llama-3.1-8b-instruct",
        ]
        ordered = apply_model_priority(
            models,
            ["meta/llama-3.1-8b-instruct"],
        )

        assert ordered[0] == "meta/llama-3.1-8b-instruct"
        assert set(ordered) == set(models)


class TestNonStreamChat:
    def test_chat_returns_content(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(200, {"choices": [{"message": {"content": "hi"}}]}),
            recorded,
        )

        result = nvidia_client().chat(make_provider(), "m", "hello")

        assert result == "hi"
        assert recorded["url"] == (
            "https://api.example.com/v1/chat/completions"
        )
        assert recorded["json"]["model"] == "m"
        assert recorded["json"]["messages"] == [
            {"role": "user", "content": "hello"}
        ]
        assert recorded["headers"]["Authorization"] == "Bearer sk-test"

    def test_chat_raises_http_error(self, monkeypatch):
        patch_post(monkeypatch, FakeResponse(429, text="slow down"))

        with pytest.raises(ProviderHTTPError) as exc:
            nvidia_client().chat(make_provider(), "m", "hi")

        assert exc.value.status_code == 429

    def test_chat_raises_timeout(self, monkeypatch):
        def handler(url, **kwargs):
            raise httpx.ReadTimeout("boom")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.httpx.post",
            handler,
        )

        with pytest.raises(ProviderTimeout) as exc:
            nvidia_client().chat(make_provider(), "m", "hi")

        assert str(exc.value) == "NVIDIA request timed out."

    @pytest.mark.asyncio
    async def test_achat_returns_content(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["url"] = request_url(request)
            seen["headers"] = dict(request.headers)
            seen["json"] = request_json(request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "async hi"}}]},
                request=request,
            )

        install_async_client(monkeypatch, "openai_compat_client", handler)

        result = await nvidia_client().achat(
            make_provider(), "m", "hello async"
        )

        assert result == "async hi"
        assert seen["url"].endswith("/chat/completions")
        assert seen["json"]["model"] == "m"
        assert seen["headers"]["authorization"] == "Bearer sk-test"

    @pytest.mark.asyncio
    async def test_achat_raises_timeout(self, monkeypatch):
        def handler(request):
            raise httpx.ReadTimeout("boom")

        install_async_client(monkeypatch, "openai_compat_client", handler)

        with pytest.raises(ProviderTimeout):
            await nvidia_client().achat(make_provider(), "m", "hi")


class TestStreamingPath:
    def test_chat_stream_yields_content_deltas(self, monkeypatch):
        body = (
            'data: {"choices": [{"delta": {"content": "Hel"}}]}\n\n'
            'data: {"choices": [{"delta": {"content": "lo"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        recorded = {}
        patch_stream(
            monkeypatch,
            FakeStreamResponse(body),
            recorded,
        )

        chunks = list(
            nvidia_client().chat_stream(make_provider(), "m", "hi")
        )

        assert chunks == ["Hel", "lo"]
        assert recorded["method"] == "POST"
        assert recorded["url"] == (
            "https://api.example.com/v1/chat/completions"
        )
        assert recorded["json"]["stream"] is True
        assert recorded["timeout"] is not None

    def test_chat_stream_skips_malformed_lines(self, monkeypatch):
        body = (
            "data: not-json\n\n"
            'data: {"choices": [{"delta": {"content": "Hi"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        patch_stream(monkeypatch, FakeStreamResponse(body))

        chunks = list(
            nvidia_client().chat_stream(make_provider(), "m", "hi")
        )

        assert chunks == ["Hi"]

    def test_chat_stream_messages_yields_chunk_dicts(self, monkeypatch):
        body = (
            'data: {"choices": [{"delta": {"content": "a"}}]}\n\n'
            'data: {"usage": {"total_tokens": 5}}\n\n'
            "data: [DONE]\n\n"
        )
        patch_stream(monkeypatch, FakeStreamResponse(body))

        chunks = list(
            nvidia_client().chat_stream_messages(
                make_provider(),
                {"model": "m", "stream": True},
            )
        )

        assert len(chunks) == 2
        assert chunks[0]["choices"][0]["delta"]["content"] == "a"
        assert chunks[1]["usage"]["total_tokens"] == 5

    def test_chat_stream_raises_http_error(self, monkeypatch):
        patch_stream(
            monkeypatch,
            FakeStreamResponse(
                body="",
                status_code=524,
                text="Error origin timeout",
            ),
        )

        with pytest.raises(ProviderHTTPError) as exc:
            list(
                nvidia_client().chat_stream_messages(
                    make_provider(),
                    {"model": "m", "stream": True},
                )
            )

        assert exc.value.status_code == 524

    def test_chat_stream_raises_timeout(self, monkeypatch):
        def handler(method, url, **kwargs):
            raise httpx.ReadTimeout("boom")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.httpx.stream",
            handler,
        )

        with pytest.raises(ProviderTimeout) as exc:
            list(
                nvidia_client().chat_stream_messages(
                    make_provider(),
                    {"model": "m", "stream": True},
                )
            )

        assert str(exc.value) == "NVIDIA request timed out."

    @pytest.mark.asyncio
    async def test_achat_stream_messages_yields_chunk_dicts(
        self, monkeypatch
    ):
        body = (
            'data: {"choices": [{"delta": {"content": "a"}}]}\n\n'
            'data: {"usage": {"total_tokens": 5}}\n\n'
            "data: [DONE]\n\n"
        )

        def handler(request):
            seen = request_json(request)
            assert seen["stream"] is True
            return httpx.Response(200, text=body, request=request)

        install_async_client(monkeypatch, "openai_compat_client", handler)

        stream = nvidia_client().achat_stream_messages(
            make_provider(),
            {"model": "m", "stream": True},
        )
        chunks = [chunk async for chunk in stream]

        assert len(chunks) == 2
        assert chunks[0]["choices"][0]["delta"]["content"] == "a"
        assert chunks[1]["usage"]["total_tokens"] == 5

    @pytest.mark.asyncio
    async def test_achat_stream_raises_redacted_http_error(
        self, monkeypatch
    ):
        body = '{"error": "invalid key sk-test-123"}'

        def handler(request):
            return httpx.Response(
                401,
                text=body,
                headers={"Retry-After": "3"},
                request=request,
            )

        install_async_client(monkeypatch, "openai_compat_client", handler)

        stream = nvidia_client().achat_stream_messages(
            make_provider(key="sk-test-123"),
            {"model": "m", "stream": True},
        )

        with pytest.raises(ProviderHTTPError) as exc:
            await anext(stream)

        assert exc.value.status_code == 401
        assert exc.value.retry_after == 3.0
        assert "sk-test-123" not in exc.value.message
        assert "[REDACTED]" in exc.value.message

    @pytest.mark.asyncio
    async def test_achat_stream_messages_raises_timeout(self, monkeypatch):
        def handler(request):
            raise httpx.ReadTimeout("boom")

        install_async_client(monkeypatch, "openai_compat_client", handler)

        stream = nvidia_client().achat_stream_messages(
            make_provider(),
            {"model": "m", "stream": True},
        )

        with pytest.raises(ProviderTimeout) as exc:
            await anext(stream)

        assert str(exc.value) == "NVIDIA request timed out."

    @pytest.mark.asyncio
    async def test_achat_stream_uses_configured_timeout(self, monkeypatch):
        def handler(request):
            return httpx.Response(
                200,
                text="data: [DONE]\n\n",
                request=request,
            )

        spies = install_async_client(
            monkeypatch, "openai_compat_client", handler
        )

        stream = nvidia_client().achat_stream_messages(
            make_provider(),
            {"model": "m", "stream": True},
        )
        [chunk async for chunk in stream]

        assert spies[0].calls[0][0] == "stream"
        assert spies[0].calls[0][3]["timeout"] is not None
