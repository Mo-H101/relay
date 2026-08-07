"""
OpenAI SDK compatibility conformance tests (P0 release requirement).

These tests verify that a stock OpenAI SDK client works against Relay's
/v1 API by changing only base_url and api_key. The SDK's http_client is
wired to Relay's ASGI app purely to avoid a real socket; the wire
protocol is unchanged, and Relay still makes real HTTP calls to the
mock upstream provider.

Fails collection when the openai package is not installed (a pinned dev
dependency): a missing SDK must fail this release conformance check, not
skip it.
"""
import asyncio

import pytest

try:
    import openai  # required conformance dependency (pinned in requirements-dev.txt)
except ImportError as exc:  # pragma: no cover - missing-dev-dep failure path
    raise RuntimeError(
        "OpenAI SDK conformance suite requires the 'openai' SDK (pinned in "
        "requirements-dev.txt as openai==2.52.0); a missing SDK must fail "
        "this release check, not skip it. Install dev dependencies and re-run."
    ) from exc

import httpx

from app.core.relay import Relay
from app.main import app as fastapi_app

from tests.conformance_helpers import (
    DEFAULT_MODEL,
    MockOpenAIProvider,
    make_provider,
)

import app.api.openai


@pytest.fixture
def mock_provider_server():
    mock = MockOpenAIProvider()
    mock.start()
    yield mock
    mock.stop()


@pytest.fixture
def relay_with_mock(monkeypatch):
    relays = []

    def _build(mock):
        provider = make_provider(name="OpenAI", base_url=mock.base_url)
        relay_obj = Relay()
        relay_obj.provider_manager.register(provider)
        monkeypatch.setattr(app.api.openai, "relay", relay_obj)
        relays.append(relay_obj)
        return relay_obj

    yield _build


def _sdk_client() -> openai.AsyncOpenAI:
    transport = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fastapi_app),
        base_url="http://relay-sdk-test",
    )
    return openai.AsyncOpenAI(
        base_url="http://relay-sdk-test/v1",
        api_key="test-key",
        http_client=transport,
    )


def _content_stream() -> list:
    return [
        {
            "id": "chatcmpl-mock-1",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": DEFAULT_MODEL,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "Hel"},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-mock-1",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": DEFAULT_MODEL,
            "choices": [
                {"index": 0, "delta": {"content": "lo"}, "finish_reason": None}
            ],
        },
        {
            "id": "chatcmpl-mock-1",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": DEFAULT_MODEL,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    ]


class TestOpenAISDKCompatibility:
    def test_sdk_non_stream(self, relay_with_mock, mock_provider_server):
        mock = mock_provider_server
        mock.script(
            json_body={
                "id": "chatcmpl-mock-1",
                "object": "chat.completion",
                "created": 1700000000,
                "model": DEFAULT_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        )
        relay_with_mock(mock)

        async def run():
            client = _sdk_client()
            try:
                return await client.chat.completions.create(
                    model=DEFAULT_MODEL,
                    messages=[{"role": "user", "content": "hello"}],
                )
            finally:
                await client.close()

        resp = asyncio.run(run())
        assert resp.model == DEFAULT_MODEL
        assert resp.choices[0].message.role == "assistant"
        assert resp.choices[0].message.content == "ok"
        assert resp.choices[0].finish_reason == "stop"
        assert resp.usage.total_tokens == 2

    def test_sdk_stream(self, relay_with_mock, mock_provider_server):
        mock = mock_provider_server
        mock.script(stream=_content_stream())
        relay_with_mock(mock)

        async def run():
            client = _sdk_client()
            try:
                chunks = []
                stream = await client.chat.completions.create(
                    model=DEFAULT_MODEL,
                    messages=[{"role": "user", "content": "hi"}],
                    stream=True,
                )
                async for chunk in stream:
                    chunks.append(chunk)
                return chunks
            finally:
                await client.close()

        chunks = asyncio.run(run())
        text = "".join(
            c.choices[0].delta.content or ""
            for c in chunks
            if c.choices
        )
        assert text == "Hello"
        ids = {c.id for c in chunks}
        assert len(ids) == 1, "SDK stream must see one stable chunk id"
        assert chunks[0].choices[0].delta.role == "assistant"

    def test_sdk_stream_usage(self, relay_with_mock, mock_provider_server):
        mock = mock_provider_server
        mock.script(
            stream=_content_stream()
            + [
                {
                    "id": "chatcmpl-mock-1",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": DEFAULT_MODEL,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 1,
                        "total_tokens": 3,
                    },
                }
            ]
        )
        relay_with_mock(mock)

        async def run():
            client = _sdk_client()
            try:
                chunks = []
                stream = await client.chat.completions.create(
                    model=DEFAULT_MODEL,
                    messages=[{"role": "user", "content": "hi"}],
                    stream=True,
                    stream_options={"include_usage": True},
                )
                async for chunk in stream:
                    chunks.append(chunk)
                return chunks
            finally:
                await client.close()

        chunks = asyncio.run(run())
        usage_chunks = [c for c in chunks if c.usage is not None]
        assert len(usage_chunks) == 1
        assert usage_chunks[0].usage.total_tokens == 3

    def test_sdk_tool_calling_round_trip(
        self, relay_with_mock, mock_provider_server
    ):
        mock = mock_provider_server
        mock.script(
            json_body={
                "id": "chatcmpl-mock-tool",
                "object": "chat.completion",
                "created": 1700000000,
                "model": DEFAULT_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_2",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": '{"city": "Lyon"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 6,
                    "total_tokens": 14,
                },
            }
        )
        relay_with_mock(mock)

        async def run():
            client = _sdk_client()
            try:
                return await client.chat.completions.create(
                    model=DEFAULT_MODEL,
                    messages=[
                        {"role": "user", "content": "Weather in Paris?"},
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
                            "content": '{"temp": 20}',
                        },
                        {"role": "user", "content": "Thanks!"},
                    ],
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "description": "Get weather.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {},
                                },
                            },
                        }
                    ],
                    tool_choice="auto",
                )
            finally:
                await client.close()

        resp = asyncio.run(run())
        msg = resp.choices[0].message
        assert resp.choices[0].finish_reason == "tool_calls"
        assert msg.tool_calls is not None
        tool_call = msg.tool_calls[0]
        assert tool_call.id == "call_2"
        assert tool_call.type == "function"
        assert tool_call.function.name == "get_weather"
        assert tool_call.function.arguments == '{"city": "Lyon"}'
