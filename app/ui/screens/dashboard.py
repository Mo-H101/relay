"""Dashboard screen — the TUI landing tab (Stage C redesign)."""

from __future__ import annotations

import time

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Collapsible, Footer, Header, Static

from app.ui.data import DashboardSummary
from app.ui.theme import theme
from app.ui.widgets import StatTile


class DashboardScreen(Screen):
    """
    Overview of server state, provider/model availability, and recent
    activity. Data comes from the ServiceFacade view-model so the screen
    stays free of core imports.

    Stage C redesign: grouped sections (System / Activity / Config),
    auto-refresh timer (30s), "last updated" indicator.
    """

    BINDINGS = [
        Binding("d", "refresh_dashboard", "Refresh"),
    ]

    def __init__(self, facade) -> None:
        super().__init__()
        self._facade = facade
        self._last_updated: float = 0.0
        self._refresh_timer = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="dashboard-title", classes="screen-title")

        # ── System group ──────────────────────────────────────────
        with Vertical(classes="dashboard-group"):
            yield Static(
                f"[{theme.text_bright}]System[/]", classes="group-heading"
            )
            with Horizontal(classes="tile-row tile-row--system"):
                yield StatTile("Server", id="tile-server")
                yield StatTile("Setup", id="tile-setup")
                yield StatTile("Preferred provider", id="tile-provider")
            with Horizontal(classes="tile-row tile-row--system"):
                yield StatTile("Providers", id="tile-providers")
                yield StatTile("Models", id="tile-models")

        # ── Activity group ────────────────────────────────────────
        with Vertical(classes="dashboard-group"):
            yield Static(
                f"[{theme.text_bright}]Activity[/]", classes="group-heading"
            )
            with Horizontal(classes="tile-row tile-row--activity"):
                yield StatTile("Requests", id="tile-requests")
                yield StatTile("Success rate", id="tile-success")
                yield StatTile("Avg latency", id="tile-latency")
                yield StatTile("Chats", id="tile-chats")

        # ── Configuration (collapsed by default) ──────────────────
        with Collapsible(
            title="Configuration",
            id="config-collapse",
            collapsed=True,
        ):
            with Horizontal(classes="tile-row tile-row--config"):
                yield StatTile("Persistence", id="tile-persistence")
                yield StatTile("Env file", id="tile-env")
                yield StatTile("State dir", id="tile-state")

        # ── Status line ───────────────────────────────────────────
        yield Static("", id="dashboard-status", classes="status-line")
        yield Footer()

    async def on_mount(self) -> None:
        self._refresh_timer = self.set_interval(30.0, self._auto_refresh)
        await self.refresh_summary()

    async def on_unmount(self) -> None:
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
            self._refresh_timer = None

    async def on_screen_resume(self) -> None:
        if self._refresh_timer is None:
            self._refresh_timer = self.set_interval(30.0, self._auto_refresh)

    async def _auto_refresh(self) -> None:
        await self.refresh_summary()

    # ── actions ───────────────────────────────────────────────────

    def action_refresh_dashboard(self) -> None:
        self.app.run_worker(self.refresh_summary())

    # ── data binding ──────────────────────────────────────────────

    async def refresh_summary(self) -> None:
        summary = self._facade.dashboard_summary()
        self._last_updated = time.monotonic()
        self._render_summary(summary)

    def _render_summary(self, summary: DashboardSummary) -> None:
        self.query_one("#dashboard-title", Static).update(
            f"{summary.relay_name} — Dashboard"
        )

        server_tile = self.query_one("#tile-server", StatTile)
        server_tile.update_value(
            "Running" if summary.server.running else "Stopped",
            theme.ok if summary.server.running else theme.error,
        )
        self.query_one("#tile-setup", StatTile).update_value(summary.setup_state)
        self.query_one("#tile-provider", StatTile).update_value(
            summary.default_provider
        )
        self.query_one("#tile-providers", StatTile).update_value(
            f"{summary.enabled_providers}/{summary.provider_count}"
        )
        self.query_one("#tile-models", StatTile).update_value(
            f"{summary.healthy_models}/{summary.model_count}"
        )
        self.query_one("#tile-requests", StatTile).update_value(str(summary.requests))
        success_text = (
            f"{summary.success_rate:.1%}" if summary.success_rate is not None else "-"
        )
        self.query_one("#tile-success", StatTile).update_value(success_text)
        latency_text = (
            f"{summary.average_latency_ms:.0f} ms"
            if summary.average_latency_ms is not None
            else "-"
        )
        self.query_one("#tile-latency", StatTile).update_value(latency_text)
        self.query_one("#tile-chats", StatTile).update_value(str(summary.chats))
        persistence_text = (
            "on" if summary.persistence_enabled else "off"
        )
        self.query_one("#tile-persistence", StatTile).update_value(
            persistence_text,
            theme.ok if summary.persistence_enabled else theme.muted,
        )
        self.query_one("#tile-env", StatTile).update_value(summary.env_file)
        self.query_one("#tile-state", StatTile).update_value(summary.state_dir)

        status = (
            f"[{theme.muted}]API:[/] {summary.server.url}"
            + (f"   [{theme.warn}]persistence error:[/] {summary.persistence_error}"
               if summary.persistence_error else "")
            + (f"   [{theme.warn}]WARNING: API authentication is disabled"
               f" (no RELAY_API_KEY, RELAY_AUTH_STORE off)[/]"
               if not summary.auth_enabled else "")
        )
        if self._last_updated:
            age = max(0, int(time.monotonic() - self._last_updated))
            if age < 5:
                status += f"   [{theme.muted}]updated just now[/]"
            else:
                status += f"   [{theme.muted}]updated {age}s ago[/]"
        self.query_one("#dashboard-status", Static).update(status)
