"""Bounded response streams for outbound provider requests.

Provider responses are untrusted input. The limits live below the individual
provider clients so synchronous, asynchronous, streaming, and model-probe
requests share one enforcement boundary without changing wire framing.
"""

from __future__ import annotations

import time

import httpx

from app.providers.exceptions import ProviderResponseLimit


class _AwaitableHookResult:
    """A no-op awaitable returned by a hook used by sync and async clients."""

    def __await__(self):
        if False:
            yield None
        return None


class _Budget:
    def __init__(self, max_bytes: int, max_chunk_bytes: int, max_seconds: float):
        self.max_bytes = max(1, int(max_bytes))
        self.max_chunk_bytes = max(1, int(max_chunk_bytes))
        self.max_seconds = max(1.0, float(max_seconds))
        self.total = 0
        self.started = time.monotonic()

    def check(self, chunk: bytes) -> bytes:
        size = len(chunk)
        self.total += size
        if size > self.max_chunk_bytes:
            raise ProviderResponseLimit("provider response chunk exceeded limit")
        if self.total > self.max_bytes:
            raise ProviderResponseLimit("provider response exceeded byte limit")
        if time.monotonic() - self.started > self.max_seconds:
            raise ProviderResponseLimit("provider response exceeded time limit")
        return chunk

    def check_line(self, line: str) -> str:
        encoded = line.encode("utf-8", errors="replace")
        if len(encoded) > self.max_chunk_bytes:
            raise ProviderResponseLimit("provider response line exceeded limit")
        if time.monotonic() - self.started > self.max_seconds:
            raise ProviderResponseLimit("provider response exceeded time limit")
        return line


class _BoundedSyncByteStream(httpx.SyncByteStream):
    def __init__(self, stream, budget: _Budget):
        self._stream = stream
        self._budget = budget

    def __iter__(self):
        for chunk in self._stream:
            yield self._budget.check(chunk)

    def close(self) -> None:
        self._stream.close()


class _BoundedAsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, stream, budget: _Budget):
        self._stream = stream
        self._budget = budget

    async def __aiter__(self):
        async for chunk in self._stream:
            yield self._budget.check(chunk)

    async def aclose(self) -> None:
        await self._stream.aclose()


class BoundedResponseHook:
    """Wrap an httpx response body before any caller can consume it."""

    def __init__(
        self,
        *,
        max_bytes: int,
        max_chunk_bytes: int,
        max_seconds: float,
    ) -> None:
        self.max_bytes = max_bytes
        self.max_chunk_bytes = max_chunk_bytes
        self.max_seconds = max_seconds

    def __call__(self, response: httpx.Response):
        content_length = response.headers.get("Content-Length")
        try:
            if content_length is not None and int(content_length) > int(
                self.max_bytes
            ):
                raise ProviderResponseLimit(
                    "provider response exceeded byte limit"
                )
        except ValueError:
            pass

        budget = _Budget(
            self.max_bytes, self.max_chunk_bytes, self.max_seconds
        )
        stream = response.stream
        if isinstance(stream, httpx.AsyncByteStream):
            response.stream = _BoundedAsyncByteStream(stream, budget)
        else:
            response.stream = _BoundedSyncByteStream(stream, budget)

        # SyncClient ignores the hook return value; AsyncClient awaits it.
        return _AwaitableHookResult()

    def __eq__(self, other) -> bool:
        return (
            isinstance(other, BoundedResponseHook)
            and self.max_bytes == other.max_bytes
            and self.max_chunk_bytes == other.max_chunk_bytes
            and self.max_seconds == other.max_seconds
        )

    def __hash__(self) -> int:
        return hash((self.max_bytes, self.max_chunk_bytes, self.max_seconds))


def bounded_iter_lines(response: httpx.Response):
    """Yield response lines while applying the configured line budget."""
    stream = getattr(response, "stream", None)
    budget = getattr(stream, "_budget", None)
    for line in response.iter_lines():
        if budget is not None:
            yield budget.check_line(line)
        else:
            yield line


async def bounded_aiter_lines(response: httpx.Response):
    """Async counterpart of :func:`bounded_iter_lines`."""
    stream = getattr(response, "stream", None)
    budget = getattr(stream, "_budget", None)
    async for line in response.aiter_lines():
        if budget is not None:
            yield budget.check_line(line)
        else:
            yield line


__all__ = [
    "BoundedResponseHook",
    "bounded_iter_lines",
    "bounded_aiter_lines",
]
