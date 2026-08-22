import asyncio

import pytest
from fastapi import Response
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from app.core.relay import Relay
from app.main import app as fastapi_app
from app.providers.base import Provider
from app.schemas.openai import OpenAIChatCompletionRequest
from app.services.admission_control import ChatAdmission

import app.api.chat
import app.api.openai


@pytest.fixture
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


@pytest.fixture
def wired_relay(monkeypatch):
    relay = Relay()
    relay.provider_manager.register(
        Provider(
            name="local",
            base_url="https://local.invalid",
            api_key="",
            requires_api_key=False,
            models=["a-1"],
        )
    )
    monkeypatch.setattr(app.api.chat, "relay", relay)
    monkeypatch.setattr(app.api.openai, "relay", relay)
    return relay


def _openai_payload(stream=False):
    return {
        "model": "a-1",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": stream,
    }


def test_openai_rejects_when_chat_capacity_is_full(
    client, wired_relay, monkeypatch
):
    controller = ChatAdmission(1)
    monkeypatch.setattr(
        app.api.openai.admission_control,
        "chat_admission",
        controller,
    )
    held = controller.try_acquire()

    response = client.post(
        "/v1/chat/completions",
        json=_openai_payload(),
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["code"] == "capacity_exhausted"
    assert response.json()["error"]["message"] == "Chat capacity unavailable."
    assert controller.active == 1

    held.release()
    assert controller.active == 0


def test_legacy_chat_rejection_does_not_change_health_behavior(
    client, wired_relay, monkeypatch
):
    controller = ChatAdmission(1)
    monkeypatch.setattr(
        app.api.chat.admission_control,
        "chat_admission",
        controller,
    )
    held = controller.try_acquire()

    chat_response = client.post("/chat", json={"message": "hello"})
    health_response = client.get("/health")

    assert chat_response.status_code == 503
    assert chat_response.json()["detail"] == "Chat capacity unavailable."
    assert chat_response.headers["Retry-After"] == "1"
    assert health_response.status_code == 200

    held.release()
    assert controller.active == 0


@pytest.mark.asyncio
async def test_stream_lease_is_held_until_stream_closes(wired_relay, monkeypatch):
    controller = ChatAdmission(1)
    monkeypatch.setattr(
        app.api.openai.admission_control,
        "chat_admission",
        controller,
    )

    stream_started = asyncio.Event()
    stream_continue = asyncio.Event()

    async def provider_stream():
        stream_started.set()
        yield {
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "hello"},
                    "finish_reason": None,
                }
            ]
        }
        await stream_continue.wait()

    class FakeAsyncChatService:
        async def achat_across_stream_messages(
            self, candidates, payload, max_retries, turn
        ):
            return {
                "success": True,
                "provider": "local",
                "model": "a-1",
                "stream_gen": provider_stream(),
                "attempts": [],
                "continuity": {},
            }

    monkeypatch.setattr(
        app.api.openai,
        "async_chat_svc",
        FakeAsyncChatService(),
    )

    result = await app.api.openai.openai_chat_completion(
        OpenAIChatCompletionRequest(**_openai_payload(stream=True)),
        response=Response(),
    )

    assert isinstance(result, StreamingResponse)
    iterator = result.body_iterator
    first_chunk = await anext(iterator)
    assert "hello" in first_chunk
    await stream_started.wait()
    assert controller.active == 1

    await iterator.aclose()
    assert controller.active == 0
