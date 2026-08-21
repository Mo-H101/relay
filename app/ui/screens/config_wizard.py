"""
Configuration wizard — guided step-by-step setup flow (Stage F).

A ModalScreen overlay that walks new users through essential configuration
using the same ``ServiceFacade.save_config()`` path as the normal
Configuration screen. Each step shows a focused subset of fields; changes
are accumulated across steps and saved atomically on completion.

The wizard is a UI skin over the existing configuration machinery — no
domain logic is duplicated. Cancel/Back never lose already-saved
configuration; only the current session's edits are discarded.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    ContentSwitcher,
    Input,
    Static,
)

if TYPE_CHECKING:
    from app.ui.data import ConfigField, ServiceFacade


WIZARD_STEP_IDS = ("welcome", "server", "providers", "routing", "summary")
WIZARD_STEP_TITLES = (
    "Welcome to Relay",
    "Server Basics",
    "Provider Setup",
    "Task Routing",
    "Review & Save",
)

WIZARD_STEP_DESCS = {
    "welcome": (
        "Relay is an AI gateway that routes requests to the best "
        "provider for each task. Let's configure the essentials."
    ),
    "server": (
        "Configure the relay host, port, and request timeouts. "
        "These define how clients connect to your gateway."
    ),
    "providers": (
        "Enable or disable AI providers. API keys are managed "
        "separately on the Providers screen (tab 4)."
    ),
    "routing": (
        "Control how Relay routes requests to the best provider "
        "for each task type (coding, vision, reasoning, etc.)."
    ),
    "summary": "Review your changes below, then press Finish to save.",
}


class ConfigWizardScreen(ModalScreen[None]):
    """
    Guided step-by-step configuration wizard.

    Each step shows a focused subset of configuration fields. Changes are
    accumulated across steps and saved atomically via
    ``ServiceFacade.save_config()`` when the user presses Finish. The
    wizard does not duplicate any domain logic — it reads fields from the
    same ``ConfigField`` model and writes through the same save path.
    """

    CSS = """
    ConfigWizardScreen {
        align: center middle;
    }

    #wizard-container {
        width: 80;
        max-width: 90%;
        height: 28;
        max-height: 85%;
        background: #161b22;
        border: tall #30363d;
        padding: 1 0;
    }

    #wizard-body {
        height: 1fr;
    }

    #wizard-body VerticalScroll {
        height: 1fr;
        padding: 0 1;
    }

    #wizard-welcome-text {
        padding: 0 1;
        color: #c9d1d9;
        height: auto;
        margin-bottom: 1;
    }

    .wizard-field {
        margin-bottom: 1;
    }

    .wizard-row {
        height: auto;
    }

    .wizard-label {
        width: 32;
        color: #e6edf3;
        padding: 0 1 0 0;
    }

    .wizard-hint {
        color: #8b949e;
        height: 1;
    }

    .wizard-secret {
        color: #d29922;
    }

    .wizard-summary-value {
        color: #58a6ff;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
    ]

    def __init__(self, facade: ServiceFacade) -> None:
        super().__init__()
        self._facade = facade
        self._step_index = 0
        self._changes: dict[str, str] = {}

    def _is_first_step(self) -> bool:
        return self._step_index == 0

    def _is_last_step(self) -> bool:
        return self._step_index == len(WIZARD_STEP_IDS) - 1

    # ------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        with Vertical(id="wizard-container"):
            yield Static("Configuration Wizard", id="wizard-title")
            yield Static(self._step_indicator(), id="wizard-steps")
            yield Static(self._step_title_text(), id="wizard-step-title")
            yield Static(self._step_desc_text(), id="wizard-step-desc")
            with VerticalScroll(id="wizard-body"):
                with ContentSwitcher(
                    id="wizard-pages", initial="wizard-page-welcome",
                ):
                    yield from self._build_all_pages()
            yield Static("", id="wizard-status")
            with Horizontal(id="wizard-nav"):
                cancel = Button("Cancel", id="wizard-cancel")
                back = Button("Back", id="wizard-back", disabled=True)
                nxt = Button("Next", id="wizard-next", variant="primary")
                for nav_button in (cancel, back, nxt):
                    nav_button.active_effect_duration = 0.0
                yield cancel
                yield back
                yield nxt

    def _step_indicator(self) -> str:
        parts: list[str] = []
        for i, title in enumerate(WIZARD_STEP_TITLES):
            if i < self._step_index:
                parts.append(f"✓ {title}")
            elif i == self._step_index:
                parts.append(f"▸ {title}")
            else:
                parts.append(f"  {title}")
        return "  ·  ".join(parts)

    def _step_title_text(self) -> str:
        return WIZARD_STEP_TITLES[self._step_index]

    def _step_desc_text(self) -> str:
        return WIZARD_STEP_DESCS[WIZARD_STEP_IDS[self._step_index]]

    def _build_all_pages(self) -> ComposeResult:
        for i, step_id in enumerate(WIZARD_STEP_IDS):
            with VerticalScroll(id=f"wizard-page-{step_id}"):
                yield from self._page_content(i)

    def _page_content(self, step_index: int) -> ComposeResult:
        step_id = WIZARD_STEP_IDS[step_index]

        if step_id == "welcome":
            yield Static(
                "This wizard will walk you through the essential "
                "configuration for your Relay AI gateway. You can skip "
                "any step or cancel at any time — no changes are saved "
                "until you finish.",
                classes="wizard-welcome-text",
            )
            yield Static(
                "Use Next/Back to navigate steps. Press Escape to cancel.",
                classes="wizard-welcome-text",
            )
            return

        if step_id == "summary":
            yield Static(
                "No changes yet. Use Back to modify settings.",
                id="wizard-summary-content",
            )
            return

        fields = self._facade.config_wizard_fields(step_id)
        if not fields:
            yield Static(
                "No configurable fields.", classes="wizard-welcome-text",
            )
            return

        for field in fields:
            widget_id = f"wiz-{field.env or field.attr}"
            with Vertical(classes="wizard-field"):
                with Horizontal(classes="wizard-row"):
                    if field.secret:
                        yield Static(field.label, classes="wizard-label")
                        yield Static(
                            field.value,
                            id=widget_id,
                            classes="wizard-secret",
                        )
                    elif field.kind == "bool":
                        yield Checkbox(
                            field.label,
                            value=field.value == "true",
                            id=widget_id,
                            disabled=not field.editable,
                        )
                    else:
                        yield Static(field.label, classes="wizard-label")
                        yield Input(
                            value=field.value,
                            placeholder=field.hint,
                            id=widget_id,
                            disabled=not field.editable,
                        )
                yield Static(field.hint, classes="wizard-hint")

    # --------------------------------------------------------- navigation

    def _collect_step_changes(self) -> None:
        step_id = WIZARD_STEP_IDS[self._step_index]
        if step_id in ("welcome", "summary"):
            return

        fields = self._facade.config_wizard_fields(step_id)
        for field in fields:
            if not field.editable or field.secret:
                continue
            widget_id = f"wiz-{field.env or field.attr}"
            try:
                widget = self.query_one(f"#{widget_id}")
            except Exception:
                continue
            if field.kind == "bool":
                value = "true" if widget.value else "false"
            else:
                value = widget.value.strip()
            if value != field.value:
                self._changes[field.env] = value
            elif field.env in self._changes:
                del self._changes[field.env]

    def _update_nav(self) -> None:
        self.query_one("#wizard-back", Button).disabled = self._is_first_step()

        next_btn = self.query_one("#wizard-next", Button)
        if self._is_last_step():
            next_btn.label = "Finish"
            next_btn.variant = "primary"
        else:
            next_btn.label = "Next"
            next_btn.variant = "primary"

        self.query_one("#wizard-steps", Static).update(self._step_indicator())
        self.query_one("#wizard-step-title", Static).update(
            self._step_title_text()
        )
        self.query_one("#wizard-step-desc", Static).update(
            self._step_desc_text()
        )

        page_id = f"wizard-page-{WIZARD_STEP_IDS[self._step_index]}"
        self.query_one("#wizard-pages", ContentSwitcher).current = page_id

        if WIZARD_STEP_IDS[self._step_index] == "summary":
            self._update_summary()

    def _update_summary(self) -> None:
        try:
            widget = self.query_one("#wizard-summary-content", Static)
        except Exception:
            return
        if not self._changes:
            widget.update("No changes yet. Use Back to modify settings.")
            return
        lines = [f"  {env} = {val}" for env, val in sorted(self._changes.items())]
        widget.update("\n".join(lines))

    # ------------------------------------------------------------ handlers

    @on(Button.Pressed, "#wizard-cancel")
    def _on_cancel(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#wizard-back")
    def _on_back(self) -> None:
        if self._step_index > 0:
            self._collect_step_changes()
            self._step_index -= 1
            self._update_nav()

    @on(Button.Pressed, "#wizard-next")
    def _on_next(self) -> None:
        self._collect_step_changes()
        if self._is_last_step():
            self.app.run_worker(self._do_finish())
            return
        self._step_index += 1
        self._update_nav()

    async def _do_finish(self) -> None:
        if not self._changes:
            self.app.pop_screen()
            return

        self._set_status("Saving\u2026")
        self._set_busy(True)
        try:
            report = await asyncio.to_thread(
                self._facade.save_config, self._changes,
            )
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Failed: {exc}")
            self._set_busy(False)
            return

        if report.get("saved"):
            self._changes.clear()
            self.app.pop_screen()
        else:
            error = report.get("error") or report.get("failures") or "unknown"
            self._set_status(f"Save failed: {error}")
            self._set_busy(False)

    # ------------------------------------------------------------- helpers

    def _set_status(self, text: str) -> None:
        try:
            self.query_one("#wizard-status", Static).update(text)
        except Exception:
            pass

    def _set_busy(self, busy: bool) -> None:
        for btn_id in ("wizard-cancel", "wizard-back", "wizard-next"):
            try:
                self.query_one(f"#{btn_id}", Button).disabled = busy
            except Exception:
                pass
