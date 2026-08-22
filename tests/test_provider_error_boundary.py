import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.relay import Relay
from app.main import app as fastapi_app
from app.providers.base import ModelProbe, Provider
from app.providers.exceptions import ProviderHTTPError
from app.services.async_chat_service import AsyncChatService
from app.services.chat_service import ChatService
from app.services.health_checker import HealthChecker
from app.services.redaction import (
    safe_provider_error,
    safe_provider_health_detail,
    safe_provider_result_error,
)

import app.api.chat
import app.api.openai


SECRET = "RELAY_AUDIT_SECRET_PROVIDER_BOUNDARY_123456"
RAW_PROVIDER_TEXT = (
    "provider body prompt=never-persist-this "
    f"secret={SECRET} "
    "url=https://user:password@provider.invalid/v1"
)


def _provider():
    return Provider(
        name="local",
        base_url="https://local.invalid",
        api_key="",
        requires_api_key=False,
        models=["a-1"],
    )


def _failure_result(kind="server_error"):
    return {
        "success": False,
        "provider": "local",
        "model": "a-1",
        "error": RAW_PROVIDER_TEXT,
        "attempts": [{"failure_type": kind}],
    }


def test_safe_provider_messages_never_include_exception_text():
    exc = ProviderHTTPError(500, RAW_PROVIDER_TEXT)

    message = safe_provider_error(exc)
    result_message = safe_provider_result_error(_failure_result())

    assert message == "Provider returned a server error."
    assert result_message == "Provider returned a server error."
    for value in (message, result_message):
        assert SECRET not in value
        assert "provider body" not in value
        assert "user:password" not in value


def test_sync_chat_result_contains_only_safe_failure_metadata(monkeypatch):
    class ExplodingClient:
        def chat_messages(self, provider, payload):
            raise ProviderHTTPError(500, RAW_PROVIDER_TEXT)

    service = ChatService()
    monkeypatch.setattr(service.registry, "get", lambda name: ExplodingClient())

    result = service.chat_across_messages(
        [(_provider(), "a-1")],
        {"messages": []},
        max_retries=0,
    )

    serialized = json.dumps(result)
    assert result["attempts"][0]["failure_type"] == "server_error"
    assert result["attempts"][0]["reason"] == "Provider returned a server error."
    assert "Provider returned a server error." in result["error"]
    assert SECRET not in serialized
    assert "provider body" not in serialized
    assert "user:password" not in serialized


@pytest.mark.asyncio
async def test_async_chat_result_contains_only_safe_failure_metadata(monkeypatch):
    class ExplodingClient:
        async def achat_messages(self, provider, payload):
            raise ProviderHTTPError(500, RAW_PROVIDER_TEXT)

    service = AsyncChatService()
    monkeypatch.setattr(service.registry, "get", lambda name: ExplodingClient())

    result = await service.achat_across_messages(
        [(_provider(), "a-1")],
        {"messages": []},
        max_retries=0,
    )

    serialized = json.dumps(result)
    assert result["attempts"][0]["failure_type"] == "server_error"
    assert result["attempts"][0]["reason"] == "Provider returned a server error."
    assert SECRET not in serialized
    assert "provider body" not in serialized
    assert "user:password" not in serialized


def test_health_boundary_keeps_provider_details_safe():
    assert safe_provider_health_detail(RAW_PROVIDER_TEXT, 503) == (
        "Provider returned a server error."
    )

    checker = HealthChecker()
    checker.registry.get = lambda name: SimpleNamespace(
        connectivity_probe=lambda provider: (True, RAW_PROVIDER_TEXT, 1),
        probe_model=lambda provider, model: ModelProbe(
            False, 1, 503, RAW_PROVIDER_TEXT
        ),
    )
    provider = _provider()
    report = checker.check(provider, deep=True)

    assert report.details == "Provider request failed."
    assert report.models[0].error == "Provider returned a server error."


@pytest.fixture
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


def test_openai_error_boundary_redacts_result_text(client, monkeypatch):
    relay = Relay()
    relay.provider_manager.register(_provider())
    monkeypatch.setattr(app.api.openai, "relay", relay)

    class FakeAsyncChatService:
        async def achat_across_messages(self, *args, **kwargs):
            return _failure_result("server_error")

    monkeypatch.setattr(app.api.openai, "async_chat_svc", FakeAsyncChatService())

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "a-1",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 502
    body = response.text
    assert "Provider returned a server error." in body
    assert SECRET not in body
    assert "provider body" not in body
    assert "user:password" not in body


def test_legacy_chat_error_boundary_redacts_result_text(client, monkeypatch):
    async def fake_achat(*args, **kwargs):
        return _failure_result("auth_error")

    monkeypatch.setattr(
        app.api.chat,
        "relay",
        SimpleNamespace(achat=fake_achat),
    )

    response = client.post("/chat", json={"message": "hello"})

    assert response.status_code == 502
    body = response.text
    assert "Provider authentication failed." in body
    assert SECRET not in body
    assert "provider body" not in body
    assert "user:password" not in body


def test_openai_stream_error_boundary_preserves_sse_and_redacts(
    client, monkeypatch
):
    relay = Relay()
    relay.provider_manager.register(_provider())
    monkeypatch.setattr(app.api.openai, "relay", relay)

    async def provider_stream():
        yield {
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "hello"},
                    "finish_reason": None,
                }
            ]
        }
        raise ProviderHTTPError(500, RAW_PROVIDER_TEXT)

    class FakeAsyncChatService:
        async def achat_across_stream_messages(self, *args, **kwargs):
            return {
                "success": True,
                "provider": "local",
                "model": "a-1",
                "stream_gen": provider_stream(),
                "attempts": [],
                "continuity": {},
            }

    monkeypatch.setattr(app.api.openai, "async_chat_svc", FakeAsyncChatService())

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "a-1",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
    ) as response:
        body = response.read().decode()

    assert response.status_code == 200
    assert "stream_error" in body
    assert "[DONE]" in body
    assert "Provider returned a server error." in body
    assert SECRET not in body
    assert "provider body" not in body
    assert "user:password" not in body
