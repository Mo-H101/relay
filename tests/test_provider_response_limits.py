"""Regression tests for bounded upstream response bodies and streams."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.core.config import settings
from app.providers.base import Provider
from app.providers.exceptions import ProviderResponseLimit
from app.providers.openai_compat_client import (
    OpenAICompatibleClient,
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


class _ScriptedHandler(BaseHTTPRequestHandler):
    """Serve a canned GET /v1/models body over a real socket."""

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802 - http.server API
        body = self.server.scripted_body  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def scripted_models_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _wire_provider(base_url: str) -> Provider:
    return Provider(
        name="fake-openai",
        base_url=base_url,
        requires_api_key=False,
    )


def test_sync_model_discovery_over_real_socket_succeeds(
    scripted_models_server,
):
    """
    Guard the sync wire plumbing: model discovery must work through the
    real client path over an actual HTTP socket.

    The bounded-response hook can only be installed on httpx Clients, so
    the sync wire paths must never call the top-level ``httpx.get/post/
    stream`` helpers again (they reject ``event_hooks``, which turned
    every outbound request into a TypeError).
    """
    scripted_models_server.scripted_body = json.dumps(
        {"data": [{"id": "model-a"}, {"id": "model-b"}]}
    ).encode("utf-8")
    provider = _wire_provider(
        f"http://127.0.0.1:{scripted_models_server.server_address[1]}/v1"
    )

    models = OpenAICompatibleClient().list_models(provider)

    assert models == ["model-a", "model-b"]


def test_sync_wire_path_enforces_byte_budget(
    scripted_models_server, monkeypatch
):
    """
    The byte budget must hold on the real sync client path, not only on
    hand-built httpx Clients.
    """
    monkeypatch.setattr(
        settings, "provider_max_response_bytes", 64, raising=False
    )
    scripted_models_server.scripted_body = json.dumps(
        {"data": [{"id": "x" * 256}]}
    ).encode("utf-8")
    provider = _wire_provider(
        f"http://127.0.0.1:{scripted_models_server.server_address[1]}/v1"
    )

    with pytest.raises(ProviderResponseLimit):
        OpenAICompatibleClient().list_models(provider)
