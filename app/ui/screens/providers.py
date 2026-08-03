"""
P2c Providers screen (tab 4).

Shows every registry provider merged with its runtime state. "Add provider"
and "Re-run setup" drive the existing setup wizard behind
``app.ui.setup_adapter.SetupAdapter`` (key entry is password-masked and
validated before anything is persisted). API keys are never rendered: the
table only shows a boolean set/missing/n-a column. Rescan re-runs the
availability scan for the selected provider.
"""

from __future__ import annotations

import asyncio

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Static

from app.ui.data import ServiceFacade
from app.ui.setup_adapter import SetupAdapter


class ProvidersScreen(Screen):
    """
    Tab 4. Row keys are provider definition ids; "Add provider" runs the
    wizard restricted to unconfigured providers, "Re-run setup" runs the
    full menu.
    """

    BINDINGS = [
        Binding("a", "add_provider", "Add provider"),
        Binding("s", "re_run_setup", "Re-run setup"),
        Binding("n", "rescan", "Rescan"),
        Binding("t", "toggle_provider", "Toggle"),
        Binding("r", "refresh_providers", "Refresh"),
    ]

    def __init__(self, facade: ServiceFacade) -> None:
        super().__init__()
        self._facade = facade
        self._selected: str | None = None
        self._busy = False

    # ------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Providers", classes="screen-title")
        with Vertical(id="providers-root"):
            yield DataTable(id="providers-table", cursor_type="row")
            with Horizontal(id="providers-controls"):
                yield Button("Add provider", id="add-provider")
                yield Button("Re-run setup", id="rerun-setup")
                yield Button("Rescan", id="rescan")
                yield Button("Toggle", id="toggle-provider")
                yield Button("Refresh", id="providers-refresh")
            yield Static("", id="providers-status")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh()

    # ------------------------------------------------------------- helpers

    def _table(self) -> DataTable:
        return self.query_one("#providers-table", DataTable)

    def _status(self) -> Static:
        return self.query_one("#providers-status", Static)

    def _set_status(self, text: str) -> None:
        self._status().update(text)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for widget_id in (
            "add-provider",
            "rerun-setup",
            "rescan",
            "toggle-provider",
            "providers-refresh",
        ):
            self.query_one(f"#{widget_id}", Button).disabled = busy

    def append_status(self, text: str) -> None:
        """
        Wizard notice sink, invoked on the UI thread by the SetupAdapter.
        """
        self._set_status(text)

    def _refresh(self) -> None:
        table = self._table()
        table.clear(columns=True)
        table.add_columns("", "Provider", "Kind", "Status", "API key", "Models")

        keys: list[str] = []
        for entry in self._facade.provider_catalog():
            status = (
                "enabled"
                if entry.enabled
                else "disabled"
                if entry.configured
                else "not configured"
            )
            key_cell = "set" if entry.has_api_key else "missing"
            if not entry.requires_api_key:
                key_cell = "n/a"
            glyph = "\u2713" if entry.enabled else "\u2717"
            if not entry.configured:
                glyph = "-"
            table.add_row(
                glyph,
                entry.display_name,
                entry.kind,
                status,
                key_cell,
                str(entry.model_count),
                key=entry.id,
            )
            keys.append(entry.id)

        if keys and self._selected not in keys:
            self._selected = keys[0]
        if keys:
            table.cursor_coordinate = (0, keys.index(self._selected))

    # ------------------------------------------------------------ handlers

    @on(DataTable.RowHighlighted, "#providers-table")
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value
        self._selected = key if isinstance(key, str) else None

    @on(Button.Pressed, "#add-provider")
    async def _on_add_provider(self) -> None:
        await self._run_wizard(menu=self._facade.unconfigured_provider_defs())

    @on(Button.Pressed, "#rerun-setup")
    async def _on_rerun_setup(self) -> None:
        await self._run_wizard(menu=self._facade.provider_menu())

    @on(Button.Pressed, "#rescan")
    async def _on_rescan(self) -> None:
        await self._rescan()

    @on(Button.Pressed, "#toggle-provider")
    async def _on_toggle_provider(self) -> None:
        await self._toggle_provider()

    @on(Button.Pressed, "#providers-refresh")
    def _on_refresh(self) -> None:
        self._refresh()

    # ------------------------------------------------------------- actions

    async def action_add_provider(self) -> None:
        await self._run_wizard(menu=self._facade.unconfigured_provider_defs())

    async def action_re_run_setup(self) -> None:
        await self._run_wizard(menu=self._facade.provider_menu())

    async def action_rescan(self) -> None:
        await self._rescan()

    async def action_toggle_provider(self) -> None:
        await self._toggle_provider()

    def action_refresh_providers(self) -> None:
        self._refresh()

    async def _run_wizard(self, menu) -> None:
        if self._busy:
            return
        if not menu:
            self._set_status("Every provider is already configured.")
            return

        self._set_busy(True)
        self._set_status("Setup wizard running\u2026")
        adapter = SetupAdapter(self, on_notice=self.append_status)
        try:
            result = await asyncio.to_thread(
                self._facade.run_setup, adapter, menu=menu
            )
        except Exception as exc:  # noqa: BLE001 - surface in the status line
            self._set_status(f"Setup failed: {exc}")
        else:
            self._show_setup_result(result)
        finally:
            self._set_busy(False)
            self._refresh()

    def _show_setup_result(self, result) -> None:
        if result.completed and result.usable:
            self._set_status(
                "Setup complete: "
                + (", ".join(result.configured) or "no providers configured")
                + "."
            )
        elif result.completed:
            self._set_status("Setup finished without usable providers.")
        else:
            self._set_status("Setup exited early; nothing was changed.")

    async def _rescan(self) -> None:
        if self._selected is None:
            self._set_status("Select a provider row first.")
            return

        self._set_busy(True)
        self._set_status("Scanning provider\u2026")
        try:
            report = await asyncio.to_thread(
                self._facade.rescan_models, self._selected
            )
        except Exception as exc:  # noqa: BLE001 - surface in the status line
            self._set_status(f"Scan failed: {exc}")
        else:
            if report.get("ok"):
                self._set_status(
                    f"Scanned {report['provider']}: "
                    f"{report['available']}/{report['models']} available."
                )
            else:
                self._set_status(f"Scan failed: {report.get('error')}")
        finally:
            self._set_busy(False)
            self._refresh()

    async def _toggle_provider(self) -> None:
        if self._selected is None:
            self._set_status("Select a provider row first.")
            return

        entry = next(
            (
                entry
                for entry in self._facade.provider_catalog()
                if entry.id == self._selected
            ),
            None,
        )

        if entry is None:
            self._set_status("Unknown provider.")
            return

        target = not entry.enabled
        self._set_busy(True)
        self._set_status(
            f"Setting {entry.display_name} "
            f"{'enabled' if target else 'disabled'}\u2026"
        )
        try:
            report = await asyncio.to_thread(
                self._facade.set_provider_enabled, self._selected, target
            )
        except Exception as exc:  # noqa: BLE001 - surface in the status line
            self._set_status(f"Failed to toggle provider: {exc}")
        else:
            self._show_reload(
                report,
                f"{entry.display_name} {'enabled' if target else 'disabled'}",
            )
        finally:
            self._set_busy(False)
            self._refresh()

    def _show_reload(self, report: dict, label: str) -> None:
        if report.get("reloaded"):
            applied = report.get("applied", [])
            self._set_status(
                f"{label} saved and applied (reloaded {len(applied)} field(s))."
            )
        else:
            detail = report.get("error") or report.get("failures") or "unknown"
            self._set_status(f"{label} saved; reload failed: {detail}")
