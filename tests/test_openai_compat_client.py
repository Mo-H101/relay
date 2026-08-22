import json

import pytest

import httpx

from app.providers.base import Provider
from app.providers.exceptions import (
    ProviderHTTPError,
    ProviderTimeout,
)
from app.providers.nvidia_client import NvidiaClient
from app.providers.openai_client import OpenAIClient
from app.providers.openai_compat_client import (
    OpenAICompatibleClient,
    proxy_request_kwargs,
)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json


def make_provider(key="sk-test", base_url="https://api.example.com/v1"):
    return Provider(name="Test", base_url=base_url, api_key=key)


def patch_post(monkeypatch, response, recorded=None):
    def handler(url, **kwargs):
        if recorded is not None:
            recorded["url"] = url
            recorded["headers"] = kwargs.get("headers", {})
            recorded["json"] = kwargs.get("json")
            recorded["timeout"] = kwargs.get("timeout")
        return response

    monkeypatch.setattr(
        "app.providers.openai_compat_client.bounded_post",
        handler,
    )


def patch_get(monkeypatch, response, recorded=None):
    def handler(url, **kwargs):
        if recorded is not None:
            recorded["url"] = url
            recorded["headers"] = kwargs.get("headers", {})
        return response

    monkeypatch.setattr(
        "app.providers.openai_compat_client.bounded_get",
        handler,
    )


class TestSharedClientInheritance:
    def test_existing_clients_subclass_shared_client(self):
        assert issubclass(NvidiaClient, OpenAICompatibleClient)
        assert issubclass(OpenAIClient, OpenAICompatibleClient)
        assert NvidiaClient().name == "NVIDIA"
        assert OpenAIClient().name == "OpenAI"


class TestProxyRequestKwargsMethod:
    def test_method_matches_module_function(self):
        provider = make_provider()
        url = "https://api.example.com/v1/chat/completions"

        kwargs = OpenAICompatibleClient().proxy_request_kwargs(provider, url)

        assert kwargs == proxy_request_kwargs(provider, url)

    def test_forced_proxy_through_method(self):
        provider = Provider(
            name="Test",
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            proxy="http://proxy.internal:8080",
        )
        url = "https://api.example.com/v1/chat/completions"

        kwargs = OpenAICompatibleClient().proxy_request_kwargs(provider, url)

        assert kwargs["proxy"] == "http://proxy.internal:8080"
        assert kwargs["trust_env"] is False

    def test_empty_proxy_bypasses_through_method(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "proxy_enabled", True)
        monkeypatch.setattr(settings, "https_proxy", "http://global:8080")
        provider = Provider(
            name="Test",
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            proxy="",
        )
        url = "https://api.example.com/v1/chat/completions"

        kwargs = OpenAICompatibleClient().proxy_request_kwargs(provider, url)

        assert kwargs["proxy"] is None
        assert kwargs["trust_env"] is False

    def test_no_proxy_matches_exact_and_suffix_hosts_through_method(
        self, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "proxy_enabled", True)
        monkeypatch.setattr(settings, "http_proxy", "http://proxy:8080")
        monkeypatch.setattr(settings, "https_proxy", "http://proxy:8080")
        monkeypatch.setattr(
            settings, "no_proxy", "internal.invalid, .corp.example"
        )
        client = OpenAICompatibleClient()
        provider = make_provider()

        internal = client.proxy_request_kwargs(
            provider, "https://api.internal.invalid/x"
        )
        corp = client.proxy_request_kwargs(
            provider, "https://db.corp.example/x"
        )
        external = client.proxy_request_kwargs(
            provider, "https://api.example.com/x"
        )

        assert internal["proxy"] is None
        assert corp["proxy"] is None
        assert external["proxy"] == "http://proxy:8080"


class TestConnectivityProbe:
    def _capture_get(self, monkeypatch, response, recorded):
        def handler(url, **kwargs):
            recorded["url"] = url
            recorded["headers"] = kwargs.get("headers", {})
            recorded["timeout"] = kwargs.get("timeout")
            return response

        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_get", handler
        )

    def test_success_with_key_sends_bearer_and_times_out_at_ten(
        self, monkeypatch
    ):
        recorded = {}
        self._capture_get(monkeypatch, FakeResponse(status_code=200), recorded)

        ok, details, latency = OpenAICompatibleClient().connectivity_probe(
            make_provider()
        )

        assert ok is True
        assert details == "HTTP 200"
        assert isinstance(latency, int)
        assert recorded["url"] == "https://api.example.com/v1/models"
        assert recorded["headers"]["Authorization"] == "Bearer sk-test"
        assert recorded["timeout"] == 10

    def test_no_key_sends_no_auth_header(self, monkeypatch):
        recorded = {}
        self._capture_get(monkeypatch, FakeResponse(status_code=200), recorded)

        provider = Provider(
            name="Test", base_url="https://api.example.com/v1", api_key=""
        )

        ok, details, _ = OpenAICompatibleClient().connectivity_probe(provider)

        assert ok is True
        assert "Authorization" not in recorded["headers"]

    def test_http_error_status_is_failure(self, monkeypatch):
        recorded = {}
        self._capture_get(monkeypatch, FakeResponse(status_code=503), recorded)

        ok, details, _ = OpenAICompatibleClient().connectivity_probe(
            make_provider()
        )

        assert ok is False
        assert details == "HTTP 503"

    def test_connection_exception_returns_failure(self, monkeypatch):
        def handler(url, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_get", handler
        )

        ok, details, latency = OpenAICompatibleClient().connectivity_probe(
            make_provider()
        )

        assert ok is False
        assert "connection refused" in details
        assert isinstance(latency, int)

    def test_timeout_exception_returns_failure(self, monkeypatch):
        def handler(url, **kwargs):
            raise httpx.TimeoutException("timed out")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_get", handler
        )

        ok, details, _ = OpenAICompatibleClient().connectivity_probe(
            make_provider()
        )

        assert ok is False
        assert "timed out" in details


class TestChat:
    def test_chat_returns_message_content(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(200, {"choices": [{"message": {"content": "hello"}}]}),
            recorded,
        )

        result = OpenAICompatibleClient().chat(make_provider(), "m", "hi")

        assert result == "hello"
        assert recorded["url"] == "https://api.example.com/v1/chat/completions"
        assert recorded["json"]["model"] == "m"
        assert recorded["json"]["temperature"] == 0.2
        assert recorded["json"]["max_tokens"] == 512
        assert recorded["json"]["messages"] == [
            {"role": "user", "content": "hi"}
        ]
        assert recorded["headers"]["Authorization"] == "Bearer sk-test"
        assert recorded["headers"]["Content-Type"] == "application/json"

    def test_chat_omits_bearer_header_without_key(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(200, {"choices": [{"message": {"content": "x"}}]}),
            recorded,
        )

        OpenAICompatibleClient().chat(make_provider(key=""), "m", "hi")

        assert "Authorization" not in recorded["headers"]

    def test_chat_uses_configured_request_timeout(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(200, {"choices": [{"message": {"content": "x"}}]}),
            recorded,
        )

        OpenAICompatibleClient().chat(make_provider(), "m", "hi")

        assert recorded["timeout"] is not None

    def test_chat_raises_provider_http_error_on_4xx(self, monkeypatch):
        patch_post(monkeypatch, FakeResponse(400, text="bad request"))

        with pytest.raises(ProviderHTTPError) as exc:
            OpenAICompatibleClient().chat(make_provider(), "m", "hi")

        assert exc.value.status_code == 400

    def test_chat_captures_retry_after_seconds(self, monkeypatch):
        patch_post(
            monkeypatch,
            FakeResponse(429, text="slow down", headers={"Retry-After": "2"}),
        )

        with pytest.raises(ProviderHTTPError) as exc:
            OpenAICompatibleClient().chat(make_provider(), "m", "hi")

        assert exc.value.status_code == 429
        assert exc.value.retry_after == 2.0

    def test_chat_retry_after_defaults_to_none_when_absent(self, monkeypatch):
        patch_post(monkeypatch, FakeResponse(429, text="slow down"))

        with pytest.raises(ProviderHTTPError) as exc:
            OpenAICompatibleClient().chat(make_provider(), "m", "hi")

        assert exc.value.status_code == 429
        assert exc.value.retry_after is None

    def test_chat_retry_after_ignores_unparseable_header(self, monkeypatch):
        patch_post(
            monkeypatch,
            FakeResponse(
                429,
                text="slow down",
                headers={"Retry-After": "not-a-date-or-number"},
            ),
        )

        with pytest.raises(ProviderHTTPError) as exc:
            OpenAICompatibleClient().chat(make_provider(), "m", "hi")

        assert exc.value.retry_after is None

    def test_chat_messages_captures_retry_after(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(429, text="limited", headers={"Retry-After": "5"}),
            recorded,
        )

        with pytest.raises(ProviderHTTPError) as exc:
            OpenAICompatibleClient().chat_messages(
                make_provider(),
                {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            )

        assert exc.value.status_code == 429
        assert exc.value.retry_after == 5.0
        assert recorded["json"]["model"] == "m"

    def test_chat_raises_provider_timeout_on_read_timeout(self, monkeypatch):
        def handler(url, **kwargs):
            raise httpx.ReadTimeout("boom")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_post",
            handler,
        )

        with pytest.raises(ProviderTimeout):
            OpenAICompatibleClient().chat(make_provider(), "m", "hi")

    def test_chat_raises_provider_timeout_on_generic_timeout(self, monkeypatch):
        def handler(url, **kwargs):
            raise httpx.TimeoutException("boom")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_post",
            handler,
        )

        with pytest.raises(ProviderTimeout):
            OpenAICompatibleClient().chat(make_provider(), "m", "hi")

    def test_chat_raises_provider_http_error_on_transport_error(self, monkeypatch):
        def handler(url, **kwargs):
            raise httpx.HTTPError("boom")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_post",
            handler,
        )

        with pytest.raises(ProviderHTTPError) as exc:
            OpenAICompatibleClient().chat(make_provider(), "m", "hi")

        assert exc.value.status_code == 0


class TestListModels:
    def test_list_models_parses_ids(self, monkeypatch):
        recorded = {}
        patch_get(
            monkeypatch,
            FakeResponse(200, {"data": [{"id": "a"}, {"id": "b"}]}),
            recorded,
        )

        result = OpenAICompatibleClient().list_models(make_provider(key="sk-1"))

        assert result == ["a", "b"]
        assert recorded["url"] == "https://api.example.com/v1/models"
        assert recorded["headers"]["Authorization"] == "Bearer sk-1"

    def test_list_models_omits_auth_when_keyless(self, monkeypatch):
        recorded = {}
        patch_get(monkeypatch, FakeResponse(200, {"data": []}), recorded)

        OpenAICompatibleClient().list_models(make_provider(key=""))

        assert "Authorization" not in recorded["headers"]

    def test_list_models_raises_on_http_error(self, monkeypatch):
        patch_get(monkeypatch, FakeResponse(500, text="nope"))

        with pytest.raises(ProviderHTTPError) as exc:
            OpenAICompatibleClient().list_models(make_provider())

        assert exc.value.status_code == 500

    def test_list_models_raises_on_timeout(self, monkeypatch):
        def handler(url, **kwargs):
            raise httpx.TimeoutException("boom")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_get",
            handler,
        )

        with pytest.raises(ProviderTimeout):
            OpenAICompatibleClient().list_models(make_provider())


class TestProbeModel:
    def test_probe_model_healthy(self, monkeypatch):
        recorded = {}
        patch_post(
            monkeypatch,
            FakeResponse(200, {"choices": []}),
            recorded,
        )

        probe = OpenAICompatibleClient().probe_model(make_provider(), "m")

        assert probe.healthy is True
        assert probe.status_code == 200
        assert probe.error == ""
        assert recorded["json"]["max_tokens"] == 1
        assert recorded["json"]["messages"][0]["content"] == "ping"

    def test_probe_model_unhealthy_with_status(self, monkeypatch):
        patch_post(monkeypatch, FakeResponse(404, text="missing"))

        probe = OpenAICompatibleClient().probe_model(make_provider(), "m")

        assert probe.healthy is False
        assert probe.status_code == 404
        assert probe.error == "missing"

    def test_probe_model_timeout(self, monkeypatch):
        def handler(url, **kwargs):
            raise httpx.ReadTimeout("boom")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_post",
            handler,
        )

        probe = OpenAICompatibleClient().probe_model(make_provider(), "m")

        assert probe.healthy is False
        assert probe.status_code == 0
        assert probe.error == "timeout"

    def test_check_model_delegates_to_probe(self, monkeypatch):
        patch_post(
            monkeypatch,
            FakeResponse(200, {"choices": []}),
        )

        assert OpenAICompatibleClient().check_model(make_provider(), "m") is True


class TestProviderBodyRedaction:
    def test_error_body_redacts_api_key(self, monkeypatch):
        body = '{"error": {"message": "invalid api key sk-test-123"}}'
        patch_post(monkeypatch, FakeResponse(400, text=body))

        with pytest.raises(ProviderHTTPError) as exc:
            OpenAICompatibleClient().chat(
                make_provider(key="sk-test-123"), "m", "hi"
            )

        assert "sk-test-123" not in exc.value.message
        assert "[REDACTED]" in exc.value.message

    def test_error_body_is_truncated(self, monkeypatch):
        body = "x" * 500
        patch_post(monkeypatch, FakeResponse(500, text=body))

        with pytest.raises(ProviderHTTPError) as exc:
            OpenAICompatibleClient().chat(make_provider(), "m", "hi")

        assert len(exc.value.message) <= 203
        assert exc.value.message.endswith("...")

    def test_error_body_strips_control_characters(self, monkeypatch):
        body = "line1\x00\x1bline2"
        patch_post(monkeypatch, FakeResponse(400, text=body))

        with pytest.raises(ProviderHTTPError) as exc:
            OpenAICompatibleClient().chat(make_provider(), "m", "hi")

        assert "\x00" not in exc.value.message
        assert "\x1b" not in exc.value.message

    def test_error_body_survives_short_plain_text(self, monkeypatch):
        patch_post(monkeypatch, FakeResponse(400, text="bad request"))

        with pytest.raises(ProviderHTTPError) as exc:
            OpenAICompatibleClient().chat(make_provider(), "m", "hi")

        assert exc.value.message == "bad request"

    def test_probe_details_redacted_and_bounded(self, monkeypatch):
        body = "denied sk-test-123 " + "y" * 500
        patch_post(monkeypatch, FakeResponse(404, text=body))

        probe = OpenAICompatibleClient().probe_model(
            make_provider(key="sk-test-123"), "m"
        )

        assert probe.error
        assert "sk-test-123" not in probe.error
        assert len(probe.error) <= 203


class TestProviderSpecificWording:
    def test_nvidia_chat_timeout_wording(self, monkeypatch):
        def handler(url, **kwargs):
            raise httpx.ReadTimeout("boom")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_post",
            handler,
        )

        with pytest.raises(ProviderTimeout) as exc:
            NvidiaClient().chat(make_provider(), "m", "hi")

        assert str(exc.value) == "NVIDIA request timed out."

    def test_openai_chat_timeout_wording(self, monkeypatch):
        def handler(url, **kwargs):
            raise httpx.ReadTimeout("boom")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_post",
            handler,
        )

        with pytest.raises(ProviderTimeout) as exc:
            OpenAIClient().chat(make_provider(), "m", "hi")

        assert str(exc.value) == "OpenAI request timed out."

    def test_nvidia_discovery_timeout_wording(self, monkeypatch):
        def handler(url, **kwargs):
            raise httpx.TimeoutException("boom")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_get",
            handler,
        )

        with pytest.raises(ProviderTimeout) as exc:
            NvidiaClient().list_models(make_provider())

        assert str(exc.value) == "NVIDIA model discovery timed out."

    def test_openai_discovery_timeout_wording(self, monkeypatch):
        def handler(url, **kwargs):
            raise httpx.TimeoutException("boom")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_get",
            handler,
        )

        with pytest.raises(ProviderTimeout) as exc:
            OpenAIClient().list_models(make_provider())

        assert str(exc.value) == "OpenAI model discovery timed out."


# ---------------------------------------------------------------------------
# F-2: SSE parser regression tests — non-dict JSON chunks must not crash
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    """Minimal httpx streaming response for SSE parser tests."""

    def __init__(self, lines, status_code=200):
        self.status_code = status_code
        self._lines = list(lines)
        self.headers = {}

    def iter_lines(self):
        return iter(self._lines)

    def aiter_lines(self):
        async def _gen():
            for line in self._lines:
                yield line
        return _gen()


class _FakeStreamContext:
    """Context manager wrapping a _FakeStreamResponse."""

    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self._resp

    def __exit__(self, *a):
        pass


def _patch_stream(monkeypatch, response):
    def handler(*args, **kwargs):
        return _FakeStreamContext(response)
    monkeypatch.setattr(
        "app.providers.openai_compat_client.bounded_stream",
        handler,
    )


class TestSSEParserNonDictChunks:
    """chat_stream must skip non-dict JSON without raising."""

    @pytest.mark.parametrize("bad_json", [
        "null",
        "42",
        '"hello"',
        "[]",
        "{}",
        '{"choices": null}',
        '{"choices": [null]}',
    ])
    def test_chat_stream_skips_non_dict(self, monkeypatch, bad_json):
        lines = [
            f"data: {bad_json}",
            "data: [DONE]",
        ]
        _patch_stream(monkeypatch, _FakeStreamResponse(lines))

        chunks = list(
            OpenAICompatibleClient().chat_stream(
                make_provider(),
                model="m",
                message="hi",
            )
        )
        assert chunks == []

    def test_chat_stream_yields_valid_chunks(self, monkeypatch):
        valid = json.dumps({
            "choices": [{"delta": {"content": "ok"}}],
        })
        lines = [
            f"data: {valid}",
            "data: [DONE]",
        ]
        _patch_stream(monkeypatch, _FakeStreamResponse(lines))

        chunks = list(
            OpenAICompatibleClient().chat_stream(
                make_provider(),
                model="m",
                message="hi",
            )
        )
        assert chunks == ["ok"]

    def test_chat_stream_mixed_valid_and_invalid(self, monkeypatch):
        valid = json.dumps({
            "choices": [{"delta": {"content": "ok"}}],
        })
        lines = [
            "data: null",
            f"data: {valid}",
            "data: 42",
            f"data: {valid}",
            "data: [DONE]",
        ]
        _patch_stream(monkeypatch, _FakeStreamResponse(lines))

        chunks = list(
            OpenAICompatibleClient().chat_stream(
                make_provider(),
                model="m",
                message="hi",
            )
        )
        assert chunks == ["ok", "ok"]

    @pytest.mark.parametrize("bad_json", [
        "null",
        "42",
        '"hello"',
        "[]",
        "{}",
        '{"choices": null}',
    ])
    def test_chat_stream_messages_skips_non_dict(self, monkeypatch, bad_json):
        lines = [
            f"data: {bad_json}",
            "data: [DONE]",
        ]
        _patch_stream(monkeypatch, _FakeStreamResponse(lines))

        chunks = list(
            OpenAICompatibleClient().chat_stream_messages(
                make_provider(),
                payload={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            )
        )
        assert chunks == []

    def test_chat_stream_messages_yields_valid_chunks(self, monkeypatch):
        valid = json.dumps({
            "choices": [{"delta": {"content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })
        lines = [
            f"data: {valid}",
            "data: [DONE]",
        ]
        _patch_stream(monkeypatch, _FakeStreamResponse(lines))

        chunks = list(
            OpenAICompatibleClient().chat_stream_messages(
                make_provider(),
                payload={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            )
        )
        assert len(chunks) == 1
        assert chunks[0]["choices"][0]["delta"]["content"] == "ok"
