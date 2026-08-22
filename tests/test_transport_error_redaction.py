"""
Phase 13a/13b regression tests: transport-error and API-error redaction.

Proves that raw httpx.HTTPError exception text carrying credentials or
API-key material is sanitized before it reaches ProviderHTTPError,
attempt records, ModelProbe, connectivity_probe, key_check, or
non-streaming API responses.
"""

import httpx
import pytest

from app.providers.base import Provider
from app.providers.exceptions import ProviderHTTPError
from app.providers.openai_compat_client import OpenAICompatibleClient
from app.providers.anthropic_client import AnthropicClient
from app.providers.gemini_client import GeminiClient
from app.services.redaction import redact_text


def make_openai_provider(key="sk-test-api-key-12345"):
    return Provider(
        name="Test",
        base_url="https://api.example.com/v1",
        api_key=key,
    )


def make_anthropic_provider(key="sk-ant-test-key-abc123"):
    return Provider(
        name="Test",
        base_url="https://api.anthropic.com/v1",
        api_key=key,
    )


def make_gemini_provider(key="gemini-test-key-abc123"):
    return Provider(
        name="Test",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key=key,
    )


class TestTransportErrorRedactionOpenAICompat:
    """OpenAI-compatible client transport-error messages are redacted."""

    def test_chat_transport_error_redacts_api_key(self, monkeypatch):
        """chat() httpx.HTTPError message must not contain the API key."""
        secret = "sk-super-secret-key-abc"

        def handler(url, **kwargs):
            raise httpx.HTTPError(
                f"connection failed to {url} with key {secret}"
            )

        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_post", handler
        )

        with pytest.raises(ProviderHTTPError) as exc:
            OpenAICompatibleClient().chat(
                make_openai_provider(secret), "m", "hi"
            )

        assert exc.value.status_code == 0
        assert secret not in exc.value.message
        assert "<redacted>" in exc.value.message

    def test_chat_messages_transport_error_redacts_api_key(self, monkeypatch):
        """chat_messages() transport error must not leak the API key."""
        secret = "sk-leaked-in-url-key"

        def handler(url, **kwargs):
            raise httpx.HTTPError(
                f"SSL error near {url} auth={secret}"
            )

        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_post", handler
        )

        with pytest.raises(ProviderHTTPError) as exc:
            OpenAICompatibleClient().chat_messages(
                make_openai_provider(secret),
                {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            )

        assert secret not in exc.value.message

    def test_list_models_transport_error_redacts_api_key(self, monkeypatch):
        """list_models() transport error must not leak the API key."""
        secret = "sk-list-models-secret"

        def handler(url, **kwargs):
            raise httpx.HTTPError(f"DNS failed for {url} key={secret}")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_get", handler
        )

        with pytest.raises(ProviderHTTPError) as exc:
            OpenAICompatibleClient().list_models(make_openai_provider(secret))

        assert secret not in exc.value.message

    def test_key_check_transport_error_redacts_api_key(self, monkeypatch):
        """key_check() transport error must not leak the API key."""
        secret = "sk-keycheck-secret-xyz"

        def handler(url, **kwargs):
            raise httpx.HTTPError(f"connection reset key={secret}")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_get", handler
        )

        status, error = OpenAICompatibleClient().key_check(
            make_openai_provider(secret)
        )

        assert status is None
        assert secret not in error

    def test_probe_model_transport_error_redacts_api_key(self, monkeypatch):
        """probe_model() transport error must not leak the API key."""
        secret = "sk-probe-secret-789"

        def handler(url, **kwargs):
            raise httpx.HTTPError(f"connect timeout at {url} bearer={secret}")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_post", handler
        )

        probe = OpenAICompatibleClient().probe_model(
            make_openai_provider(secret), "m"
        )

        assert probe.healthy is False
        assert secret not in probe.error

    def test_connectivity_probe_redacts_api_key(self, monkeypatch):
        """connectivity_probe() exception details must not leak the key."""
        secret = "sk-connectivity-secret"

        def handler(url, **kwargs):
            raise httpx.ConnectError(f"refused at {url} key={secret}")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_get", handler
        )

        ok, details, _ = OpenAICompatibleClient().connectivity_probe(
            make_openai_provider(secret)
        )

        assert ok is False
        assert secret not in details

    def test_transport_error_preserves_safe_message(self, monkeypatch):
        """A benign transport error message is preserved (minus any secrets)."""
        def handler(url, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_post", handler
        )

        with pytest.raises(ProviderHTTPError) as exc:
            OpenAICompatibleClient().chat(make_openai_provider(), "m", "hi")

        assert "connection refused" in exc.value.message

    def test_transport_error_with_bearer_token_redacted(self, monkeypatch):
        """Exception text containing a Bearer token is redacted."""
        token = "bearer-super-secret-token-value"

        def handler(url, **kwargs):
            raise httpx.HTTPError(
                f"auth failed: Bearer {token}"
            )

        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_post", handler
        )

        with pytest.raises(ProviderHTTPError) as exc:
            OpenAICompatibleClient().chat(make_openai_provider(), "m", "hi")

        assert token not in exc.value.message


class TestTransportErrorRedactionAnthropic:
    """Anthropic client transport-error messages are redacted."""

    def test_chat_transport_error_redacts_api_key(self, monkeypatch):
        """chat() httpx.HTTPError message must not contain the API key."""
        secret = "sk-ant-transport-secret-123"

        def handler(url, **kwargs):
            raise httpx.HTTPError(
                f"connection failed key={secret}"
            )

        monkeypatch.setattr(
            "app.providers.anthropic_client.bounded_post", handler
        )

        with pytest.raises(ProviderHTTPError) as exc:
            AnthropicClient().chat(make_anthropic_provider(secret), "m", "hi")

        assert exc.value.status_code == 0
        assert secret not in exc.value.message

    def test_chat_messages_transport_error_redacts_api_key(self, monkeypatch):
        """chat_messages() transport error must not leak the API key."""
        secret = "sk-ant-msg-secret"

        def handler(url, **kwargs):
            raise httpx.HTTPError(f"network error key={secret}")

        monkeypatch.setattr(
            "app.providers.anthropic_client.bounded_post", handler
        )

        with pytest.raises(ProviderHTTPError) as exc:
            AnthropicClient().chat_messages(
                make_anthropic_provider(secret),
                {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            )

        assert secret not in exc.value.message

    def test_list_models_transport_error_redacts_api_key(self, monkeypatch):
        """list_models() transport error must not leak the API key."""
        secret = "sk-ant-list-secret"

        def handler(url, **kwargs):
            raise httpx.HTTPError(f"DNS error key={secret}")

        monkeypatch.setattr(
            "app.providers.anthropic_client.bounded_get", handler
        )

        with pytest.raises(ProviderHTTPError) as exc:
            AnthropicClient().list_models(make_anthropic_provider(secret))

        assert secret not in exc.value.message

    def test_key_check_transport_error_redacts_api_key(self, monkeypatch):
        """key_check() transport error must not leak the API key."""
        secret = "sk-ant-keycheck-secret"

        def handler(url, **kwargs):
            raise httpx.HTTPError(f"reset key={secret}")

        monkeypatch.setattr(
            "app.providers.anthropic_client.bounded_get", handler
        )

        status, error = AnthropicClient().key_check(
            make_anthropic_provider(secret)
        )

        assert status is None
        assert secret not in error

    def test_probe_model_transport_error_redacts_api_key(self, monkeypatch):
        """probe_model() transport error must not leak the API key."""
        secret = "sk-ant-probe-secret"

        def handler(url, **kwargs):
            raise httpx.HTTPError(f"timeout key={secret}")

        monkeypatch.setattr(
            "app.providers.anthropic_client.bounded_post", handler
        )

        probe = AnthropicClient().probe_model(
            make_anthropic_provider(secret), "m"
        )

        assert probe.healthy is False
        assert secret not in probe.error

    def test_connectivity_probe_redacts_api_key(self, monkeypatch):
        """connectivity_probe() exception details must not leak the key."""
        secret = "sk-ant-connect-secret"

        def handler(url, **kwargs):
            raise httpx.ConnectError(f"refused key={secret}")

        monkeypatch.setattr(
            "app.providers.anthropic_client.bounded_get", handler
        )

        ok, details, _ = AnthropicClient().connectivity_probe(
            make_anthropic_provider(secret)
        )

        assert ok is False
        assert secret not in details

    def test_transport_error_preserves_safe_message(self, monkeypatch):
        """A benign transport error message is preserved."""
        def handler(url, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(
            "app.providers.anthropic_client.bounded_post", handler
        )

        with pytest.raises(ProviderHTTPError) as exc:
            AnthropicClient().chat(make_anthropic_provider(), "m", "hi")

        assert "connection refused" in exc.value.message


@pytest.mark.parametrize(
    ("client_path", "provider_factory"),
    [
        ("app.providers.openai_compat_client.bounded_get", make_openai_provider),
        ("app.providers.anthropic_client.bounded_get", make_anthropic_provider),
        ("app.providers.gemini_client.bounded_get", make_gemini_provider),
    ],
)
def test_key_check_does_not_return_provider_error_body(
    monkeypatch, client_path, provider_factory
):
    """Key validation must not copy an untrusted provider body to callers."""
    secret = "RELAY_AUDIT_PROVIDER_BODY_SECRET"
    body = f"provider detail prompt=do-not-export key={secret}"

    def handler(url, **kwargs):
        return httpx.Response(
            401,
            text=body,
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(client_path, handler)
    client_type = {
        "app.providers.openai_compat_client.bounded_get": OpenAICompatibleClient,
        "app.providers.anthropic_client.bounded_get": AnthropicClient,
        "app.providers.gemini_client.bounded_get": GeminiClient,
    }[client_path]

    status, error = client_type().key_check(provider_factory(secret))

    assert status == 401
    assert secret not in error
    assert "do-not-export" not in error


class TestNonStreamingAPIErrorRedaction:
    """Non-streaming API error path applies redact_text to exception messages."""

    def test_redact_text_removes_sk_keys(self):
        """redact_text strips sk-* API keys from transport error text."""
        key = "sk-test-api-key-1234567890"
        result = redact_text(f"error connecting: {key}")
        assert key not in result

    def test_redact_text_removes_nvapi_keys(self):
        """redact_text strips nvapi-* API keys from transport error text."""
        key = "nvapi-1234567890abcdef"
        result = redact_text(f"error: {key}")
        assert key not in result

    def test_redact_text_removes_bearer_tokens(self):
        """redact_text strips Bearer tokens from transport error text."""
        token = "my-super-secret-bearer-token-value"
        result = redact_text(f"auth failed: Bearer {token}")
        assert token not in result

    def test_redact_text_preserves_innocuous_messages(self):
        """redact_text does not alter messages without secrets."""
        msg = "connection refused to api.example.com:443"
        assert redact_text(msg) == msg

    def test_redact_text_handles_empty_string(self):
        """redact_text handles empty input."""
        assert redact_text("") == ""
