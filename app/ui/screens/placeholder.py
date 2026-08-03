"""Placeholder screen for tabs that land in later P2 phases."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Middle
from textual.screen import Screen
from textual.widgets import Footer, Header, Static


class PlaceholderScreen(Screen):
    """
    Interim body for Models, Providers, Configuration, Applications, and
    Diagnostics tabs. Replaced in P2c–P2e; deliberately free of any
    unimplemented behaviour so no partially-finished feature leaks into
    the TUI.
    """

    def __init__(self, title: str, note: str = "") -> None:
        super().__init__()
        self._title = title
        self._note = note

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Center():
            with Middle():
                yield Static(
                    f"[bold]{self._title}[/bold]",
                    classes="placeholder-title",
                )
                yield Static(
                    self._note or "This panel ships in a later P2 phase.",
                    classes="placeholder-note",
                )
        yield Footer()
