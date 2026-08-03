"""
TUI theme palette (P9 seam).

Kept deliberately small and free of any mascot/personality content: this
exists so later phases can swap colors/fonts without touching widget
code. Screens import colors from here instead of hardcoding them.

Values are hex so they stay valid in both Textual CSS and Rich styles
(Rich only accepts a handful of named colors; hex works everywhere).
"""

from __future__ import annotations


class Theme:
    accent = "#00bfff"
    ok = "#00cc00"
    warn = "#ffcc00"
    error = "#ff5555"
    muted = "#808080"
    panel_border = "#808080"
    background = "#000000"
    surface = "#262626"
    text = "#ffffff"
    text_bright = "#ffffff"


theme = Theme()
