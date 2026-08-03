"""
Embedded API server lifecycle tests (app/core/server.py).
"""

import socket
import urllib.request

from app.core.server import EmbeddedServer


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def test_start_serves_and_stop():
    port = _free_port()
    server = EmbeddedServer()

    server.start(host="127.0.0.1", port=port)
    assert server.running

    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5)
    assert resp.status == 200

    server.stop()
    assert not server.running


def test_stop_is_idempotent():
    port = _free_port()
    server = EmbeddedServer()

    server.start(host="127.0.0.1", port=port)
    server.stop()
    server.stop()
    assert not server.running


def test_start_is_idempotent():
    port = _free_port()
    server = EmbeddedServer()

    server.start(host="127.0.0.1", port=port)
    server.start(host="127.0.0.1", port=port)
    assert server.running

    server.stop()
    assert not server.running
