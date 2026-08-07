"""
P2d Applications screen (tab 6).

Shows endpoint/auth status and a client-activity table bucketed from
trimmed User-Agent headers (Cline / OpenCode / Continue / Other). All
data is metadata only: the Authorization header value, API keys, request
bodies, prompts, messages, and responses are never captured, stored, or
rendered.
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

    # ------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Applications", classes="screen-title")
        with Vertical(id="applications-root"):
            yield Static("", id="auth-line")
            yield Static("", id="endpoint-line")
            yield Static("Client activity", classes="config-group")
            yield DataTable(id="client-table", cursor_type="row")
            with Horizontal(id="applications-controls"):
                yield Button("Refresh", id="applications-refresh")
        yield Static("", id="applications-status")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_all()

    def on_screen_resume(self) -> None:
        self._refresh_all()

    # ------------------------------------------------------------- helpers

    def _refresh_all(self) -> None:
        auth = self._facade.auth_status()

        scheme_text = ", ".join(
            f"{key}: {value}"
            for key, value in sorted(auth.presented.items())
            if value
        )
        state = "enabled" if auth.enabled else "disabled"

        self.query_one("#auth-line", Static).update(
            f"API-key auth: {state} | authenticated: {int(auth.authenticated)} "
            f"| failures: {int(auth.failures)} "
            f"| presented schemes: {scheme_text or 'none'}"
        )

        endpoints = self._facade.endpoint_status()
        self.query_one("#endpoint-line", Static).update(
            f"Endpoints (last window): {endpoints['requests']} requests, "
            f"{endpoints['successes']} ok, {endpoints['failures']} failed"
        )

        self._refresh_client_table()

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

    @staticmethod
    def _age(timestamp: float) -> str:
        age = max(0, int(time.monotonic() - timestamp))
        return f"{age}s" if age < 60 else f"{age // 60}m"

    # ------------------------------------------------------------ handlers

    @on(Button.Pressed, "#applications-refresh")
    def _on_refresh(self) -> None:
        self._refresh_all()

    def action_refresh_applications(self) -> None:
        self._refresh_all()
