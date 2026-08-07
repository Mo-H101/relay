"""
Interactive UI abstraction for the setup wizard.

The wizard talks to a ``UI`` protocol so real users drive ``TerminalUI``
while tests drive every branch through ``ScriptedUI``. ``TerminalUI`` wraps
``input()`` (preserving the CLI's existing EOFError/KeyboardInterrupt
handling) and picks a progress reporter by TTY-ness. ``ScriptedUI`` pops
the next canned answer and raises loudly when the script is exhausted, so a
test that under-scripts a branch fails instead of hanging.
"""

import os
import sys
from typing import List, Protocol

from app.setup.reporting import (
    PlainProgressReporter,
    ProgressReporter,
    RecordingReporter,
    RichProgressReporter,
)


class UI(Protocol):
    """The interaction surface the wizard is allowed to touch."""

    def notice(self, text: str) -> None: ...

    def ask(self, prompt: str, default: str | None = None) -> str: ...

    def ask_yes_no(self, prompt: str, default: bool) -> bool: ...

    def menu(self, options: List[str], prompt: str) -> int | None: ...

    def confirm(self, prompt: str, default: bool) -> bool: ...

    def retry_or_skip(self, prompt: str) -> str: ...

    def progress(self) -> ProgressReporter: ...


def _read(prompt: str) -> str:
    """
    Read one line from stdin. EOF/KeyboardInterrupt exits cleanly, matching
    the pre-wizard ``app/cli.py`` behavior.
    """
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


class TerminalUI:
    """
    Real interactive UI. Prompts go to stdout via ``input()``; Rich is used
    only for the per-scan progress bar (and never on a non-TTY).
    """

    def __init__(self) -> None:
        self._console = None

    def notice(self, text: str) -> None:
        print(text)

    def ask(self, prompt: str, default: str | None = None) -> str:
        suffix = f" [{default}]" if default is not None else ""
        value = _read(f"{prompt}{suffix}: ")
        if not value and default is not None:
            return default
        return value

    def ask_yes_no(self, prompt: str, default: bool) -> bool:
        value = self.ask(
            f"{prompt} (y/n)",
            "y" if default else "n",
        ).lower()

        if value not in ("y", "n"):
            print("  Please answer 'y' or 'n'.")
            return self.ask_yes_no(prompt, default)

        return value == "y"

    def menu(self, options: List[str], prompt: str) -> int | None:
        while True:
            print(prompt)
            for index, option in enumerate(options, start=1):
                print(f"  [{index}] {option}")
            raw = _read("Enter a number (blank to cancel): ")

            if not raw:
                return None

            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return int(raw)

            print(
                f"  Please enter a number between 1 and {len(options)}, "
                "or press Enter to cancel."
            )

    def confirm(self, prompt: str, default: bool) -> bool:
        return self.ask_yes_no(prompt, default)

    def retry_or_skip(self, prompt: str) -> str:
        while True:
            raw = _read(f"{prompt}: ").lower()

            if raw.startswith("r"):
                return "r"
            if raw.startswith("s"):
                return "s"

            print("  Choose [R]etry or [S]kip.")

    def progress(self) -> ProgressReporter:
        if sys.stdout.isatty() and os.getenv("SETUP_NO_PROGRESS", "") != "1":
            return RichProgressReporter()
        return PlainProgressReporter()


class ScriptedUI:
    """
    Test UI: every interaction pops the next canned answer. A missing
    answer raises so a test with an incomplete script fails loudly.
    """

    def __init__(self, script, recorder: RecordingReporter | None = None):
        self._script = list(script)
        self.recorder = recorder or RecordingReporter()
        self.notices: List[str] = []
        self.answers: List = []

    def _next(self, kind: str):
        if not self._script:
            raise RuntimeError(
                f"ScriptedUI script exhausted while answering {kind}."
            )
        value = self._script.pop(0)
        self.answers.append(value)
        return value

    def notice(self, text: str) -> None:
        self.notices.append(text)

    def ask(self, prompt: str, default: str | None = None) -> str:
        return str(self._next("ask"))

    def ask_yes_no(self, prompt: str, default: bool) -> bool:
        value = self._next("yes_no")
        if isinstance(value, bool):
            return value
        return str(value).strip().lower().startswith("y")

    def menu(self, options: List[str], prompt: str) -> int | None:
        value = self._next("menu")
        if value is None:
            return None
        return int(value)

    def confirm(self, prompt: str, default: bool) -> bool:
        value = self._next("confirm")
        if isinstance(value, bool):
            return value
        return str(value).strip().lower().startswith("y")

    def retry_or_skip(self, prompt: str) -> str:
        return str(self._next("retry_or_skip")).strip().lower()[:1]

    def progress(self) -> ProgressReporter:
        return self.recorder
