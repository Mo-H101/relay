"""
P2d Applications screen (tab 6) — Stage C redesign.

Shows endpoint/auth status and a client-activity table bucketed from
trimmed User-Agent headers (Cline / OpenCode / Continue / Other). All
data is metadata only: the Authorization header value, API keys, request
bodies, prompts, messages, and responses are never captured, stored, or
rendered.

Stage C changes:
- Auto-refresh timer (15s)
- "Last updated" indicator in status line
- Cleaner layout with grouped summary lines
"""

from __future__ import annotations

import time

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Static

from app.ui.data import ServiceFacade
from app.ui.theme import theme


class ApplicationsScreen(Screen):
    """
    Tab 6. Auth posture line, endpoint summary line, and the bounded
    client-activity table.
    """

    BINDINGS = [
        Binding("r", "refresh_applications", "Refresh"),
    ]

    def __init__(self, facade: ServiceFacade) -> None:
        super().__init__()
        self._facade = facade
        self._last_updated: float = 0.0
        self._refresh_timer = None

    # ------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Applications", classes="screen-title")
        with Vertical(id="applications-root"):
            yield Static("", id="auth-line", classes="summary-line")
            yield Static("", id="endpoint-line", classes="summary-line")
            yield Static("Client activity", classes="group-heading")
            yield DataTable(id="client-table", cursor_type="row")
            with Horizontal(id="applications-controls"):
                yield Button("Refresh", id="applications-refresh")
        yield Static("", id="applications-status", classes="status-line")
        yield Footer()

    async def on_mount(self) -> None:
        self._refresh_timer = self.set_interval(15.0, self._auto_refresh)
        self._refresh_all()

    async def on_unmount(self) -> None:
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
            self._refresh_timer = None

    async def on_screen_resume(self) -> None:
        if self._refresh_timer is None:
            self._refresh_timer = self.set_interval(15.0, self._auto_refresh)

    async def _auto_refresh(self) -> None:
        self._refresh_all()

    # ── actions ───────────────────────────────────────────────────

    def action_refresh_applications(self) -> None:
        self._refresh_all()

    # ── data binding ──────────────────────────────────────────────

    def _refresh_all(self) -> None:
        self._last_updated = time.monotonic()

        auth = self._facade.auth_status()

        scheme_text = ", ".join(
            f"{key}: {value}"
            for key, value in sorted(auth.presented.items())
            if value
        )
        state = "enabled" if auth.enabled else "disabled"
        state_color = theme.ok if auth.enabled else theme.muted

        self.query_one("#auth-line", Static).update(
            f"[{state_color}]{state}[/]"
            f"   authenticated: {int(auth.authenticated)}"
            f"   failures: {int(auth.failures)}"
            + (f"   schemes: {scheme_text}" if scheme_text else "")
        )

        endpoints = self._facade.endpoint_status()
        ep_color = (
            theme.ok
            if endpoints["failures"] == 0
            else theme.warn
            if endpoints["failures"] < endpoints["requests"] * 0.1
            else theme.error
        )
        self.query_one("#endpoint-line", Static).update(
            f"[{theme.text_muted}]Endpoints:[/] "
            f"{endpoints['requests']} requests"
            f"   [{theme.ok}]{endpoints['successes']} ok[/]"
            + (
                f"   [{ep_color}]{endpoints['failures']} failed[/]"
                if endpoints["failures"]
                else ""
            )
        )

        self._refresh_client_table()
        self._update_status()

    def _refresh_client_table(self) -> None:
        table = self.query_one("#client-table", DataTable)
        table.clear(columns=True)
        table.add_columns(
            "Client",
            "User-Agent",
            "Route",
            "Req",
            "OK",
            "Fail",
            "Auth",
            "Last seen",
        )

        for entry in self._facade.client_activity():
            table.add_row(
                entry.bucket,
                entry.ua or "-",
                entry.route,
                str(entry.requests),
                str(entry.successes),
                str(entry.failures),
                "/".join(entry.auth_schemes) or "-",
                self._age(entry.last_seen),
                key=(entry.bucket, entry.route),
            )

    def _update_status(self) -> None:
        if self._last_updated:
            age = max(0, int(time.monotonic() - self._last_updated))
            if age < 5:
                self.query_one("#applications-status", Static).update(
                    f"[{theme.muted}]updated just now[/]"
                )
            else:
                self.query_one("#applications-status", Static).update(
                    f"[{theme.muted}]updated {age}s ago  •  auto-refresh 15s[/]"
                )

    @staticmethod
    def _age(timestamp: float) -> str:
        age = max(0, int(time.monotonic() - timestamp))
        return f"{age}s" if age < 60 else f"{age // 60}m"

    # ------------------------------------------------------------ handlers

    @on(Button.Pressed, "#applications-refresh")
    def _on_refresh(self) -> None:
        self._refresh_all()
