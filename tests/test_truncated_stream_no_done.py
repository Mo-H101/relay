"""
Regression tests for E1: a provider stream that ends without the terminal
`[DONE]` marker (and without raising an HTTP-level exception) must NOT be
treated as a successful, complete response.

Previously the four OpenAI-compat streaming methods recorded status 200 and
returned partial content as if the response were complete whenever the
underlying connection ended cleanly before `[DONE]` (only genuine
`httpx.HTTPError` subclasses before `[DONE]` were treated as failures). The
API layer would then relay the truncated content as a finished `[DONE]`
stream and the continuation layer would record the turn as successful with
incomplete content.

Providers routed through these methods (OpenAI, NVIDIA NIM, LM Studio, and any
other OpenAI-compatible endpoint) are required to emit `[DONE]`, so a clean
EOF before `[DONE]` is a genuine truncation and must be surfaced as a failure.
"""

import json
import asyncio
from unittest.mock import MagicMock

import pytest

from app.providers.openai_compat_client import OpenAICompatibleClient
from app.providers.exceptions import ProviderHTTPError
from app.services.metrics import relay_metrics


def _make_provider(name="Test"):
    p = MagicMock()
    p.name = name
    p.identity.return_value = name
    p.base_url = "http://fake:1234/v1"
    p.models = ["m1"]
    p.proxy = None
    p.has_api_key.return_value = False
    p.api_key = None
    return p


def _chunk(content):
    return f"data: {json.dumps({'choices': [{'delta': {'content': content}}]})}"


# ---------------------------------------------------------------------------
# Sync fakes (chat_stream / chat_stream_messages)
# ---------------------------------------------------------------------------


class _FakeSyncResponse:
    """Clean-EOF streaming response that never emits [DONE]."""

    def __init__(self, lines):
        self._lines = list(lines)
        self.status_code = 200
        self.headers = {}

    def iter_lines(self):
        return iter(self._lines)


class _FakeStreamCtx:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *a):
        return False


def _patch_sync(monkeypatch, lines):
    def handler(*args, **kwargs):
        return _FakeStreamCtx(_FakeSyncResponse(lines))
    monkeypatch.setattr(
        "app.providers.openai_compat_client.bounded_stream", handler
    )


# ---------------------------------------------------------------------------
# Async fakes (achat_stream / achat_stream_messages)
# ---------------------------------------------------------------------------


class _FakeAsyncResponse:
    def __init__(self, lines):
        self._lines = list(lines)
        self.status_code = 200

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeAsyncStreamCtx:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *a):
        return False


class _FakeAsyncClient:
    def __init__(self, response):
        self._response = response

    def stream(self, method, url, **kwargs):
        return _FakeAsyncStreamCtx(self._response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _patch_async(monkeypatch, lines):
    monkeypatch.setattr(
        "app.providers.openai_compat_client.httpx.AsyncClient",
        lambda **kw: _FakeAsyncClient(_FakeAsyncResponse(lines)),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_chat_stream_truncated_no_done_is_failure(monkeypatch):
    relay_metrics.reset()
    _patch_sync(
        monkeypatch, [_chunk("partial")]
    )  # clean EOF, no [DONE]

    with pytest.raises(ProviderHTTPError) as ei:
        list(
            OpenAICompatibleClient().chat_stream(
                _make_provider(), model="m1", message="hi"
            )
        )
    assert "truncated provider stream" in ei.value.message
    assert (
        relay_metrics.provider_outcomes.value(
            provider="Test", operation="chat_stream", status="network"
        )
        == 1
    )
    assert (
        relay_metrics.provider_outcomes.value(
            provider="Test", operation="chat_stream", status="ok"
        )
        == 0
    )


def test_chat_stream_messages_truncated_no_done_is_failure(monkeypatch):
    relay_metrics.reset()
    _patch_sync(monkeypatch, [_chunk("partial")])

    with pytest.raises(ProviderHTTPError) as ei:
        list(
            OpenAICompatibleClient().chat_stream_messages(
                _make_provider(),
                payload={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
            )
        )
    assert "truncated provider stream" in ei.value.message
    assert (
        relay_metrics.provider_outcomes.value(
            provider="Test", operation="chat_stream_messages", status="network"
        )
        == 1
    )
    assert (
        relay_metrics.provider_outcomes.value(
            provider="Test", operation="chat_stream_messages", status="ok"
        )
        == 0
    )


@pytest.mark.asyncio
async def test_achat_stream_truncated_no_done_is_failure(monkeypatch):
    relay_metrics.reset()
    _patch_async(monkeypatch, [_chunk("partial")])

    with pytest.raises(ProviderHTTPError) as ei:
        async for _ in OpenAICompatibleClient().achat_stream(
            _make_provider(), "m1", "hi"
        ):
            pass
    assert "truncated provider stream" in ei.value.message
    assert (
        relay_metrics.provider_outcomes.value(
            provider="Test", operation="achat_stream", status="network"
        )
        == 1
    )
    assert (
        relay_metrics.provider_outcomes.value(
            provider="Test", operation="achat_stream", status="ok"
        )
        == 0
    )


@pytest.mark.asyncio
async def test_achat_stream_messages_truncated_no_done_is_failure(monkeypatch):
    relay_metrics.reset()
    _patch_async(monkeypatch, [_chunk("partial")])

    with pytest.raises(ProviderHTTPError) as ei:
        async for _ in OpenAICompatibleClient().achat_stream_messages(
            _make_provider(),
            payload={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
        ):
            pass
    assert "truncated provider stream" in ei.value.message
    assert (
        relay_metrics.provider_outcomes.value(
            provider="Test", operation="achat_stream_messages", status="network"
        )
        == 1
    )
    assert (
        relay_metrics.provider_outcomes.value(
            provider="Test", operation="achat_stream_messages", status="ok"
        )
        == 0
    )
