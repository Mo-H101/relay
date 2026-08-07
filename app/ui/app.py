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
from app.ui.screens.applications import ApplicationsScreen
from app.ui.screens.chat import ChatScreen
from app.ui.screens.configuration import ConfigurationScreen
from app.ui.screens.dashboard import DashboardScreen
from app.ui.screens.diagnostics import DiagnosticsScreen
from app.ui.screens.models import ModelsScreen
from app.ui.screens.providers import ProvidersScreen
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
        Binding("escape", "go_dashboard", "Back to Dashboard"),
        # Priority variants navigate even while an Input or Select holds
        # focus and would otherwise consume the plain digit keys.
        Binding("ctrl+1", "tab('dashboard')", "Dashboard", priority=True, show=False),
        Binding("ctrl+2", "tab('chat')", "Chat", priority=True, show=False),
        Binding("ctrl+3", "tab('models')", "Models", priority=True, show=False),
        Binding("ctrl+4", "tab('providers')", "Providers", priority=True, show=False),
        Binding("ctrl+5", "tab('configuration')", "Configuration", priority=True, show=False),
        Binding("ctrl+6", "tab('applications')", "Applications", priority=True, show=False),
        Binding("ctrl+7", "tab('diagnostics')", "Diagnostics", priority=True, show=False),
        Binding("ctrl+q", "quit", "Quit", priority=True),
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

    #chat-root {{
        padding: 0 1;
        height: 1fr;
    }}

    #chat-controls {{
        height: 3;
        margin-bottom: 1;
        align: center middle;
    }}

    #chat-controls Input {{
        width: 2fr;
        margin: 0 1;
    }}

    #model-picker {{
        width: 30;
        margin: 0 1;
    }}

    #chat-status {{
        height: 1;
        padding: 0 1;
        margin-top: 1;
    }}

    .hidden {{
        display: none;
    }}

    #models-root, #providers-root {{
        padding: 0 1;
        height: 1fr;
    }}

    #models-controls, #providers-controls {{
        height: 3;
        margin-bottom: 1;
        align: center middle;
    }}

    #models-controls > *, #providers-controls > * {{
        margin: 0 1;
    }}

    #provider-toggle {{
        width: 24;
    }}

    #models-root DataTable, #providers-root DataTable {{
        height: 1fr;
        margin-bottom: 1;
    }}

    #models-status, #providers-status {{
        height: 1;
        padding: 0 1;
        margin-top: 1;
    }}

    #config-root, #applications-root, #diagnostics-root {{
        padding: 0 1;
        height: 1fr;
    }}

    .config-group {{
        color: {theme.accent};
        text-style: bold;
        margin-top: 1;
    }}

    .config-note {{
        color: {theme.warn};
        height: 1;
        margin-top: 1;
    }}

    .config-field {{
        margin-bottom: 1;
    }}

    .config-row {{
        height: auto;
    }}

    .config-label {{
        width: 32;
        color: {theme.text_bright};
        padding: 0 1 0 0;
    }}

    .config-hint {{
        color: {theme.muted};
        height: 1;
    }}

    .config-secret {{
        color: {theme.warn};
    }}

    #config-controls, #applications-controls, #diag-controls, #diag-probe-controls {{
        height: 3;
        margin: 1 0;
        align: left middle;
    }}

    #config-controls > *, #applications-controls > *, #diag-controls > *, #diag-probe-controls > * {{
        margin: 0 1 0 0;
    }}

    #config-status, #applications-status, #diagnostics-status {{
        height: 1;
        padding: 0 1;
        margin-top: 1;
    }}

    #auth-line, #endpoint-line, #diag-summary {{
        height: 1;
        color: {theme.text};
        margin-bottom: 1;
    }}

    #applications-root DataTable, #diagnostics-root DataTable {{
        height: 1fr;
        margin-bottom: 1;
    }}

    #export-path {{
        width: 40;
    }}

    #provider-select {{
        width: 24;
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
            "chat": ChatScreen(self._facade),
            "models": ModelsScreen(self._facade),
            "providers": ProvidersScreen(self._facade),
            "configuration": ConfigurationScreen(self._facade),
            "applications": ApplicationsScreen(self._facade),
            "diagnostics": DiagnosticsScreen(self._facade),
        }

    async def on_mount(self) -> None:
        # Install the screens so switching tabs suspends/resumes them
        # instead of removing them from the DOM. Removing a screen on
        # switch would (a) deadlock when the switch originates from a
        # Button.Pressed handler on that screen (the removal waits on the
        # screen's message pump while the handler awaits the removal) and
        # (b) discard every screen's transient state, e.g. the chat
        # transcript, on each tab switch.
        for name, screen in self._screens.items():
            self.install_screen(screen, name)

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

    async def action_go_dashboard(self) -> None:
        await self.switch_screen(self._screens["dashboard"])

    def _mark_server_running(self, running: bool) -> None:
        self._facade._relay._embedded_server_running = bool(running)
