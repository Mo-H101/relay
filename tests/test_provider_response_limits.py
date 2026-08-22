"""Regression tests for bounded upstream response bodies and streams."""

import httpx
import pytest

from app.providers.exceptions import ProviderResponseLimit
from app.providers.openai_compat_client import (
    bounded_aiter_lines,
    bounded_iter_lines,
)
from app.providers.transport_limits import BoundedResponseHook


def test_sync_provider_body_is_bounded_before_client_returns():
    hook = BoundedResponseHook(
        max_bytes=3, max_chunk_bytes=16, max_seconds=10
    )
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"1234")
        ),
        event_hooks={"response": [hook]},
    )
    try:
        with pytest.raises(ProviderResponseLimit):
            client.get("https://provider.invalid/models")
    finally:
        client.close()


@pytest.mark.asyncio
async def test_async_provider_body_is_bounded_before_client_returns():
    hook = BoundedResponseHook(
        max_bytes=3, max_chunk_bytes=16, max_seconds=10
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"1234")
        ),
        event_hooks={"response": [hook]},
    )
    try:
        with pytest.raises(ProviderResponseLimit):
            await client.get("https://provider.invalid/models")
    finally:
        await client.aclose()


def test_sync_stream_line_budget_is_enforced():
    hook = BoundedResponseHook(
        max_bytes=1024, max_chunk_bytes=4, max_seconds=10
    )
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"12345\n")
        ),
        event_hooks={"response": [hook]},
    )
    try:
        with client.stream("GET", "https://provider.invalid/stream") as response:
            with pytest.raises(ProviderResponseLimit):
                list(bounded_iter_lines(response))
    finally:
        client.close()


@pytest.mark.asyncio
async def test_async_stream_line_budget_is_enforced():
    hook = BoundedResponseHook(
        max_bytes=1024, max_chunk_bytes=4, max_seconds=10
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"12345\n")
        ),
        event_hooks={"response": [hook]},
    )
    try:
        async with client.stream(
            "GET", "https://provider.invalid/stream"
        ) as response:
            with pytest.raises(ProviderResponseLimit):
                async for _line in bounded_aiter_lines(response):
                    pass
    finally:
        await client.aclose()
