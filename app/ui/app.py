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

from app import __version__
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

_logger = logging.getLogger("relay.ui")

_TAB_DESCRIPTIONS = {
    "dashboard": "Overview of system health, activity, and configuration",
    "chat": "Chat with your configured providers",
    "models": "Model availability and priority controls",
    "providers": "Provider keys, scanning, and setup",
    "configuration": "Routing, failover, and server settings",
    "applications": "Client activity and endpoint/auth status",
    "diagnostics": "Operations tail, health, and export",
}


class RelayApp(App[None]):
    """
    Main TUI app: 7 tabs (Dashboard 1 … Diagnostics 7), global keymap,
    embedded API server hooks, and a single ServiceFacade that every
    screen reads.
    """

    TITLE = "Relay"
    SUB_TITLE = f"AI gateway terminal  ·  v{__version__}"

    CSS_PATH = "styles/base.tcss"

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
        self.sub_title = f"AI gateway terminal  ·  v{__version__}  ·  {_TAB_DESCRIPTIONS['dashboard']}"

    async def on_unmount(self) -> None:
        self._mark_server_running(False)

    async def action_tab(self, name: str) -> None:
        await self.switch_screen(self._screens[name])
        self.sub_title = f"AI gateway terminal  ·  v{__version__}  ·  {_TAB_DESCRIPTIONS.get(name, '')}"

    async def action_go_dashboard(self) -> None:
        await self.switch_screen(self._screens["dashboard"])
        self.sub_title = f"AI gateway terminal  ·  v{__version__}  ·  {_TAB_DESCRIPTIONS['dashboard']}"

    def _mark_server_running(self, running: bool) -> None:
        self._facade._relay._embedded_server_running = bool(running)
