"""
P2d Configuration screen (tab 5).

Live-reloadable routing (TASK_*) and failover/retry settings are edited
here; the save flow goes through ``ServiceFacade.save_config`` which
writes via the single-writer ``config_store``, validates with a dry-run
reload, applies with a real reload, and restores the previous values on
any failure. Restart-required and informational fields are read-only.
No secret/API-key field ever appears in this form: keys stay managed by
the Providers flow.
"""

from __future__ import annotations

import asyncio

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Footer, Header, Input, Static

from app.ui.data import ConfigField, ServiceFacade

_GROUP_TITLES = {
    "routing": "Routing (TASK_*) — applied live",
    "failover": "Failover & retry — applied live",
    "restart": "Restart required (read-only)",
    "info": "Informational (read-only)",
}

_GROUP_ORDER = ("routing", "failover", "restart", "info")


class ConfigurationScreen(Screen):
    """
    Tab 5. A scrolling form grouped by how each setting takes effect.
    Save runs the write -> validate -> apply -> confirm flow and surfaces
    the (redacted) reload report in the status line.
    """

    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("r", "refresh_config", "Refresh"),
    ]

    def __init__(self, facade: ServiceFacade) -> None:
        super().__init__()
        self._facade = facade
        self._fields: list[ConfigField] = facade.config_form()

    # ------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Configuration", classes="screen-title")
        with VerticalScroll(id="config-root"):
            for group in _GROUP_ORDER:
                yield Static(_GROUP_TITLES[group], classes="config-group")

                for field in self._fields:
                    if field.group == group:
                        yield self._field_widget(field)

            yield Static("", id="config-restart-note", classes="config-note")
            with Horizontal(id="config-controls"):
                yield Button("Save", id="config-save")
                yield Button("Revert", id="config-revert")
                yield Button("Refresh", id="config-refresh")
        yield Static("", id="config-status")
        yield Footer()

    def _field_widget(self, field: ConfigField):
        widget_id = f"cfg-{field.env}"

        if field.kind == "bool":
            return Checkbox(
                field.label,
                value=field.value == "true",
                id=widget_id,
                disabled=not field.editable,
            )

        return Input(
            value=field.value,
            placeholder=field.hint,
            id=widget_id,
            disabled=not field.editable,
        )

    def on_mount(self) -> None:
        self._refresh_values()

    # ------------------------------------------------------------- helpers

    def _status(self) -> Static:
        return self.query_one("#config-status", Static)

    def _set_status(self, text: str) -> None:
        self._status().update(text)

    def _set_busy(self, busy: bool) -> None:
        for widget_id in ("config-save", "config-revert", "config-refresh"):
            self.query_one(f"#{widget_id}", Button).disabled = busy

    def _refresh_values(self) -> None:
        self._fields = self._facade.config_form()

        for field in self._fields:
            try:
                widget = self.query_one(f"#cfg-{field.env}")
            except Exception:
                continue

            if field.kind == "bool":
                widget.value = field.value == "true"
            else:
                widget.value = field.value

        restart = self._facade.config_restart_required_fields()
        self.query_one("#config-restart-note", Static).update(
            "Restart required to change: " + ", ".join(restart)
        )

    def _collect_changes(self) -> dict[str, str]:
        changes: dict[str, str] = {}

        for field in self._fields:
            if not field.editable:
                continue

            widget = self.query_one(f"#cfg-{field.env}")

            if field.kind == "bool":
                value = "true" if widget.value else "false"
            else:
                value = widget.value.strip()

            if value != field.value:
                changes[field.env] = value

        return changes

    def _show_report(self, report: dict) -> None:
        if report.get("saved"):
            applied = len(report.get("applied") or [])
            unchanged = len(report.get("unchanged") or [])
            failures = report.get("failures") or []
            text = (
                f"Configuration saved and applied "
                f"({applied} field(s) reloaded, {unchanged} unchanged)."
            )
            if failures:
                text += f" Non-fatal failures: {failures}"
            self._set_status(text)
        else:
            self._set_status(
                "Save failed; previous configuration restored. "
                f"{report.get('error') or report.get('failures') or 'unknown error'}"
            )

    # ------------------------------------------------------------ handlers

    @on(Button.Pressed, "#config-save")
    async def _on_save(self) -> None:
        changes = self._collect_changes()

        if not changes:
            self._set_status("No changes to save.")
            return

        self._set_status("Saving configuration\u2026")
        self._set_busy(True)
        try:
            report = await asyncio.to_thread(self._facade.save_config, changes)
        except Exception as exc:  # noqa: BLE001 - surface in the status line
            self._set_status(f"Failed to save configuration: {exc}")
        else:
            self._show_report(report)
        finally:
            self._set_busy(False)
            self._refresh_values()

    @on(Button.Pressed, "#config-revert")
    def _on_revert(self) -> None:
        self._refresh_values()
        self._set_status("Discarded unsaved edits.")

    @on(Button.Pressed, "#config-refresh")
    def _on_refresh(self) -> None:
        self._refresh_values()
        self._set_status("Configuration refreshed.")

    async def action_save(self) -> None:
        await self._on_save()

    def action_refresh_config(self) -> None:
        self._refresh_values()
