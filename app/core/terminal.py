"""
Interactive-terminal preflight for the Textual TUI.

Textual renders only in a real interactive terminal. On POSIX an
``isatty()`` check on stdin/stdout is sufficient. On Windows the standard
output must also be backed by a real console handle — Windows Terminal or
VS Code (ConPTY) or a conhost console — otherwise Textual would emit
garbage or fail entirely.

This module is deliberately UI-free and import-safe in headless/CLI
contexts, so ``app.cli`` can guard the TUI entry point without importing
Textual (the ``test_core_and_cli_import_without_textual_in_runtime``
boundary test keeps it that way).
"""

from __future__ import annotations

import os
import sys

STD_OUTPUT_HANDLE = -11


def is_windows() -> bool:
    """True when running on Windows (nt)."""
    return os.name == "nt"


def _stdin_interactive() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except Exception:  # noqa: BLE001 - a closed/odd stream is not interactive
        return False


def _stdout_interactive() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except Exception:  # noqa: BLE001 - a closed/odd stream is not interactive
        return False


def _windows_console_available() -> bool:
    """
    True when the standard output handle is a real Windows console
    (ConPTY or conhost). Redirected pipes/files and non-console contexts
    (pythonw, services, scheduled tasks) make ``GetConsoleMode`` fail.
    """
    if not is_windows():
        return True

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetConsoleMode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
        ]

        handle = kernel32.GetStdHandle(wintypes.DWORD(STD_OUTPUT_HANDLE))

        if not handle or handle == ctypes.c_void_p(-1).value:
            return False

        mode = wintypes.DWORD()
        return bool(kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    except Exception:  # noqa: BLE001 - be conservative and refuse the TUI
        return False


def tui_ready() -> tuple[bool, str]:
    """
    ``(available, reason)`` describing whether the Textual TUI can run in
    the current environment. ``reason`` is ``""`` when ``available`` is
    True.
    """
    if not _stdin_interactive():
        return False, "standard input is not an interactive terminal"

    if not _stdout_interactive():
        return False, "standard output is not an interactive terminal"

    if is_windows():
        in_conpty_terminal = bool(
            os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM")
        )

        if not in_conpty_terminal and not _windows_console_available():
            return False, "no Windows console (ConPTY/conhost) is attached"

    return True, ""


def tui_guidance(reason: str) -> str:
    """
    Clear guidance for a non-interactive environment: how to run the TUI
    and how to run Relay headless instead.
    """
    return (
        "Relay's terminal interface needs an interactive terminal.\n"
        f"Reason: {reason}.\n\n"
        "  - Run 'relay' (or 'relay tui') from a real terminal, such as\n"
        "    Windows Terminal, PowerShell, or a POSIX shell.\n"
        "  - To run Relay without a UI, use 'relay serve' to start only\n"
        "    the API server.\n"
    )


def print_tui_guidance(reason: str) -> None:
    """Print the non-interactive guidance block."""
    print(tui_guidance(reason))
