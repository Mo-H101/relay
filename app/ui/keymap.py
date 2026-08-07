"""
Global keymap constants for the Relay TUI (P9 seam).

Tab digits and the quit key are shared here. Screen-specific shortcuts
(such as ``r`` refresh, ``m`` model mode, ``ctrl+s`` save) are declared
inline in each screen's ``BINDINGS``, and the app-level tab-switching,
quitting, and escape bindings live in ``RelayApp.BINDINGS`` — so changing
a shortcut means editing that screen's binding list, not this module.
"""

from __future__ import annotations

# Tab navigation
TAB_DASHBOARD = "1"
TAB_CHAT = "2"
TAB_MODELS = "3"
TAB_PROVIDERS = "4"
TAB_CONFIGURATION = "5"
TAB_APPLICATIONS = "6"
TAB_DIAGNOSTICS = "7"

# Global actions
QUIT = "q"
