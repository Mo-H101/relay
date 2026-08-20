"""
TUI design system tokens.

Defines the complete visual language for the Relay TUI: ~25 semantic
color tokens, spacing scale, typography hierarchy, border vocabulary,
and icon/status mappings. Every visual decision flows through these
tokens so screens stay free of hardcoded magic values.

The module exposes a ``theme`` namespace object for Python-side access
(e.g. ``theme.accent``) and individual constants for CSS f-string
interpolation (e.g. ``{accent}``).

CSS files in ``app/ui/styles/`` use Textual ``$variable`` syntax
(e.g. ``$accent``, ``$surface``) which maps to these values at runtime.
"""

from __future__ import annotations


# ────────────────────────────────────────────────────── Semantic Colors
#
# ~25 tokens organized by category. Each token has a single, clear
# purpose. The palette is deep, layered, and high-contrast where it
# matters — inspired by the best terminal UIs (lazygit, btop, yazi, k9s)
# without copying their branding.

# ── Backgrounds & Surfaces
background = "#0d1117"
surface = "#161b22"
surface_raised = "#1c2128"
surface_sunken = "#010409"

# ── Borders & Separators
border = "#30363d"
border_focus = "#58a6ff"
border_subtle = "#21262d"

# ── Primary Accent
accent = "#58a6ff"

# ── Semantic Status Colors
ok = "#3fb950"
warn = "#d29922"
error = "#f85149"
info = "#58a6ff"

# ── Text Hierarchy (title → heading → body → metadata → muted)
text = "#e6edf3"
text_bright = "#ffffff"
text_dim = "#c9d1d9"
text_muted = "#8b949e"
text_subtle = "#6e7681"
text_disabled = "#484f58"


# ──────────────────────────────────────────────────────── Spacing Scale
#
# Consistent spatial rhythm. All margin/padding values should reference
# these instead of raw numbers.

sp = type("sp", (), dict(
    xs="0",
    sm="1",
    md="2",
    lg="3",
    xl="4",
))()


# ─────────────────────────────────────────────────── Typography Hierarchy
#
# Typographic hierarchy via Rich markup. Apply these as style strings
# in Rich ``Text`` objects or Textual ``Static.update()`` calls.
# Level purpose:
#   heading   — screen/section titles
#   subheading — subsection labels
#   body      — default text
#   caption   — secondary info
#   muted     — tertiary, de-emphasized

typ = type("typ", (), dict(
    heading="bold",
    subheading="bold",
    body="",
    caption="italic",
    muted="dim",
))()


# ──────────────────────────────────────────────────── Border Vocabulary
#
# Border styles for consistent visual structure.

bdr = type("bdr", (), dict(
    subtle=f"round {border}",
    default=f"tall {border}",
    strong=f"heavy {accent}",
    focus=f"tall {accent}",
))()


# ────────────────────────────────────────────────────── Icon Vocabulary
#
# Unicode icons with ASCII fallbacks for terminals that don't support
# box-drawing or symbol characters.

ico = type("ico", (), dict(
    check="✓",
    cross="✗",
    dash="—",
    bullet="•",
    arrow_up="↑",
    arrow_down="↓",
    chevron_right="›",
    ellipsis="…",
    dot="●",
    diamond="◆",
))()


# ──────────────────────────────────────────────────── Status-to-Icon Map
#
# Semantic status-to-icon mapping. Use these everywhere status is
# displayed so the visual language stays consistent.

status_icon = {
    "enabled": ico.check,
    "disabled": ico.cross,
    "configured": ico.check,
    "not_configured": ico.dash,
    "set": ico.check,
    "missing": ico.cross,
    "running": ico.check,
    "stopped": ico.cross,
}


# ───────────────────────────────────────────── Provider Health-to-Icon Map
#
# Provider/model availability glyphs. These map directly to the
# availability statuses returned by the health store.

health_icon = {
    "healthy": ico.check,
    "degraded": "⚠",
    "unavailable": ico.cross,
    "unsupported": "?",
    "unknown": ico.dash,
    "not_checked": ico.dash,
}


# ─────────────────────────────────────────── Status-to-Color Lookup Map
#
# Convenience lookups for status-to-color mapping. Screens use these
# to apply consistent color through Rich markup.

status_color = {
    "healthy": ok,
    "available": ok,
    "degraded": warn,
    "overloaded": warn,
    "unavailable": error,
    "error": error,
}


# ──────────────────────────────────────── Backward-Compatible Namespace
#
# The ``theme`` object preserves the original attribute names so existing
# screen code (``theme.accent``, ``theme.ok``, etc.) continues to work
# without modification.

class _Theme:
    """Namespace for Python-side token access."""

    # Backgrounds & surfaces
    background = background
    surface = surface
    surface_raised = surface_raised
    surface_sunken = surface_sunken

    # Borders
    border = border
    border_focus = border_focus
    border_subtle = border_subtle
    panel_border = border  # backward compat

    # Accent
    accent = accent

    # Status colors
    ok = ok
    warn = warn
    error = error
    info = info

    # Text hierarchy
    text = text
    text_bright = text_bright
    text_dim = text_dim
    text_muted = text_muted
    text_subtle = text_subtle
    text_disabled = text_disabled

    # Backward compat aliases
    muted = text_muted


theme = _Theme()
