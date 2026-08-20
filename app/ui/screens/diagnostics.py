"""
P2d Diagnostics screen (tab 7).

Read-only operations tail (metadata only), redacted JSON file-log tail,
a per-provider health deep view, an explicit per-provider test
connection, and a redacted snapshot export. Every export passes through
the redaction layer before its atomic file write, so fake API keys,
Authorization headers, and request content can never appear in a file.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Select, Static

from app.ui.data import ServiceFacade


class DiagnosticsScreen(Screen):
    """
    Tab 7. Summary line, ops/log tails, health deep view, and the
    export/test-connection actions.
    """

    BINDINGS = [
        Binding("r", "refresh_diagnostics", "Refresh"),
    ]

    def __init__(self, facade: ServiceFacade) -> None:
        super().__init__()
        self._facade = facade
        self._selected_provider: str = ""
        self._providers: list[str] = []
        self._last_updated: float = 0.0

    # ------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Diagnostics", classes="screen-title")
        with Vertical(id="diagnostics-root"):
            yield Static("", id="diag-summary")
            yield Static("Operations tail (metadata only)", classes="config-group")
            yield DataTable(id="ops-table", cursor_type="row")
            yield Static("File log tail (redacted)", classes="config-group")
            yield DataTable(id="log-table", cursor_type="row")
            yield Static("Provider health", classes="config-group")
            with Horizontal(id="diag-probe-controls"):
                yield Select([], id="provider-select")
                yield Button("Test connection", id="test-conn")
            yield DataTable(id="health-table", cursor_type="row")
            yield Static("Continuity recovery (read-only)", classes="config-group")
            yield DataTable(id="continuity-table", cursor_type="row")
            with Horizontal(id="diag-controls"):
                yield Input(
                    placeholder="Export path",
                    id="export-path",
                )
                yield Button("Export snapshot", id="export-btn")
                yield Button("Refresh", id="diag-refresh")
        yield Static("", id="diagnostics-status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#export-path", Input).value = self._default_export_path()
        self._refresh_all()

    def on_screen_resume(self) -> None:
        self._refresh_all()

    # ------------------------------------------------------------- helpers

    def _default_export_path(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return str(
            Path(self._facade.env_file_path()).parent
            / f"relay-diagnostics-{stamp}.json"
        )

    def _status(self) -> Static:
        return self.query_one("#diagnostics-status", Static)

    def _set_status(self, text: str) -> None:
        self._status().update(text)

    def _refresh_all(self) -> None:
        self._refresh_summary()
        self._refresh_ops_table()
        self._refresh_log_table()
        self._refresh_provider_select()
        self._refresh_health_table()
        self._refresh_continuity_table()
        self._last_updated = time.monotonic()

    def _refresh_summary(self) -> None:
        stats = self._facade.ops_stats()
        auth = self._facade.auth_status()
        keyring_health = self._facade.keyring_health()

        self.query_one("#diag-summary", Static).update(
            f"Ops window: {stats.get('requests', 0)} requests, "
            f"{stats.get('success_rate') or 0.0:.2f} success rate, "
            f"{stats.get('chats', 0)} chats, "
            f"{int(auth.failures)} auth failures, "
            f"persistence {'enabled' if self._facade.persistence_enabled() else 'disabled'}"
            + (
                f"   keyring error: {keyring_health['error']}"
                if not keyring_health["ok"]
                else ""
            )
        )

    def _refresh_ops_table(self) -> None:
        table = self.query_one("#ops-table", DataTable)
        table.clear(columns=True)
        table.add_columns(
            "Age", "Kind", "Method", "Route", "Status", "Latency", "Provider", "Model"
        )

        for row_index, event in enumerate(self._facade.ops_tail(limit=200)):
            latency = f"{event.latency_ms:.0f}ms" if event.latency_ms else ""
            table.add_row(
                f"{event.age_seconds}s",
                event.kind,
                event.method or "-",
                event.route or event.endpoint or "-",
                str(event.status) if event.status is not None else "-",
                latency,
                event.provider or "-",
                event.model or "-",
                key=f"ops-{row_index}",
            )

    def _refresh_log_table(self) -> None:
        table = self.query_one("#log-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Time", "Level", "Event", "Data")

        result = self._facade.log_tail(limit=30)

        for row_index, entry in enumerate(result.get("entries", [])):
            table.add_row(
                entry.ts[:23],
                entry.level,
                entry.event,
                entry.data,
                key=f"log-{row_index}",
            )

        if not result.get("entries"):
            note = result.get("error") or "No log entries."
            table.add_row("-", "-", "-", note)

    def _refresh_provider_select(self) -> None:
        self._providers = [provider.name for provider in self._facade.providers()]
        select = self.query_one("#provider-select", Select)
        select.set_options([(name, name) for name in self._providers])

        if self._providers:
            if self._selected_provider not in self._providers:
                self._selected_provider = self._providers[0]
            select.value = self._selected_provider

    def _refresh_health_table(self) -> None:
        table = self.query_one("#health-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Model", "Health", "Snapshot", "Latency", "Learned")

        if not self._selected_provider:
            return

        deep = self._facade.provider_health_deep(self._selected_provider)

        if not deep.get("found"):
            return

        for model in deep.get("models", []):
            latency = (
                f"{model['latency_ms']}ms" if model.get("latency_ms") else ""
            )
            table.add_row(
                model["name"],
                model["health"],
                model["snapshot"],
                latency,
                ",".join(model.get("learned") or []) or "-",
                key=model["name"],
            )

    def _refresh_continuity_table(self) -> None:
        table = self.query_one("#continuity-table", DataTable)
        table.clear(columns=True)
        table.add_columns(
            "Metric", "Value"
        )

        health = self._facade.continuity_health()
        if not health:
            table.add_row("state", "continuity disabled")
            return

        for state_name, count in sorted(health.get("recovery_states", {}).items()):
            table.add_row(f"recovery {state_name}", str(count))

        flusher = health.get("flusher") or {}
        if flusher:
            table.add_row("flusher queued", str(flusher.get("queued", "-")))
            table.add_row(
                "flusher drained",
                str(flusher.get("drained_total", "-")),
            )
            table.add_row(
                "flush errors",
                str(len(flusher.get("flush_errors") or [])),
            )

        preview = health.get("prune_preview") or {}
        if preview:
            table.add_row(
                "prune candidates",
                f"{preview.get('candidates')} (window: "
                f"{preview.get('days')} days)",
            )

    # ------------------------------------------------------------ handlers

    @on(Select.Changed, "#provider-select")
    def _on_provider_changed(self, event: Select.Changed) -> None:
        self._selected_provider = str(event.value)
        self._refresh_health_table()

    @on(Button.Pressed, "#test-conn")
    async def _on_test_connection(self) -> None:
        if not self._selected_provider:
            self._set_status("No provider selected.")
            return

        provider = self._selected_provider
        self._set_status(f"Testing connection to {provider}\u2026")
        try:
            result = await asyncio.to_thread(
                self._facade.test_connection,
                provider,
            )
        except Exception as exc:  # noqa: BLE001 - surface in the status line
            self._set_status(f"Test connection failed: {exc}")
        else:
            if result.get("ok"):
                self._set_status(
                    f"{provider} reachable via {result.get('model')} "
                    f"({result.get('status')}, {result.get('latency_ms')}ms)."
                )
            else:
                self._set_status(
                    f"{provider} unreachable: "
                    f"{result.get('error') or result.get('status')}"
                )

    @on(Button.Pressed, "#export-btn")
    async def _on_export(self) -> None:
        path = self.query_one("#export-path", Input).value.strip()

        if not path:
            self._set_status("Enter an export path first.")
            return

        self._set_status(f"Exporting redacted snapshot to {path}\u2026")
        try:
            result = await asyncio.to_thread(
                self._facade.export_diagnostics,
                path,
            )
        except Exception as exc:  # noqa: BLE001 - surface in the status line
            self._set_status(f"Export failed: {exc}")
        else:
            if result.get("ok"):
                self._set_status(
                    f"Exported {result.get('bytes')} bytes to "
                    f"{result.get('path')} at {result.get('generated_at')}."
                )
            else:
                self._set_status(f"Export failed: {result.get('error')}")

    @on(Button.Pressed, "#diag-refresh")
    def _on_refresh(self) -> None:
        self._refresh_all()
        self._set_status("Diagnostics refreshed.")

    def action_refresh_diagnostics(self) -> None:
        self._refresh_all()
        self._set_status("Diagnostics refreshed.")
