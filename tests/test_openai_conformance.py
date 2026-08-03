"""
OpenAI-wire conformance tests for Relay's /v1 API (P0).

These tests define the compatibility contract Relay must satisfy to act
as a drop-in OpenAI gateway for clients like Cline and OpenCode:

- Full message-structure passthrough (system/user/assistant/tool,
  multi-turn, tool_call_id, multimodal content parts). No flattening.
- First-class tool calling: tools input, tool_choice, assistant
  tool_calls output, tool role messages, streaming tool_call deltas,
  finish_reason="tool_calls".
- OpenAI-shaped errors ({"error": ...}), stable streaming ids,
  delta.role, and usage passthrough (non-stream and stream).
- The legacy /chat endpoint stays a plain string interface.

The mock provider runs in-process on loopback and records the exact
request payloads Relay sends, so these tests verify the real provider
client serialization and the real /v1 mapping end to end.
"""
import pytest

from fastapi.testclient import TestClient

from app.core.relay import Relay
from app.main import app as fastapi_app

from tests.conformance_helpers import (
    DEFAULT_MODEL,
    MockOpenAIProvider,
    make_provider,
    parse_sse,
)

import app.api.chat
import app.api.openai


def _completion_ok(content: str = "ok") -> dict:
    return {
        "id": "chatcmpl-mock-1",
        "object": "chat.completion",
        "created": 1700000000,
        "model": DEFAULT_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        },
    }


def _tool_completion() -> dict:
    return {
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


def _weather_tools() -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the weather for a city.",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            },
        }
    ]


@pytest.fixture
def mock_provider_server():
    mock = MockOpenAIProvider()
    mock.start()
    yield mock
    mock.stop()


@pytest.fixture
def relay_with_mock(monkeypatch):
    relays = []

    def _build(mock, provider_name="OpenAI"):
        provider = make_provider(
            name=provider_name, base_url=mock.base_url
        )
        relay_obj = Relay()
        relay_obj.provider_manager.register(provider)
        monkeypatch.setattr(app.api.openai, "relay", relay_obj)
        monkeypatch.setattr(app.api.chat, "relay", relay_obj)
        relays.append(relay_obj)
        return relay_obj

    yield _build


@pytest.fixture
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


class TestMessagePassthrough:
    def test_full_message_array_forwarded_verbatim(
        self, relay_with_mock, mock_provider_server, client
    ):
        """Multi-turn structure is preserved: no flattening to one string."""
        mock = mock_provider_server
        mock.script(json_body=_completion_ok())
        relay_with_mock(mock)

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": "how are you?"},
        ]

        response = client.post(
            "/v1/chat/completions",
            json={"model": DEFAULT_MODEL, "messages": messages},
        )

        assert response.status_code == 200
        assert len(mock.requests) == 1
        sent = mock.requests[0]["body"]["messages"]
        assert sent == messages

    def test_tool_role_and_tool_call_id_forwarded(
        self, relay_with_mock, mock_provider_server, client
    ):
        """A tool-calling conversation reaches the provider intact."""
        mock = mock_provider_server
        mock.script(json_body=_completion_ok())
        relay_with_mock(mock)

        messages = [
            {"role": "user", "content": "What is 6*7?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "multiply",
                            "arguments": '{"a": 6, "b": 7}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_abc", "content": "42"},
        ]

        response = client.post(
            "/v1/chat/completions",
            json={"model": DEFAULT_MODEL, "messages": messages},
        )

        assert response.status_code == 200
        sent = mock.requests[0]["body"]["messages"]
        assert sent == messages

    def test_multimodal_content_parts_forwarded(
        self, relay_with_mock, mock_provider_server, client
    ):
        """Content arrays (text + image_url parts) pass through."""
        mock = mock_provider_server
        mock.script(json_body=_completion_ok())
        relay_with_mock(mock)

        content = [
            {"type": "text", "text": "What is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": content}],
            },
        )

        assert response.status_code == 200
        sent_content = mock.requests[0]["body"]["messages"][0]["content"]
        assert sent_content == content


class TestToolCalling:
    def test_tools_and_tool_choice_forwarded(
        self, relay_with_mock, mock_provider_server, client
    ):
        mock = mock_provider_server
        mock.script(json_body=_completion_ok())
        relay_with_mock(mock)

        tools = _weather_tools()

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": tools,
                "tool_choice": "auto",
            },
        )

        assert response.status_code == 200
        sent = mock.requests[0]["body"]
        assert sent["tools"] == tools
        assert sent["tool_choice"] == "auto"

    def test_tool_calls_response_mapped(
        self, relay_with_mock, mock_provider_server, client
    ):
        mock = mock_provider_server
        mock.script(json_body=_tool_completion())
        relay_with_mock(mock)

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "weather in Paris?"}],
                "tools": _weather_tools(),
                "tool_choice": "auto",
            },
        )

        assert response.status_code == 200
        choice = response.json()["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        msg = choice["message"]
        assert msg["role"] == "assistant"
        tool_call = msg["tool_calls"][0]
        assert tool_call["id"] == "call_2"
        assert tool_call["type"] == "function"
        assert tool_call["function"]["name"] == "get_weather"
        assert tool_call["function"]["arguments"] == '{"city": "Lyon"}'

    def test_tool_choice_without_tools_returns_openai_400(
        self, relay_with_mock, mock_provider_server, client
    ):
        mock = mock_provider_server
        relay_with_mock(mock)

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": "auto",
            },
        )

        assert response.status_code == 400
        payload = response.json()
        assert "error" in payload
        assert "message" in payload["error"]
        assert "detail" not in payload


class TestStreamingConformance:
    def _content_stream(self):
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
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ],
            },
        ]

    def test_stream_content_deltas_stable_id_and_role(
        self, relay_with_mock, mock_provider_server, client
    ):
        mock = mock_provider_server
        mock.script(stream=self._content_stream())
        relay_with_mock(mock)

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        events = parse_sse(response.text)
        assert events[-1] == "[DONE]"
        chunks = events[:-1]
        assert len(chunks) == 3
        ids = {c["id"] for c in chunks}
        assert len(ids) == 1, "stream id must be stable across chunks"
        assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
        assert chunks[0]["choices"][0]["delta"]["content"] == "Hel"
        assert chunks[1]["choices"][0]["delta"]["content"] == "lo"
        assert chunks[2]["choices"][0]["finish_reason"] == "stop"

    def test_stream_tool_call_deltas(
        self, relay_with_mock, mock_provider_server, client
    ):
        mock = mock_provider_server
        mock.script(
            stream=[
                {
                    "id": "chatcmpl-mock-1",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": DEFAULT_MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "get_weather",
                                            "arguments": "",
                                        },
                                    }
                                ],
                            },
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
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"arguments": '{"city": '}}
                                ]
                            },
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
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"arguments": 'Paris"}'}}
                                ]
                            },
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
                        {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
                    ],
                },
            ]
        )
        relay_with_mock(mock)

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "weather in Paris?"}],
                "tools": _weather_tools(),
                "tool_choice": "auto",
                "stream": True,
            },
        )

        assert response.status_code == 200
        events = parse_sse(response.text)
        assert events[-1] == "[DONE]"
        tool_chunks = [
            c
            for c in events[:-1]
            if c["choices"][0]["delta"].get("tool_calls")
        ]
        assert tool_chunks, "no streaming tool_call deltas emitted"
        first = tool_chunks[0]["choices"][0]["delta"]["tool_calls"][0]
        assert first["id"] == "call_1"
        assert first["function"]["name"] == "get_weather"
        final = events[-2]["choices"][0]
        assert final["finish_reason"] == "tool_calls"

    def test_stream_usage_emitted_when_requested(
        self, relay_with_mock, mock_provider_server, client
    ):
        mock = mock_provider_server
        mock.script(
            stream=self._content_stream()
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

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )

        assert response.status_code == 200
        events = parse_sse(response.text)
        usage_chunks = [
            c for c in events[:-1] if c.get("usage") is not None
        ]
        assert len(usage_chunks) == 1
        assert usage_chunks[0]["usage"] == {
            "prompt_tokens": 2,
            "completion_tokens": 1,
            "total_tokens": 3,
        }

    def test_stream_no_usage_without_stream_options(
        self, relay_with_mock, mock_provider_server, client
    ):
        mock = mock_provider_server
        mock.script(stream=self._content_stream())
        relay_with_mock(mock)

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        events = parse_sse(response.text)
        usage_chunks = [c for c in events[:-1] if "usage" in c]
        assert usage_chunks == []


class TestErrorShape:
    def test_unknown_model_error_uses_openai_shape(
        self, relay_with_mock, mock_provider_server, client
    ):
        mock = mock_provider_server
        relay_with_mock(mock)

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "no-such-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 400
        payload = response.json()
        assert "error" in payload
        assert "message" in payload["error"]
        assert "no-such-model" in payload["error"]["message"]
        assert "detail" not in payload

    def test_provider_error_openai_shape_and_redaction(
        self, relay_with_mock, mock_provider_server, client
    ):
        mock = mock_provider_server
        mock.script(
            error=500,
            body={
                "error": {
                    "message": "upstream exploded sk-test-key",
                    "type": "server_error",
                    "code": "server_error",
                }
            },
        )
        relay_with_mock(mock)

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 502
        payload = response.json()
        assert "error" in payload
        assert "detail" not in payload
        assert "sk-test-key" not in response.text


class TestResponseConformance:
    def test_usage_passthrough_non_stream(
        self, relay_with_mock, mock_provider_server, client
    ):
        mock = mock_provider_server
        mock.script(json_body=_completion_ok())
        relay_with_mock(mock)

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["object"] == "chat.completion"
        assert payload["choices"][0]["message"]["content"] == "ok"
        assert payload["usage"] == {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        }


class TestMessagePathFailover:
    def test_message_path_failover_preserves_messages(
        self, client, monkeypatch
    ):
        """Failover across providers keeps the exact message array on both."""
        failing = MockOpenAIProvider()
        failing.script(error=500).script(error=500)
        ok = MockOpenAIProvider()
        ok.script(json_body=_completion_ok())
        failing.start()
        ok.start()

        try:
            relay_obj = Relay()
            relay_obj.provider_manager.register(
                make_provider(name="OpenAI", base_url=failing.base_url)
            )
            relay_obj.provider_manager.register(
                make_provider(name="NVIDIA", base_url=ok.base_url)
            )
            monkeypatch.setattr(app.api.openai, "relay", relay_obj)
            monkeypatch.setattr(app.api.chat, "relay", relay_obj)

            messages = [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
                {"role": "user", "content": "how are you?"},
            ]

            response = client.post(
                "/v1/chat/completions",
                json={"model": DEFAULT_MODEL, "messages": messages},
            )

            assert response.status_code == 200
            assert len(failing.requests) == 2
            assert len(ok.requests) == 1
            for rec in failing.requests + ok.requests:
                assert rec["body"]["messages"] == messages
        finally:
            failing.stop()
            ok.stop()


class TestNativeChatBoundary:
    def test_native_chat_still_string_interface(
        self, relay_with_mock, mock_provider_server, client
    ):
        """The legacy /chat endpoint keeps its single-string message path."""
        mock = mock_provider_server
        mock.script(json_body=_completion_ok())
        relay_with_mock(mock)

        response = client.post("/chat", json={"message": "hello"})

        assert response.status_code == 200
        assert len(mock.requests) == 1
        sent = mock.requests[0]["body"]["messages"]
        assert sent == [{"role": "user", "content": "hello"}]
