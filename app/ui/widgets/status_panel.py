"""Reusable dashboard widgets for the Relay TUI."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static


class StatTile(Vertical):
    """
    A labelled dashboard tile: a muted caption over a prominent value,
    with an optional trailing status dot colour.
    """

    def __init__(
        self,
        label: str,
        value: str = "-",
        *,
        color: str = "white",
        id: str | None = None,  # noqa: A002
    ) -> None:
        super().__init__(id=id)
        self._label = label
        self._value = value
        self._color = color

    def compose(self) -> ComposeResult:
        yield Static(self._label, classes="stat-label")
        yield Static(Text(self._value, style=self._color), classes="stat-value")

    def update_value(self, value: str, color: str | None = None) -> None:
        self._value = value
        if color is not None:
            self._color = color
        value_widget = self.query_one(".stat-value", Static)
        value_widget.update(Text(self._value, style=self._color))
