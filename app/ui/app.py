"""
RelayApp — the main Relay terminal interface.

The only module allowed to import Textual outside the screen/widget
modules. Wires the ServiceFacade, the 7-tab navigation, the embedded
API server, and global key bindings together.
"""

from __future__ import annotations

import asyncio
import logging

from textual.app import App, Binding
from textual.binding import BindingType

from app.core import config
from app.core.server import EmbeddedServer
from app.ui.data import ServiceFacade
from app.ui.screens.dashboard import DashboardScreen
from app.ui.screens.placeholder import PlaceholderScreen
from app.ui.theme import theme

_logger = logging.getLogger("relay.ui")

NOTES = {
    "chat": "Chat with your configured providers.",
    "models": "Model availability and priority controls.",
    "providers": "Provider keys, scanning, and setup.",
    "configuration": "Routing, failover, and server settings.",
    "applications": "Client activity and endpoint/auth status.",
    "diagnostics": "Operations tail, health, and export.",
}


class RelayApp(App[None]):
    """
    Main TUI app: 7 tabs (Dashboard 1 … Diagnostics 7), global keymap,
    embedded API server hooks, and a single ServiceFacade that every
    screen reads.
    """

    TITLE = "Relay"
    SUB_TITLE = "AI gateway terminal"

    BINDINGS: list[BindingType] = [
        Binding("1", "tab('dashboard')", "Dashboard"),
        Binding("2", "tab('chat')", "Chat"),
        Binding("3", "tab('models')", "Models"),
        Binding("4", "tab('providers')", "Providers"),
        Binding("5", "tab('configuration')", "Configuration"),
        Binding("6", "tab('applications')", "Applications"),
        Binding("7", "tab('diagnostics')", "Diagnostics"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", priority=True),
    ]

    CSS = f"""
    Screen {{
        background: {theme.background};
    }}

    .screen-title {{
        padding: 0 1;
        height: 1;
        margin-bottom: 1;
    }}

    .tile-row {{
        height: 5;
        margin: 0 1 1 1;
    }}

    StatTile {{
        border: round {theme.panel_border};
        background: {theme.surface};
        padding: 0 1;
        width: 1fr;
        margin: 0 1;
    }}

    .stat-label {{
        color: {theme.muted};
        height: 1;
    }}

    .stat-value {{
        color: {theme.text_bright};
        height: 1;
        text-overflow: ellipsis;
    }}

    .status-line {{
        padding: 0 1;
        height: 1;
    }}

    .placeholder-title {{
        color: {theme.accent};
        text-style: bold;
    }}

    .placeholder-note {{
        color: {theme.muted};
    }}
    """

    def __init__(
        self,
        facade: ServiceFacade | None = None,
        embedded_server: EmbeddedServer | None = None,
        *,
        start_server: bool = True,
    ) -> None:
        super().__init__()
        self._facade = facade or ServiceFacade()
        self._server = embedded_server or EmbeddedServer()
        self._start_server = start_server and not config.settings.relay_tui_no_embed

        self._screens = {
            "dashboard": DashboardScreen(self._facade),
            "chat": PlaceholderScreen("Chat", NOTES["chat"]),
            "models": PlaceholderScreen("Models", NOTES["models"]),
            "providers": PlaceholderScreen("Providers", NOTES["providers"]),
            "configuration": PlaceholderScreen(
                "Configuration", NOTES["configuration"]
            ),
            "applications": PlaceholderScreen(
                "Applications", NOTES["applications"]
            ),
            "diagnostics": PlaceholderScreen(
                "Diagnostics", NOTES["diagnostics"]
            ),
        }

    async def on_mount(self) -> None:
        if self._start_server and not self._server.running:
            try:
                await asyncio.to_thread(self._server.start)
            except Exception:
                _logger.exception("embedded API server failed to start")
            else:
                self._mark_server_running(True)

        await self.push_screen(self._screens["dashboard"])

    async def on_unmount(self) -> None:
        self._mark_server_running(False)

    async def action_tab(self, name: str) -> None:
        await self.switch_screen(self._screens[name])

    def _mark_server_running(self, running: bool) -> None:
        self._facade._relay._embedded_server_running = bool(running)
