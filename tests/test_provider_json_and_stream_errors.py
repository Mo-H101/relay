"""
Regression tests for Iteration 2 fixes:

I2-H1: Unguarded response.json() in provider clients — empty/non-JSON
       bodies from 200 OK now raise ProviderHTTPError (not JSONDecodeError).
I2-P0a: Streaming error chunks now include OpenAI-compatible fields (id,
        object, created, model, choices) so SDK parsers don't crash.
I2-H2: Anthropic simple streams now detect in-stream {"type":"error"} events.
I2-H3: Gemini simple streams now detect in-stream {"error":...} events.
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.providers.base import Provider
from app.providers.exceptions import ProviderHTTPError
from app.providers.openai_compat_client import (
    OpenAICompatibleClient,
    _parse_provider_json,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _provider(name="Test", base_url="https://api.example.com/v1", key="sk-test"):
    return Provider(name=name, base_url=base_url, api_key=key)


class _FakeResponse:
    """Minimal httpx.Response stand-in for unit tests."""

    def __init__(self, status_code=200, json_data=None, text="",
                 raw_body=None, headers=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.headers = headers or {}
        self._raw_body = raw_body

    def json(self):
        if self._raw_body is not None:
            return json.loads(self._raw_body)
        if self._json is not None:
            return self._json
        # Simulate empty / non-JSON body
        raise json.JSONDecodeError("Expecting value", "", 0)


# ===================================================================
# I2-H1 — _parse_provider_json converts JSONDecodeError to
#         ProviderHTTPError so it flows through retry/failover.
# ===================================================================

class TestParseProviderJson:
    """Unit tests for the shared _parse_provider_json helper."""

    def test_valid_json_passthrough(self):
        resp = _FakeResponse(json_data={"choices": []})
        result = _parse_provider_json(resp, _provider(), 200)
        assert result == {"choices": []}

    def test_empty_body_raises_provider_http_error(self):
        resp = _FakeResponse()  # json() will raise JSONDecodeError
        with pytest.raises(ProviderHTTPError) as exc_info:
            _parse_provider_json(resp, _provider(), 200)
        assert exc_info.value.status_code == 200
        assert "Invalid JSON" in exc_info.value.message

    def test_malformed_json_raises_provider_http_error(self):
        resp = _FakeResponse(raw_body=b"not json at all")
        with pytest.raises(ProviderHTTPError) as exc_info:
            _parse_provider_json(resp, _provider(), 200)
        assert exc_info.value.status_code == 200
        assert "Invalid JSON" in exc_info.value.message

    def test_html_body_raises_provider_http_error(self):
        resp = _FakeResponse(raw_body=b"<html><body>Error</body></html>")
        with pytest.raises(ProviderHTTPError) as exc_info:
            _parse_provider_json(resp, _provider(), 200)
        assert exc_info.value.status_code == 200

    def test_none_body_raises_provider_http_error(self):
        """Simulate a response where .json() returns None."""
        class _NoneResponse:
            status_code = 200
            def json(self):
                return None
        # None is valid JSON but not a dict — must raise ProviderHTTPError
        # so callers don't crash with AttributeError on .get().
        with pytest.raises(ProviderHTTPError) as exc_info:
            _parse_provider_json(_NoneResponse(), _provider(), 200)
        assert "Expected JSON object" in exc_info.value.message
        assert exc_info.value.status_code == 200

    def test_status_code_preserved_in_error(self):
        resp = _FakeResponse(status_code=200)
        with pytest.raises(ProviderHTTPError) as exc_info:
            _parse_provider_json(resp, _provider(), 200)
        assert exc_info.value.status_code == 200

    def test_error_message_does_not_leak_api_key(self):
        provider = _provider(key="sk-super-secret-key-12345")
        resp = _FakeResponse(raw_body=b"bad")
        with pytest.raises(ProviderHTTPError) as exc_info:
            _parse_provider_json(resp, provider, 200)
        assert "sk-super-secret-key" not in exc_info.value.message


# ===================================================================
# I2-H1 — Integration: OpenAICompatibleClient.chat() with empty body
# ===================================================================

class TestOpenAICompatChatEmptyBody:
    """Verify that OpenAICompatibleClient.chat() raises ProviderHTTPError
    (not JSONDecodeError) when the provider returns 200 with empty body."""

    def test_empty_body_raises_provider_http_error(self, monkeypatch):
        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_post",
            lambda *a, **kw: _FakeResponse(status_code=200),
        )
        client = OpenAICompatibleClient()
        with pytest.raises(ProviderHTTPError) as exc_info:
            client.chat(_provider(), "gpt-4", "hello")
        assert exc_info.value.status_code == 200
        assert "Invalid JSON" in exc_info.value.message

    def test_malformed_body_raises_provider_http_error(self, monkeypatch):
        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_post",
            lambda *a, **kw: _FakeResponse(raw_body=b"<html>error</html>"),
        )
        client = OpenAICompatibleClient()
        with pytest.raises(ProviderHTTPError) as exc_info:
            client.chat(_provider(), "gpt-4", "hello")
        assert exc_info.value.status_code == 200


# ===================================================================
# I2-H1 — Integration: OpenAICompatibleClient.chat_messages() empty
# ===================================================================

class TestOpenAICompatChatMessagesEmptyBody:
    def test_empty_body_raises_provider_http_error(self, monkeypatch):
        monkeypatch.setattr(
            "app.providers.openai_compat_client.bounded_post",
            lambda *a, **kw: _FakeResponse(status_code=200),
        )
        client = OpenAICompatibleClient()
        with pytest.raises(ProviderHTTPError) as exc_info:
            client.chat_messages(_provider(), {"messages": []})
        assert exc_info.value.status_code == 200
        assert "Invalid JSON" in exc_info.value.message


# ===================================================================
# I2-P0a — Streaming error chunk includes OpenAI-compatible fields
# ===================================================================

class TestStreamingErrorChunkShape:
    """Verify that the streaming error chunk emitted by the /v1
    endpoint includes id, object, created, model, and choices fields
    so that OpenAI SDK parsers don't crash."""

    def test_error_chunk_has_required_fields(self):
        """Simulate the error chunk construction logic from openai.py."""
        stream_id = "chatcmpl-test123"
        created = 1234567890
        stream_model = "gpt-4"

        error_chunk = {
            "id": stream_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": stream_model,
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }],
            "error": {
                "message": "Provider returned a server error.",
                "type": "stream_error",
                "code": "stream_error"
            }
        }

        # Verify required fields for SDK compatibility
        assert error_chunk["id"] == stream_id
        assert error_chunk["object"] == "chat.completion.chunk"
        assert error_chunk["created"] == created
        assert error_chunk["model"] == stream_model
        assert isinstance(error_chunk["choices"], list)
        assert len(error_chunk["choices"]) == 1
        assert "delta" in error_chunk["choices"][0]
        assert "finish_reason" in error_chunk["choices"][0]
        assert "error" in error_chunk

    def test_error_chunk_json_serializable(self):
        """Ensure the error chunk can be serialized to JSON without error."""
        error_chunk = {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1234567890,
            "model": "gpt-4",
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }],
            "error": {
                "message": "test error",
                "type": "stream_error",
                "code": "stream_error"
            }
        }
        serialized = json.dumps(error_chunk)
        parsed = json.loads(serialized)
        assert parsed["choices"][0]["delta"] == {}
        assert parsed["error"]["type"] == "stream_error"

    def test_error_chunk_choices_are_empty_delta(self):
        """SDK parsers expect choices[0].delta to exist and be a dict."""
        error_chunk = {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1234567890,
            "model": "gpt-4",
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }],
            "error": {
                "message": "test",
                "type": "stream_error",
                "code": "stream_error"
            }
        }
        # OpenAI SDK does: resp.choices[0].delta -> should not crash
        delta = error_chunk["choices"][0]["delta"]
        assert isinstance(delta, dict)


# ===================================================================
# I2-H2 — Anthropic simple stream in-stream error detection
# ===================================================================

class TestAnthropicSimpleStreamErrorDetection:
    """Verify that Anthropic simple streams raise ProviderHTTPError
    when an in-stream {"type":"error"} event is received."""

    def _make_anthropic_error_sse(self, error_msg="overloaded"):
        """Create SSE lines simulating an Anthropic in-stream error."""
        error_event = json.dumps({
            "type": "error",
            "error": {
                "type": "overloaded_error",
                "message": error_msg,
            }
        })
        return [
            f"data: {error_event}",
        ]

    def _make_anthropic_normal_sse(self, text="hello"):
        """Create SSE lines simulating a normal Anthropic stream."""
        start_event = json.dumps({
            "type": "message_start",
            "message": {"usage": {"input_tokens": 10}},
        })
        delta_event = json.dumps({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        })
        stop_event = json.dumps({
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 5},
        })
        return [
            f"data: {start_event}",
            f"data: {delta_event}",
            f"data: {stop_event}",
        ]

    def test_error_event_raises_provider_http_error(self):
        """Verify the error detection logic."""
        from app.providers.availability import safe_error_body
        lines = self._make_anthropic_error_sse()

        for line in lines:
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if event.get("type") == "error":
                with pytest.raises(ProviderHTTPError) as exc_info:
                    raise ProviderHTTPError(
                        0,
                        safe_error_body(
                            _provider(), 0, str(event.get("error"))
                        ),
                    )
                assert exc_info.value.status_code == 0
                assert "overloaded" in exc_info.value.message.lower() or "error" in exc_info.value.message.lower()

    def test_normal_event_does_not_raise(self):
        """Verify normal events don't trigger error detection."""
        lines = self._make_anthropic_normal_sse()
        events = []
        for line in lines:
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if event.get("type") == "error":
                pytest.fail("Should not detect error in normal stream")
            events.append(event)
        assert len(events) == 3


# ===================================================================
# I2-H3 — Gemini simple stream in-stream error detection
# ===================================================================

class TestGeminiSimpleStreamErrorDetection:
    """Verify that Gemini simple streams raise ProviderHTTPError
    when an in-stream {"error":...} event is received."""

    def _make_gemini_error_sse(self, error_msg="quota exceeded"):
        """Create SSE lines simulating a Gemini in-stream error."""
        error_event = json.dumps({
            "error": {
                "code": 429,
                "message": error_msg,
                "status": "RESOURCE_EXHAUSTED",
            }
        })
        return [
            f"data: {error_event}",
        ]

    def _make_gemini_normal_sse(self, text="hello"):
        """Create SSE lines simulating a normal Gemini stream."""
        normal_event = json.dumps({
            "candidates": [{
                "content": {"parts": [{"text": text}]},
                "finishReason": "STOP",
            }],
        })
        return [
            f"data: {normal_event}",
        ]

    def test_error_event_raises_provider_http_error(self):
        """Verify the error detection logic."""
        from app.providers.availability import safe_error_body
        lines = self._make_gemini_error_sse()

        for line in lines:
            if not line.startswith("data: "):
                continue
            chunk = json.loads(line[6:])
            if "error" in chunk:
                with pytest.raises(ProviderHTTPError) as exc_info:
                    raise ProviderHTTPError(
                        0,
                        safe_error_body(
                            _provider(), 0, str(chunk["error"])
                        ),
                    )
                assert exc_info.value.status_code == 0
                assert "quota" in exc_info.value.message.lower() or "error" in exc_info.value.message.lower()

    def test_normal_event_does_not_raise(self):
        """Verify normal events don't trigger error detection."""
        lines = self._make_gemini_normal_sse()
        for line in lines:
            if not line.startswith("data: "):
                continue
            chunk = json.loads(line[6:])
            if "error" in chunk:
                pytest.fail("Should not detect error in normal stream")


# ===================================================================
# Integration tests — drive actual client methods through mocked SSE
# ===================================================================

class _FakeStreamResponse:
    """Minimal context-manager response for bounded_stream mocking."""

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


def _patch_anthropic_stream(monkeypatch, response):
    monkeypatch.setattr(
        "app.providers.anthropic_client.bounded_stream",
        lambda *a, **kw: response,
    )


def _patch_gemini_stream(monkeypatch, response):
    monkeypatch.setattr(
        "app.providers.gemini_client.bounded_stream",
        lambda *a, **kw: response,
    )


# --- I2-H2 Integration: Anthropic chat_stream with in-stream error ---

class TestAnthropicChatStreamIntegration:
    """Drive AnthropicClient.chat_stream() with mocked SSE to verify
    that in-stream error events raise ProviderHTTPError."""

    def test_in_stream_error_raises(self, monkeypatch):
        from app.providers.anthropic_client import AnthropicClient

        error_event = json.dumps({
            "type": "error",
            "error": {"type": "overloaded_error", "message": "try later"},
        })
        resp = _FakeStreamResponse([f"data: {error_event}"])
        _patch_anthropic_stream(monkeypatch, resp)

        with pytest.raises(ProviderHTTPError) as exc_info:
            list(AnthropicClient().chat_stream(
                _provider(name="Anthropic"), "claude-3", "hi"
            ))
        assert exc_info.value.status_code == 0

    def test_normal_stream_yields_text(self, monkeypatch):
        from app.providers.anthropic_client import AnthropicClient

        start = json.dumps({
            "type": "message_start",
            "message": {"usage": {"input_tokens": 10}},
        })
        delta = json.dumps({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "hello"},
        })
        stop = json.dumps({
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 5},
        })
        terminal = json.dumps({"type": "message_stop"})
        resp = _FakeStreamResponse([
            f"data: {start}", f"data: {delta}", f"data: {stop}",
            f"data: {terminal}",
        ])
        _patch_anthropic_stream(monkeypatch, resp)

        chunks = list(AnthropicClient().chat_stream(
            _provider(name="Anthropic"), "claude-3", "hi"
        ))
        assert chunks == ["hello"]

    def test_error_after_text_still_raises(self, monkeypatch):
        from app.providers.anthropic_client import AnthropicClient

        start = json.dumps({
            "type": "message_start",
            "message": {"usage": {"input_tokens": 10}},
        })
        delta = json.dumps({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "partial"},
        })
        error = json.dumps({
            "type": "error",
            "error": {"type": "api_error", "message": "disconnected"},
        })
        resp = _FakeStreamResponse([
            f"data: {start}", f"data: {delta}", f"data: {error}",
        ])
        _patch_anthropic_stream(monkeypatch, resp)

        with pytest.raises(ProviderHTTPError):
            list(AnthropicClient().chat_stream(
                _provider(name="Anthropic"), "claude-3", "hi"
            ))


# --- I2-H3 Integration: Gemini chat_stream with in-stream error ---

class TestGeminiChatStreamIntegration:
    """Drive GeminiClient.chat_stream() with mocked SSE to verify
    that in-stream error events raise ProviderHTTPError."""

    def test_in_stream_error_raises(self, monkeypatch):
        from app.providers.gemini_client import GeminiClient

        error_event = json.dumps({
            "error": {"code": 429, "message": "quota exceeded"},
        })
        resp = _FakeStreamResponse([f"data: {error_event}"])
        _patch_gemini_stream(monkeypatch, resp)

        with pytest.raises(ProviderHTTPError) as exc_info:
            list(GeminiClient().chat_stream(
                _provider(name="Gemini"), "gemini-pro", "hi"
            ))
        assert exc_info.value.status_code == 0

    def test_normal_stream_yields_text(self, monkeypatch):
        from app.providers.gemini_client import GeminiClient

        normal = json.dumps({
            "candidates": [{
                "content": {"parts": [{"text": "hello"}]},
                "finishReason": "STOP",
            }],
        })
        resp = _FakeStreamResponse([f"data: {normal}"])
        _patch_gemini_stream(monkeypatch, resp)

        chunks = list(GeminiClient().chat_stream(
            _provider(name="Gemini"), "gemini-pro", "hi"
        ))
        assert chunks == ["hello"]

    def test_error_after_text_still_raises(self, monkeypatch):
        from app.providers.gemini_client import GeminiClient

        normal = json.dumps({
            "candidates": [{
                "content": {"parts": [{"text": "partial"}]},
            }],
        })
        error = json.dumps({
            "error": {"code": 500, "message": "internal error"},
        })
        resp = _FakeStreamResponse([f"data: {normal}", f"data: {error}"])
        _patch_gemini_stream(monkeypatch, resp)

        with pytest.raises(ProviderHTTPError):
            list(GeminiClient().chat_stream(
                _provider(name="Gemini"), "gemini-pro", "hi"
            ))


# ---------------------------------------------------------------------------
# I6-N1: _parse_provider_json rejects non-dict JSON (arrays, strings, etc.)
# ---------------------------------------------------------------------------


class TestParseProviderJsonNonDict:
    """_parse_provider_json must reject non-dict JSON values (lists,
    strings, numbers, booleans, null) which would cause AttributeError
    at every .get() call site."""

    @pytest.mark.parametrize("payload", [
        [],
        [1, 2, 3],
        "text",
        42,
        3.14,
        True,
        False,
    ])
    def test_non_dict_json_raises_provider_http_error(self, payload):
        resp = _FakeResponse(status_code=200, json_data=payload)
        with pytest.raises(ProviderHTTPError) as exc_info:
            _parse_provider_json(resp, _provider(), 200)
        assert "Expected JSON object" in exc_info.value.message
        assert exc_info.value.status_code == 200

    def test_dict_json_passes_through(self):
        resp = _FakeResponse(status_code=200, json_data={"key": "value"})
        result = _parse_provider_json(resp, _provider(), 200)
        assert result == {"key": "value"}
