"""
P2d Configuration screen (tab 5).

The form is derived entirely from ``app.core.config_spec`` through the
facade (P7.3): every setting renders under its stable display group, live
fields edit in place, restart-required fields are editable but never
live-applied (a restart notice is shown), and secret fields render as
masked read-only rows — raw key material never enters a widget. The save
flow goes through ``ServiceFacade.save_config``, which validates each
change with a dry run through the P7.2 mutation layer (zero writes on
refusal), persists through the single writer, applies live fields with the
full reload engine, and restores the previous values on failure.
"""

from __future__ import annotations

import asyncio

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Footer, Header, Input, Static

from app.ui.data import ConfigField, ServiceFacade


class ConfigurationScreen(Screen):
    """
    Tab 5. A scrolling form grouped by display group (Runtime, Network,
    Providers, Security, Storage, Logging, UI) in stable order. Save runs
    the validate -> write -> apply -> confirm flow and surfaces the
    (redacted) reload report in the status line.
    """

    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("r", "refresh_config", "Refresh"),
    ]

    def __init__(self, facade: ServiceFacade) -> None:
        super().__init__()
        self._facade = facade
        self._groups = facade.config_groups()
        self._fields: list[ConfigField] = facade.config_form()

    # ------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Configuration", classes="screen-title")
        with VerticalScroll(id="config-root"):
            for group in self._groups:
                yield Static(group, classes="config-group")

                for field in self._fields:
                    if field.group == group:
                        yield from self._field_widget(field)

            yield Static(
                "", id="config-restart-note", classes="config-note"
            )
            with Horizontal(id="config-controls"):
                yield Button("Save", id="config-save")
                yield Button("Revert", id="config-revert")
                yield Button("Refresh", id="config-refresh")
        yield Static("", id="config-status")
        yield Footer()

    def _widget_id(self, field: ConfigField) -> str:
        return f"cfg-{field.env or field.attr}"

    def _field_widget(self, field: ConfigField):
        widget_id = self._widget_id(field)

        with Vertical(classes="config-field"):
            with Horizontal(classes="config-row"):
                if field.secret:
                    yield Static(field.label, classes="config-label")
                    yield Static(
                        self._secret_value(field),
                        id=widget_id,
                        classes="config-secret",
                    )
                elif field.kind == "bool":
                    yield Checkbox(
                        field.label,
                        value=field.value == "true",
                        id=widget_id,
                        disabled=not field.editable,
                    )
                else:
                    yield Static(field.label, classes="config-label")
                    yield Input(
                        value=field.value,
                        placeholder=field.hint,
                        id=widget_id,
                        disabled=not field.editable,
                    )

            yield Static(self._hint_text(field), classes="config-hint")

    def _secret_value(self, field: ConfigField) -> str:
        if field.value == "(unset)":
            return "(unset) — set on the Providers screen"
        return f"{field.value} — managed on the Providers screen"

    def _hint_text(self, field: ConfigField) -> str:
        hint = field.hint
        if field.restart_required and "restart" not in hint.lower():
            hint = f"{hint} (restart required)"
        return hint

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
                widget = self.query_one(f"#{self._widget_id(field)}")
            except Exception:
                continue

            if field.secret:
                widget.update(self._secret_value(field))
            elif field.kind == "bool":
                widget.value = field.value == "true"
            else:
                widget.value = field.value

        restart = self._facade.config_restart_required_fields()
        self.query_one("#config-restart-note", Static).update(
            "Restart required to take effect: " + ", ".join(restart)
        )

    def _collect_changes(self) -> dict[str, str]:
        changes: dict[str, str] = {}

        for field in self._fields:
            if not field.editable:
                continue

            widget = self.query_one(f"#{self._widget_id(field)}")

            if field.kind == "bool":
                value = "true" if widget.value else "false"
            else:
                value = widget.value.strip()

            if value != field.value:
                changes[field.env] = value

        return changes

    def _show_report(self, report: dict) -> None:
        if report.get("saved"):
            restart = report.get("restart_required") or []
            if report.get("applied"):
                applied = len(report.get("applied") or [])
                unchanged = len(report.get("unchanged") or [])
                text = (
                    f"Configuration saved and applied "
                    f"({applied} field(s) reloaded, {unchanged} unchanged)."
                )
                if restart:
                    text += (
                        " The following need a restart: "
                        + ", ".join(restart)
                        + "."
                    )
            else:
                text = report.get("message") or "Saved."
            failures = report.get("failures") or []
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
