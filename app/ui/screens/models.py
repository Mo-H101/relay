"""
P2c Models screen (tab 3) — Stage C redesign.

On-demand union of every provider's models with a ``✓/⚠/✗`` availability
glyph merged from the health store and the setup availability snapshot.
Priority reordering is restricted to available models (the P1 rule) and is
persisted through ``config_store`` + applied with ``reload_config(relay)``.
Provider enable/disable (where supported) is also reachable here.

Stage C changes:
- Rank number column (1, 2, 3...) in first column
- Visual separator between available and unavailable groups
- Color-coded rows by status (green=healthy, yellow=degraded, red=unavailable)
"""

from __future__ import annotations

import asyncio

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Select, Static

from app.ui.data import ModelInfo, ServiceFacade, candidate_glyph
from app.ui.theme import theme


class ModelsScreen(Screen):
    """
    Tab 3. Table rows carry a ``(provider, model)`` key; the priority
    controls reorder available models within the selected row's provider.
    """

    BINDINGS = [
        Binding("r", "refresh_models", "Refresh"),
        Binding("ctrl+up", "priority_up", "Priority up"),
        Binding("ctrl+down", "priority_down", "Priority down"),
    ]

    def __init__(self, facade: ServiceFacade) -> None:
        super().__init__()
        self._facade = facade
        self._selected: tuple[str, str] | None = None
        self._priority: dict[str, list[str]] = {}
        self._available: dict[str, set[str]] = {}
        self._busy = False

    # ------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Models", classes="screen-title")
        with Vertical(id="models-root"):
            yield DataTable(id="models-table", cursor_type="row")
            with Horizontal(id="models-controls"):
                yield Button("Move up", id="priority-up")
                yield Button("Move down", id="priority-down")
                yield Button("Apply priority", id="priority-apply")
                yield Button("Refresh", id="models-refresh")
                yield Select([], id="provider-toggle")
                yield Button("Toggle provider", id="provider-toggle-btn")
            yield Static("", id="models-status")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_all()

    def on_screen_resume(self) -> None:
        self._refresh_all()

    # ------------------------------------------------------------- helpers

    def _table(self) -> DataTable:
        return self.query_one("#models-table", DataTable)

    def _status(self) -> Static:
        return self.query_one("#models-status", Static)

    def _provider_select(self) -> Select:
        return self.query_one("#provider-toggle", Select)

    def _set_status(self, text: str) -> None:
        self._status().update(text)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for widget_id in (
            "priority-up",
            "priority-down",
            "priority-apply",
            "models-refresh",
            "provider-toggle-btn",
        ):
            self.query_one(f"#{widget_id}", Button).disabled = busy

    def _refresh_all(self) -> None:
        self._refresh_priority_state()
        self._refresh_models()
        self._refresh_provider_select()

    def _refresh_priority_state(self) -> None:
        models = self._facade.models()
        providers = [provider.name for provider in self._facade.providers()]

        available: dict[str, set[str]] = {}
        for info in models:
            if info.status != "unavailable":
                available.setdefault(info.provider, set()).add(info.name)

        self._available = available
        self._priority = {
            provider: self._facade.model_priority(provider)
            for provider in providers
        }

    def _ordered_rows(self) -> list[ModelInfo]:
        """
        Provider order from the runtime manager; within a provider the
        working priority order first (available models), unavailable last.
        """
        models = self._facade.models()
        by_provider: dict[str, list[ModelInfo]] = {}
        for info in models:
            by_provider.setdefault(info.provider, []).append(info)

        rows: list[ModelInfo] = []
        for provider_name in [p.name for p in self._facade.providers()]:
            infos = by_provider.get(provider_name, [])
            avail = [
                info
                for info in infos
                if info.name in self._available.get(provider_name, set())
            ]
            rest = [
                info
                for info in infos
                if info.name not in self._available.get(provider_name, set())
            ]
            priority = self._priority.get(provider_name, [])
            avail.sort(
                key=lambda info: (
                    priority.index(info.name) if info.name in priority else len(priority)
                )
            )
            rows.extend(avail)
            rows.extend(rest)
        return rows

    @staticmethod
    def _status_color(status: str) -> str:
        """Return Rich color string for a model status."""
        if status in ("healthy", "available"):
            return theme.ok
        if status in ("degraded", "overloaded"):
            return theme.warn
        if status in ("unavailable", "error"):
            return theme.error
        return theme.text_muted

    def _refresh_models(self) -> None:
        table = self._table()
        table.clear(columns=True)
        table.add_columns("#", "Provider", "Model", "Status", "Latency")

        rows = self._ordered_rows()

        rank = 0
        separator_inserted = False

        for info in rows:
            # Insert separator between available and unavailable groups
            if info.status == "unavailable" and not separator_inserted:
                separator_inserted = True
                table.add_row(
                    "—",
                    "—",
                    f"── unavailable ──",
                    "—",
                    "—",
                    key=("__separator__", "__separator__"),
                )

            if info.status != "unavailable":
                rank += 1
                rank_str = str(rank)
            else:
                rank_str = ""

            latency = f"{info.latency_ms}ms" if info.latency_ms else ""
            glyph = candidate_glyph(info.status)
            status_color = self._status_color(info.status)

            # Build colored status text with glyph
            status_markup = f"[{status_color}]{glyph} {info.status}[/]"

            table.add_row(
                rank_str,
                info.provider,
                info.name,
                status_markup,
                latency,
                key=(info.provider, info.name),
            )

    def _refresh_provider_select(self) -> None:
        options = [
            (
                f"{'✓' if entry.enabled else '✗'} {entry.display_name}"
                if entry.configured
                else f"- {entry.display_name}",
                entry.id,
            )
            for entry in self._facade.provider_catalog()
            if entry.configured
        ]
        select = self._provider_select()
        select.set_options(options)
        if options:
            select.value = options[0][1]

    # ------------------------------------------------------------ handlers

    @on(DataTable.RowHighlighted, "#models-table")
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value
        self._selected = key if isinstance(key, tuple) else None

    @on(Button.Pressed, "#priority-up")
    async def _on_priority_up(self) -> None:
        await self._move_priority(-1)

    @on(Button.Pressed, "#priority-down")
    async def _on_priority_down(self) -> None:
        await self._move_priority(1)

    @on(Button.Pressed, "#priority-apply")
    async def _on_priority_apply(self) -> None:
        await self._apply_priority()

    @on(Button.Pressed, "#models-refresh")
    def _on_models_refresh(self) -> None:
        self._refresh_all()

    @on(Button.Pressed, "#provider-toggle-btn")
    async def _on_provider_toggle(self) -> None:
        await self._toggle_provider()

    # ------------------------------------------------------------- actions

    def action_refresh_models(self) -> None:
        self._refresh_all()

    async def action_priority_up(self) -> None:
        await self._move_priority(-1)

    async def action_priority_down(self) -> None:
        await self._move_priority(1)

    async def _move_priority(self, direction: int) -> None:
        if self._selected is None:
            self._set_status("Select a model row first.")
            return

        provider, model = self._selected
        available = self._available.get(provider, set())

        if model not in available:
            self._set_status(
                f"{model} is not available; priority is restricted "
                "to available models."
            )
            return

        priority = self._priority.setdefault(
            provider, self._facade.model_priority(provider)
        )

        if model not in priority:
            priority.append(model)

        index = priority.index(model)
        target = index + direction

        if target < 0 or target >= len(priority):
            self._set_status("Already at the edge of the priority order.")
            return

        priority[index], priority[target] = priority[target], priority[index]
        self._refresh_models()
        self._set_status(
            f"Priority for {provider}: {', '.join(priority)}"
        )

    async def _apply_priority(self) -> None:
        if self._selected is None:
            self._set_status("Select a model row first.")
            return

        provider, _ = self._selected
        defn_id = self._facade.provider_defn_id(provider)

        if defn_id is None:
            self._set_status(f"No registry definition for '{provider}'.")
            return

        priority = list(
            self._priority.get(provider, self._facade.model_priority(provider))
        )
        self._set_busy(True)
        self._set_status(f"Saving priority for {provider}\u2026")
        try:
            report = await asyncio.to_thread(
                self._facade.set_model_priority, defn_id, priority
            )
        except Exception as exc:  # noqa: BLE001 - surface in the status line
            self._set_status(f"Failed to save priority: {exc}")
        else:
            self._show_reload(report, f"{provider} priority")
        finally:
            self._set_busy(False)
            self._refresh_all()

    async def _toggle_provider(self) -> None:
        defn_id = self._provider_select().value

        if not defn_id:
            self._set_status("No configured provider selected.")
            return

        entry = next(
            (entry for entry in self._facade.provider_catalog() if entry.id == defn_id),
            None,
        )

        if entry is None:
            self._set_status("Unknown provider.")
            return

        target = not entry.enabled
        self._set_busy(True)
        self._set_status(
            f"Setting {entry.display_name} {'enabled' if target else 'disabled'}\u2026"
        )
        try:
            report = await asyncio.to_thread(
                self._facade.set_provider_enabled, defn_id, target
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
            self._refresh_all()

    def _show_reload(self, report: dict, label: str) -> None:
        if report.get("reloaded"):
            applied = report.get("applied", [])
            self._set_status(
                f"{label} saved and applied "
                f"(reloaded {len(applied)} field(s))."
            )
        else:
            detail = report.get("error") or report.get("failures") or "unknown"
            self._set_status(f"{label} saved; reload failed: {detail}")
