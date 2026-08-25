"""Regression tests for F-C3: spurious stream_error after full delivery.

Verifies that when [DONE] is received and content is fully delivered,
an httpx.HTTPError during response cleanup (aclose()) is suppressed
rather than propagated as a ProviderHTTPError / stream_error.

Test matrix:
  A – achat_stream_messages: clean stream → yields all chunks
  B – achat_stream_messages: [DONE] + aclose() error → suppressed (F-C3)
  C – achat_stream_messages: httpx error BEFORE [DONE] → ProviderHTTPError
  D – achat_stream_messages: aclose() error BEFORE [DONE] → raises
  E – chat_stream_messages: [DONE] + close() error → suppressed
  F – achat_stream: clean stream → yields all content strings
  G – achat_stream: [DONE] + aclose() error → suppressed
  H – chat_stream: clean stream → yields all content strings
  I – chat_stream: [DONE] + close() error → suppressed
  J – chat_stream_messages: httpx error BEFORE [DONE] → ProviderHTTPError
"""

import json
import pytest
import httpx
from unittest.mock import patch

from app.providers.openai_compat_client import OpenAICompatibleClient
from app.providers.exceptions import ProviderHTTPError, ProviderTimeout


def _make_provider(name="test"):
    from unittest.mock import MagicMock
    p = MagicMock()
    p.name = name
    p.identity.return_value = name
    p.base_url = "http://fake:1234"
    p.models = ["m1"]
    p.proxy = None
    p.has_api_key.return_value = False
    p.api_key = None
    return p


def _sse_data_lines(*contents):
    """Build SSE data lines from content strings (for messages-based methods)."""
    lines = []
    for c in contents:
        lines.append(f"data: {json.dumps({'choices': [{'delta': {'content': c}}]})}")
    lines.append("data: [DONE]")
    return lines


def _sse_text_lines(*contents):
    """Build SSE lines that yield raw text (for chat_stream / achat_stream)."""
    lines = []
    for c in contents:
        lines.append(f"data: {json.dumps({'choices': [{'delta': {'content': c}}]})}")
    lines.append("data: [DONE]")
    return lines


# ---------------------------------------------------------------------------
# Async fake objects for achat_stream* methods
# ---------------------------------------------------------------------------

class _FakeAsyncResponse:
    """Mimics httpx.Response for async streaming tests."""

    def __init__(self, lines, raise_on_close=None):
        self._lines = list(lines)
        self._idx = 0
        self.status_code = 200
        self._raise_on_close = raise_on_close

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._idx >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._idx]
        self._idx += 1
        return line

    class _AiterLines:
        def __init__(self, lines):
            self._lines = list(lines)
            self._idx = 0
        def __aiter__(self):
            return self
        async def __anext__(self):
            if self._idx >= len(self._lines):
                raise StopAsyncIteration
            line = self._lines[self._idx]
            self._idx += 1
            return line

    def aiter_lines(self):
        return self._AiterLines(self._lines)

    async def aclose(self):
        if self._raise_on_close:
            raise self._raise_on_close

    async def aread(self):
        return b""


class _FakeAsyncStreamCtx:
    """Async context manager returning a fake response; aclose() on exit."""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        await self._response.aclose()
        return False


class _FakeAsyncHTTPXClient:
    """Fake httpx.AsyncClient that returns a FakeStreamCtx from stream()."""

    def __init__(self, response):
        self._response = response

    def stream(self, method, url, **kwargs):
        return _FakeAsyncStreamCtx(self._response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# Sync fake objects for chat_stream* methods
# ---------------------------------------------------------------------------

class _FakeSyncResponse:
    """Mimics httpx.Response for sync streaming tests."""

    def __init__(self, lines, raise_on_close=None):
        self._lines = list(lines)
        self._idx = 0
        self.status_code = 200
        self._raise_on_close = raise_on_close

    def __iter__(self):
        return self

    def __next__(self):
        if self._idx >= len(self._lines):
            raise StopIteration
        line = self._lines[self._idx]
        self._idx += 1
        return line

    def iter_lines(self):
        return iter(self._lines)

    def close(self):
        if self._raise_on_close:
            raise self._raise_on_close

    def read(self):
        return b""


class _FakeSyncStreamCtx:
    """Sync context manager returning a fake response; close() on exit."""

    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *exc):
        self._response.close()
        return False


class _FakeSyncHTTPXClient:
    """Fake httpx.Client for bounded_stream."""

    def __init__(self, response):
        self._response = response

    def stream(self, method, url, **kwargs):
        return _FakeSyncStreamCtx(self._response)


# ===========================================================================
#  achat_stream_messages (async, payload-based)
# ===========================================================================

class TestAchatStreamMessagesFC3:
    """Tests for achat_stream_messages — the primary F-C3 fix target."""

    @pytest.mark.asyncio
    async def test_clean_stream_yields_all_chunks(self):
        client = OpenAICompatibleClient()
        provider = _make_provider()
        lines = _sse_data_lines("hello", " world")
        fake_response = _FakeAsyncResponse(lines)

        with patch(
            "app.providers.openai_compat_client.httpx.AsyncClient",
            return_value=_FakeAsyncHTTPXClient(fake_response),
        ):
            chunks = []
            async for chunk in client.achat_stream_messages(
                provider=provider,
                payload={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
            ):
                chunks.append(chunk)

        assert len(chunks) == 2
        assert chunks[0]["choices"][0]["delta"]["content"] == "hello"
        assert chunks[1]["choices"][0]["delta"]["content"] == " world"

    @pytest.mark.asyncio
    async def test_done_with_cleanup_error_suppressed(self):
        """F-C3 core: [DONE] received, aclose() raises httpx error → suppressed."""
        client = OpenAICompatibleClient()
        provider = _make_provider()
        lines = _sse_data_lines("delivered")
        fake_response = _FakeAsyncResponse(
            lines, raise_on_close=httpx.RemoteProtocolError("upstream closed"),
        )

        with patch(
            "app.providers.openai_compat_client.httpx.AsyncClient",
            return_value=_FakeAsyncHTTPXClient(fake_response),
        ):
            chunks = []
            async for chunk in client.achat_stream_messages(
                provider=provider,
                payload={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
            ):
                chunks.append(chunk)

        assert len(chunks) == 1
        assert chunks[0]["choices"][0]["delta"]["content"] == "delivered"

    @pytest.mark.asyncio
    async def test_http_error_before_done_still_raises(self):
        """Genuine failure: httpx error during iteration, before [DONE]."""
        class FailAfterFirstLine:
            def __init__(self):
                self.status_code = 200

            async def aiter_lines(self):
                yield _sse_data_lines("partial")[0]
                raise httpx.RemoteProtocolError("connection reset")

            async def aclose(self):
                pass

            async def aread(self):
                return b""

        class FailStreamCtx:
            async def __aenter__(self):
                return FailAfterFirstLine()
            async def __aexit__(self, *exc):
                return False

        class FailHTTPXClient:
            def stream(self, method, url, **kwargs):
                return FailStreamCtx()
            async def __aenter__(self):
                return self
            async def __aexit__(self, *exc):
                return False

        client = OpenAICompatibleClient()
        provider = _make_provider()

        with patch(
            "app.providers.openai_compat_client.httpx.AsyncClient",
            return_value=FailHTTPXClient(),
        ):
            with pytest.raises(ProviderHTTPError):
                async for _ in client.achat_stream_messages(
                    provider=provider,
                    payload={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
                ):
                    pass

    @pytest.mark.asyncio
    async def test_cleanup_error_before_done_still_raises(self):
        """aclose() error with [DONE] never seen → error must propagate."""
        client = OpenAICompatibleClient()
        provider = _make_provider()
        lines = [_sse_data_lines("partial")[0]]
        fake_response = _FakeAsyncResponse(
            lines, raise_on_close=httpx.RemoteProtocolError("connection lost"),
        )

        with patch(
            "app.providers.openai_compat_client.httpx.AsyncClient",
            return_value=_FakeAsyncHTTPXClient(fake_response),
        ):
            raised = False
            try:
                async for _ in client.achat_stream_messages(
                    provider=provider,
                    payload={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
                ):
                    pass
            except ProviderHTTPError:
                raised = True
        assert raised, "aclose() error before [DONE] must not be silently suppressed"


# ===========================================================================
#  achat_stream (async, message-based)
# ===========================================================================

class TestAchatStreamFC3:
    """Tests for achat_stream (async, yields content strings)."""

    @pytest.mark.asyncio
    async def test_clean_stream_yields_all_content(self):
        client = OpenAICompatibleClient()
        provider = _make_provider()
        lines = _sse_text_lines("hello", " world")
        fake_response = _FakeAsyncResponse(lines)

        with patch(
            "app.providers.openai_compat_client.httpx.AsyncClient",
            return_value=_FakeAsyncHTTPXClient(fake_response),
        ):
            chunks = []
            async for chunk in client.achat_stream(
                provider=provider,
                model="m1",
                message="hi",
            ):
                chunks.append(chunk)

        assert chunks == ["hello", " world"]

    @pytest.mark.asyncio
    async def test_done_with_cleanup_error_suppressed(self):
        """F-C3: [DONE] + aclose() error → suppressed."""
        client = OpenAICompatibleClient()
        provider = _make_provider()
        lines = _sse_text_lines("delivered")
        fake_response = _FakeAsyncResponse(
            lines, raise_on_close=httpx.RemoteProtocolError("upstream closed"),
        )

        with patch(
            "app.providers.openai_compat_client.httpx.AsyncClient",
            return_value=_FakeAsyncHTTPXClient(fake_response),
        ):
            chunks = []
            async for chunk in client.achat_stream(
                provider=provider,
                model="m1",
                message="hi",
            ):
                chunks.append(chunk)

        assert chunks == ["delivered"]

    @pytest.mark.asyncio
    async def test_http_error_before_done_still_raises(self):
        """httpx error during iteration before [DONE] → ProviderHTTPError."""
        class FailLines:
            def __init__(self):
                self.status_code = 200

            async def aiter_lines(self):
                yield _sse_text_lines("partial")[0]
                raise httpx.RemoteProtocolError("reset")

            async def aclose(self):
                pass

            async def aread(self):
                return b""

        class FailStreamCtx:
            async def __aenter__(self):
                return FailLines()
            async def __aexit__(self, *exc):
                return False

        class FailHTTPXClient:
            def stream(self, method, url, **kwargs):
                return FailStreamCtx()
            async def __aenter__(self):
                return self
            async def __aexit__(self, *exc):
                return False

        client = OpenAICompatibleClient()
        provider = _make_provider()

        with patch(
            "app.providers.openai_compat_client.httpx.AsyncClient",
            return_value=FailHTTPXClient(),
        ):
            with pytest.raises(ProviderHTTPError):
                async for _ in client.achat_stream(
                    provider=provider, model="m1", message="hi",
                ):
                    pass


# ===========================================================================
#  chat_stream (sync, message-based)
# ===========================================================================

class TestChatStreamFC3:
    """Tests for chat_stream (sync, yields content strings)."""

    def test_clean_stream_yields_all_content(self):
        client = OpenAICompatibleClient()
        provider = _make_provider()
        lines = _sse_text_lines("hello", " world")
        fake_response = _FakeSyncResponse(lines)

        with patch(
            "app.providers.openai_compat_client.bounded_stream",
            return_value=_FakeSyncStreamCtx(fake_response),
        ):
            chunks = list(client.chat_stream(
                provider=provider, model="m1", message="hi",
            ))

        assert chunks == ["hello", " world"]

    def test_done_with_cleanup_error_suppressed(self):
        """F-C3: [DONE] + close() error → suppressed."""
        client = OpenAICompatibleClient()
        provider = _make_provider()
        lines = _sse_text_lines("delivered")
        fake_response = _FakeSyncResponse(
            lines, raise_on_close=httpx.RemoteProtocolError("upstream closed"),
        )

        with patch(
            "app.providers.openai_compat_client.bounded_stream",
            return_value=_FakeSyncStreamCtx(fake_response),
        ):
            chunks = list(client.chat_stream(
                provider=provider, model="m1", message="hi",
            ))

        assert chunks == ["delivered"]

    def test_http_error_before_done_still_raises(self):
        """httpx error during iteration before [DONE] → ProviderHTTPError."""
        class FailLines:
            def __init__(self):
                self.status_code = 200

            def iter_lines(self):
                yield _sse_text_lines("partial")[0]
                raise httpx.RemoteProtocolError("reset")

            def close(self):
                pass

            def read(self):
                return b""

        class FailStreamCtx:
            def __enter__(self):
                return FailLines()
            def __exit__(self, *exc):
                return False

        client = OpenAICompatibleClient()
        provider = _make_provider()

        with patch(
            "app.providers.openai_compat_client.bounded_stream",
            return_value=FailStreamCtx(),
        ):
            with pytest.raises(ProviderHTTPError):
                for _ in client.chat_stream(
                    provider=provider, model="m1", message="hi",
                ):
                    pass


# ===========================================================================
#  chat_stream_messages (sync, payload-based)
# ===========================================================================

class TestChatStreamMessagesFC3:
    """Tests for chat_stream_messages (sync, yields chunk dicts)."""

    def test_clean_stream_yields_all_chunks(self):
        client = OpenAICompatibleClient()
        provider = _make_provider()
        lines = _sse_data_lines("hello", " world")
        fake_response = _FakeSyncResponse(lines)

        with patch(
            "app.providers.openai_compat_client.bounded_stream",
            return_value=_FakeSyncStreamCtx(fake_response),
        ):
            chunks = list(client.chat_stream_messages(
                provider=provider,
                payload={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
            ))

        assert len(chunks) == 2
        assert chunks[0]["choices"][0]["delta"]["content"] == "hello"
        assert chunks[1]["choices"][0]["delta"]["content"] == " world"

    def test_done_with_cleanup_error_suppressed(self):
        """F-C3: [DONE] + close() error → suppressed."""
        client = OpenAICompatibleClient()
        provider = _make_provider()
        lines = _sse_data_lines("delivered")
        fake_response = _FakeSyncResponse(
            lines, raise_on_close=httpx.RemoteProtocolError("upstream closed"),
        )

        with patch(
            "app.providers.openai_compat_client.bounded_stream",
            return_value=_FakeSyncStreamCtx(fake_response),
        ):
            chunks = list(client.chat_stream_messages(
                provider=provider,
                payload={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
            ))

        assert len(chunks) == 1
        assert chunks[0]["choices"][0]["delta"]["content"] == "delivered"

    def test_http_error_before_done_still_raises(self):
        """httpx error during iteration before [DONE] → ProviderHTTPError."""
        class FailLines:
            def __init__(self):
                self.status_code = 200

            def iter_lines(self):
                yield _sse_data_lines("partial")[0]
                raise httpx.RemoteProtocolError("reset")

            def close(self):
                pass

            def read(self):
                return b""

        class FailStreamCtx:
            def __enter__(self):
                return FailLines()
            def __exit__(self, *exc):
                return False

        client = OpenAICompatibleClient()
        provider = _make_provider()

        with patch(
            "app.providers.openai_compat_client.bounded_stream",
            return_value=FailStreamCtx(),
        ):
            with pytest.raises(ProviderHTTPError):
                for _ in client.chat_stream_messages(
                    provider=provider,
                    payload={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
                ):
                    pass

    def test_cleanup_error_before_done_still_raises(self):
        """aclose() error with [DONE] never seen → must propagate."""
        client = OpenAICompatibleClient()
        provider = _make_provider()
        lines = [_sse_data_lines("partial")[0]]
        fake_response = _FakeSyncResponse(
            lines, raise_on_close=httpx.RemoteProtocolError("lost"),
        )

        with patch(
            "app.providers.openai_compat_client.bounded_stream",
            return_value=_FakeSyncStreamCtx(fake_response),
        ):
            raised = False
            try:
                for _ in client.chat_stream_messages(
                    provider=provider,
                    payload={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
                ):
                    pass
            except ProviderHTTPError:
                raised = True
        assert raised, "aclose() error before [DONE] must not be silently suppressed"
