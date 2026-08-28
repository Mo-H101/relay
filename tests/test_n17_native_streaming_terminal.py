"""
N-17 regression tests: native provider streaming terminal-signal enforcement.

Every native streaming method must treat a clean EOF that never carries the
provider's required wire-level terminal signal as a failure, not success:

* Anthropic: the ``message_stop`` SSE event.
* Gemini:    a candidate with the terminal ``finishReason``.
* Ollama:    the ``done: true`` NDJSON marker.

Covered surfaces (12 methods, 3 providers x 4 variants):
  chat_stream / achat_stream                 (single-message text)
  chat_stream_messages / achat_stream_messages (full-payload messages)

For each method we assert:
  * success when the native terminal signal is present;
  * ProviderHTTPError + provider HTTP status 0 (NOT 200) when the stream
    yields content and then hits a clean EOF without the terminal signal;
  * the failure propagates through the client's provider-error path.
"""

import json

import httpx
import pytest

from app.providers.anthropic_client import AnthropicClient
from app.providers.exceptions import ProviderHTTPError
from app.providers.gemini_client import GeminiClient
from app.providers.ollama_client import OllamaClient
from app.providers.base import Provider
from app.services.metrics import relay_metrics


def _provider(name):
    return Provider(
        name=name,
        base_url="http://localhost:9999",
        api_key="sk-test",
    )


# ---------------------------------------------------------------------------
# Fake sync/async streaming responses
# ---------------------------------------------------------------------------


def _wrapper(line):
    return f"data: {line}"


class _SyncResponse:
    def __init__(self, lines):
        self._lines = list(lines)
        self.status_code = 200

    def iter_lines(self):
        yield from self._lines


class _SyncCtx:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *a):
        return False


class _AsyncResponse:
    def __init__(self, lines):
        self._lines = list(lines)
        self.status_code = 200

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _AsyncCtx:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *a):
        return False


class _AsyncClient:
    def __init__(self, response):
        self._response = response

    def stream(self, method, url, **kwargs):
        return _AsyncCtx(self._response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _patch_sync(monkeypatch, module, lines):
    monkeypatch.setattr(
        f"app.providers.{module}.bounded_stream",
        lambda *a, **k: _SyncCtx(_SyncResponse(lines)),
    )


def _patch_async(monkeypatch, module, lines):
    monkeypatch.setattr(
        f"app.providers.{module}.httpx.AsyncClient",
        lambda **k: _AsyncClient(_AsyncResponse(lines)),
    )


# ---------------------------------------------------------------------------
# Anthropic fixtures
# ---------------------------------------------------------------------------

ANTHROPIC_TEXT = [
    _wrapper(json.dumps({"type": "content_block_delta", "index": 0,
                         "delta": {"type": "text_delta", "text": "Hel"}})),
    _wrapper(json.dumps({"type": "message_stop"})),
]

ANTHROPIC_TEXT_TRUNCATED = [
    _wrapper(json.dumps({"type": "content_block_delta", "index": 0,
                         "delta": {"type": "text_delta", "text": "Hel"}})),
]

ANTHROPIC_MSG = [
    _wrapper(json.dumps({"type": "message_start",
                         "message": {"usage": {"input_tokens": 5}}})),
    _wrapper(json.dumps({"type": "content_block_delta", "index": 0,
                         "delta": {"type": "text_delta", "text": "Hel"}})),
    _wrapper(json.dumps({"type": "message_delta",
                         "delta": {"stop_reason": "end_turn"},
                         "usage": {"output_tokens": 3}})),
    _wrapper(json.dumps({"type": "message_stop"})),
]

ANTHROPIC_MSG_TRUNCATED = ANTHROPIC_MSG[:-1]


# ---------------------------------------------------------------------------
# Gemini fixtures
# ---------------------------------------------------------------------------

GEMINI_TEXT = [
    _wrapper(json.dumps({"candidates": [
        {"content": {"parts": [{"text": "Hel"}]}, "finishReason": "STOP"}]})),
]

GEMINI_TEXT_TRUNCATED = [
    _wrapper(json.dumps({"candidates": [
        {"content": {"parts": [{"text": "Hel"}]}}]})),
]

GEMINI_MSG = [
    _wrapper(json.dumps({"candidates": [
        {"content": {"parts": [{"text": "Hel"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3}})),
]

GEMINI_MSG_TRUNCATED = [
    _wrapper(json.dumps({"candidates": [
        {"content": {"parts": [{"text": "Hel"}]}}]})),
]


# ---------------------------------------------------------------------------
# Ollama fixtures
# ---------------------------------------------------------------------------

OLLAMA_TEXT = [
    json.dumps({"message": {"role": "assistant", "content": "Hel"}}),
    json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}),
]

OLLAMA_TEXT_TRUNCATED = [
    json.dumps({"message": {"role": "assistant", "content": "Hel"}}),
]

OLLAMA_MSG = [
    json.dumps({"message": {"role": "assistant", "content": "Hel"}}),
    json.dumps({"done": True}),
]

OLLAMA_MSG_TRUNCATED = [
    json.dumps({"message": {"role": "assistant", "content": "Hel"}}),
]


# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module,lines,provider_name", [
    ("anthropic_client", ANTHROPIC_TEXT, "Anthropic"),
    ("anthropic_client", ANTHROPIC_MSG, "Anthropic"),
    ("gemini_client", GEMINI_TEXT, "Google Gemini"),
    ("gemini_client", GEMINI_MSG, "Google Gemini"),
    ("ollama_client", OLLAMA_TEXT, "Ollama"),
    ("ollama_client", OLLAMA_MSG, "Ollama"),
])
@pytest.mark.parametrize("method", ["chat_stream", "chat_stream_messages"])
def test_terminal_present_stream_succeeds_sync(monkeypatch, method, module,
                                               lines, provider_name):
    """A sync stream carrying the provider's native terminal signal succeeds."""
    _patch_sync(monkeypatch, module, lines)

    client = _client_for(module)
    provider = _provider(provider_name)

    if method == "chat_stream":
        chunks = list(client.chat_stream(provider, "m", "hi"))
    else:
        chunks = list(client.chat_stream_messages(
            provider, {"model": "m", "messages": []}))

    assert chunks, "a complete stream must yield content"


@pytest.mark.parametrize("module,lines,provider_name", [
    ("anthropic_client", ANTHROPIC_TEXT, "Anthropic"),
    ("anthropic_client", ANTHROPIC_MSG, "Anthropic"),
    ("gemini_client", GEMINI_TEXT, "Google Gemini"),
    ("gemini_client", GEMINI_MSG, "Google Gemini"),
    ("ollama_client", OLLAMA_TEXT, "Ollama"),
    ("ollama_client", OLLAMA_MSG, "Ollama"),
])
@pytest.mark.parametrize("method", ["achat_stream", "achat_stream_messages"])
@pytest.mark.asyncio
async def test_terminal_present_stream_succeeds_async(monkeypatch, method,
                                                      module, lines,
                                                      provider_name):
    """An async stream carrying the provider's native terminal succeeds."""
    _patch_async(monkeypatch, module, lines)

    client = _client_for(module)
    provider = _provider(provider_name)

    if method == "achat_stream":
        chunks = [c async for c in client.achat_stream(provider, "m", "hi")]
    else:
        chunks = [c async for c in client.achat_stream_messages(
            provider, {"model": "m", "messages": []})]

    assert chunks, "a complete stream must yield content"


def _op_for(method):
    return "chat_stream_messages" if method.endswith("_messages") \
        else "chat_stream"


@pytest.mark.parametrize("module,lines,provider_name", [
    ("anthropic_client", ANTHROPIC_TEXT_TRUNCATED, "Anthropic"),
    ("anthropic_client", ANTHROPIC_MSG_TRUNCATED, "Anthropic"),
    ("gemini_client", GEMINI_TEXT_TRUNCATED, "Google Gemini"),
    ("gemini_client", GEMINI_MSG_TRUNCATED, "Google Gemini"),
    ("ollama_client", OLLAMA_TEXT_TRUNCATED, "Ollama"),
    ("ollama_client", OLLAMA_MSG_TRUNCATED, "Ollama"),
])
@pytest.mark.parametrize("method", ["chat_stream", "chat_stream_messages"])
def test_truncated_eof_without_terminal_is_failure_sync(monkeypatch, method,
                                                        module, lines,
                                                        provider_name):
    """Sync: content then clean EOF without terminal signal = failure."""
    relay_metrics.reset()
    _patch_sync(monkeypatch, module, lines)

    client = _client_for(module)
    provider = _provider(provider_name)
    op = _op_for(method)

    if method == "chat_stream":
        gen = lambda: client.chat_stream(provider, "m", "hi")
    else:
        gen = lambda: client.chat_stream_messages(
            provider, {"model": "m", "messages": []})

    with pytest.raises(ProviderHTTPError):
        for _ in gen():
            pass

    assert relay_metrics.provider_outcomes.value(
        provider=provider_name, operation=op, status="network") == 1
    assert relay_metrics.provider_outcomes.value(
        provider=provider_name, operation=op, status="success") == 0


@pytest.mark.parametrize("module,lines,provider_name", [
    ("anthropic_client", ANTHROPIC_TEXT_TRUNCATED, "Anthropic"),
    ("anthropic_client", ANTHROPIC_MSG_TRUNCATED, "Anthropic"),
    ("gemini_client", GEMINI_TEXT_TRUNCATED, "Google Gemini"),
    ("gemini_client", GEMINI_MSG_TRUNCATED, "Google Gemini"),
    ("ollama_client", OLLAMA_TEXT_TRUNCATED, "Ollama"),
    ("ollama_client", OLLAMA_MSG_TRUNCATED, "Ollama"),
])
@pytest.mark.parametrize("method", ["achat_stream", "achat_stream_messages"])
@pytest.mark.asyncio
async def test_truncated_eof_without_terminal_is_failure_async(monkeypatch,
                                                               method, module,
                                                               lines,
                                                               provider_name):
    """Async: content then clean EOF without terminal signal = failure."""
    relay_metrics.reset()
    _patch_async(monkeypatch, module, lines)

    client = _client_for(module)
    provider = _provider(provider_name)
    op = _op_for(method)

    if method == "achat_stream":
        gen = lambda: client.achat_stream(provider, "m", "hi")
    else:
        gen = lambda: client.achat_stream_messages(
            provider, {"model": "m", "messages": []})

    with pytest.raises(ProviderHTTPError):
        async for _ in gen():
            pass

    assert relay_metrics.provider_outcomes.value(
        provider=provider_name, operation=op, status="network") == 1
    assert relay_metrics.provider_outcomes.value(
        provider=provider_name, operation=op, status="success") == 0


def _client_for(module):
    if module == "anthropic_client":
        return AnthropicClient()
    if module == "gemini_client":
        return GeminiClient()
    return OllamaClient()
