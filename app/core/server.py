"""
Embedded API server support for the TUI.

The TUI runs the API server in a background thread inside the same
process (``RELAY_TUI_NO_EMBED=1`` opts out for service-manager setups).
This module is import-safe in non-TUI contexts: uvicorn is only imported
lazily inside the server thread.
"""

from __future__ import annotations

import logging
import threading
import time

from app.core.config import settings

_logger = logging.getLogger("relay")

# Maximum time (seconds) to wait for the embedded server thread to stop
# after a stop request. Kept modest so a hung event loop cannot wedge the
# TUI exit path forever.
_JOIN_TIMEOUT_SECONDS = 10.0

# Maximum time (seconds) to wait for the embedded server to finish binding
# and complete startup before start() returns. Mirrors the historical
# _started.wait() budget.
_START_TIMEOUT_SECONDS = 30.0

# How often start() polls uvicorn's started flag while waiting.
_READINESS_POLL_SECONDS = 0.05


class EmbeddedServer:
    """
    Run the Relay FastAPI app (``app.main.app``) in a daemon thread.

    A shared ``relay`` singleton is used by both the embedded server and
    the TUI, so API writes and TUI reads observe the same in-memory state
    (e.g. telemetry recorded by the server shows up in the Applications
    and Diagnostics panels). The thread is a daemon so a TUI crash cannot
    leak a blocking process; the server is stopped explicitly on TUI exit.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, *, host: str | None = None, port: int | None = None) -> None:
        """
        Start the embedded server. No-op when already running.
        """
        if self.running:
            return

        host = host or settings.relay_host
        port = port if port is not None else settings.relay_port

        def _run() -> None:
            # Imported lazily inside the thread so that merely importing
            # app.core.server never drags in uvicorn, and so the import
            # failure (missing optional dependency) surfaces here rather
            # than at module import time.
            import uvicorn

            from app.main import app as fastapi_app

            config = uvicorn.Config(
                fastapi_app,
                host=host,
                port=port,
                log_level=settings.log_level.lower(),
                access_log=False,
                log_config=None,
            )
            server = uvicorn.Server(config)
            self._server = server
            server.run()

        self._thread = threading.Thread(
            target=_run,
            name="relay-embedded-server",
            daemon=True,
        )
        self._thread.start()

        # Wait for real readiness (socket bound + startup complete), not
        # just thread start: uvicorn only sets server.started after binding
        # the port, so returning earlier races with the first client
        # request (ConnectionRefused on slow machines).
        deadline = time.monotonic() + _START_TIMEOUT_SECONDS

        while time.monotonic() < deadline:
            server = getattr(self, "_server", None)

            if server is not None and getattr(server, "started", False):
                break

            if not self.running:
                break

            time.sleep(_READINESS_POLL_SECONDS)

        if not self.running or (
            getattr(self, "_server", None) is not None
            and not getattr(self._server, "started", False)
        ):
            raise RuntimeError("embedded API server failed to start")

    def stop(self) -> None:
        """
        Stop the embedded server and wait for the thread to finish.
        """
        thread = self._thread
        server = getattr(self, "_server", None)
        if thread is None or not thread.is_alive():
            self._thread = None
            return

        if server is not None:
            server.should_exit = True

        thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
        if thread.is_alive():
            _logger.warning(
                "embedded API server did not stop within %ss",
                _JOIN_TIMEOUT_SECONDS,
            )
        self._thread = None
        self._server = None
