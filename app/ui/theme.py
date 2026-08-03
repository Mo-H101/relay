"""
TUI theme palette (P9 seam).

Kept deliberately small and free of any mascot/personality content: this
exists so later phases can swap colors/fonts without touching widget
code. Screens import colors from here instead of hardcoding them.

Values must be valid in both Textual CSS and Rich markup: use plain CSS
color names for anything referenced from ``[...]`` markup.
"""

from __future__ import annotations


class Theme:
    accent = "deepskyblue"
    ok = "green"
    warn = "yellow"
    error = "red"
    muted = "grey"
    panel_border = "grey"
    background = "black"
    surface = "#262626"
    text = "white"
    text_bright = "white"


theme = Theme()
