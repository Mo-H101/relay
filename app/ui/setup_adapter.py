"""
TUI-backed UI for the setup wizard (P2c).

The wizard is synchronous (``input()``-style prompts) but the TUI is
event-driven, so ``SetupAdapter`` renders each interaction as a modal
``PromptScreen`` pushed onto the app's screen stack. The wizard runs on a
worker thread; each prompt blocks on a ``threading.Event`` until the user
answers in the modal. Scan progress is captured by a ``RecordingReporter``
so no Rich progress bar fights the TUI for the terminal.

API keys entered through the modal are password-masked at the input, and
the adapter never echoes a key back: notices and the Providers screen only
ever see the wizard's ``mask_key`` output or a plain "set/not set" boolean.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, List

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Input, Static

from app.setup.reporting import RecordingReporter
from app.setup.ui import UI
from app.ui.theme import theme


class PromptScreen(ModalScreen[str]):
    """
    Modal hosting one wizard interaction. ``kind`` selects the prompt style:

    * ``"input"`` — free text (API keys are password-masked).
    * ``"yes_no"`` — a ``y``/``n`` answer.
    * ``"menu"`` — a numbered choice from ``options``.
    * ``"retry"`` — an ``r``/``s`` choice.

    On submit the answer is handed to ``resolver`` and the modal is popped;
    Escape resolves with ``None`` (the wizard treats that as cancel/skip).
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    CSS = f"""
    PromptScreen {{
        background: rgba(0, 0, 0, 0.75);
        align: center middle;
    }}

    #prompt-box {{
        width: 64;
        height: auto;
        background: {theme.surface};
        border: round {theme.panel_border};
        padding: 1 2;
    }}

    #prompt-text {{
        color: {theme.text_bright};
        margin-bottom: 1;
    }}

    #prompt-options {{
        height: auto;
        margin-bottom: 1;
    }}

    #prompt-options Static {{
        color: {theme.text};
    }}

    #prompt-input {{
        margin-top: 1;
    }}

    #prompt-ok {{
        margin-top: 1;
        width: 100%;
    }}
    """

    def __init__(
        self,
        kind: str,
        prompt: str,
        options: List[str] | None = None,
        default: str | None = None,
        *,
        resolver: Callable[[Any], None],
    ) -> None:
        super().__init__()
        self._kind = kind
        self._prompt = prompt
        self._options = options or []
        self._default = default
        self._resolver = resolver
        self._secret = kind == "input" and "key" in prompt.lower()

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Static(self._prompt, id="prompt-text")
            if self._kind == "menu":
                with Vertical(id="prompt-options"):
                    for index, option in enumerate(self._options, start=1):
                        yield Static(f"  [{index}] {option}")
            elif self._kind == "retry":
                with Vertical(id="prompt-options"):
                    yield Static("  [R]etry or [S]kip")
            yield Input(
                placeholder="answer\u2026",
                id="prompt-input",
                password=self._secret,
            )
        yield Button("OK", id="prompt-ok")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    def _submit(self) -> None:
        answer = self._answer()
        if answer is None:
            self.query_one("#prompt-input", Input).value = ""
            return
        self._resolver(answer)
        self.app.pop_screen()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "prompt-ok":
            self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def action_cancel(self) -> None:
        self._resolver(None)
        self.app.pop_screen()

    def _answer(self) -> Any | None:
        value = self.query_one("#prompt-input", Input).value.strip()

        if not value and self._default is not None:
            value = self._default

        if self._kind == "yes_no":
            if value.lower() not in ("y", "n"):
                return None
            return value.lower()

        if self._kind == "menu":
            if not value.isdigit():
                return None
            number = int(value)
            if not (1 <= number <= len(self._options)):
                return None
            return number

        if self._kind == "retry":
            return "r" if value.lower().startswith("r") else "s"

        return value


class SetupAdapter:
    """
    ``app.setup.ui.UI`` implementation backed by modal prompts.

    ``screen`` is the screen that hosts the wizard (its app is used to push
    modals and to deliver notices). ``on_notice`` receives wizard status
    lines on the UI thread; by default notices are only accumulated.
    """

    def __init__(
        self,
        screen: Screen,
        on_notice: Callable[[str], None] | None = None,
    ) -> None:
        self._screen = screen
        self._on_notice = on_notice
        self._event = threading.Event()
        self._answer: Any = None
        self.notices: List[str] = []
        self.reporter = RecordingReporter()

    # ------------------------------------------------------ UI protocol

    def notice(self, text: str) -> None:
        self.notices.append(text)
        if self._on_notice is not None:
            self._screen.app.call_from_thread(self._on_notice, text)

    def ask(self, prompt: str, default: str | None = None) -> str:
        answer = self._prompt("input", prompt, default=default)
        return "" if answer is None else str(answer)

    def ask_yes_no(self, prompt: str, default: bool) -> bool:
        answer = self._prompt(
            "yes_no", prompt, default="y" if default else "n"
        )
        if answer is None:
            return default
        return str(answer).strip().lower() == "y"

    def menu(self, options: List[str], prompt: str) -> int | None:
        answer = self._prompt("menu", prompt, options=options)
        return None if answer is None else int(answer)

    def confirm(self, prompt: str, default: bool) -> bool:
        return self.ask_yes_no(prompt, default)

    def retry_or_skip(self, prompt: str) -> str:
        answer = self._prompt("retry", prompt)
        return "r" if answer is None else str(answer)

    def progress(self) -> RecordingReporter:
        return self.reporter

    # ------------------------------------------------------------ plumbing

    def _prompt(
        self,
        kind: str,
        prompt: str,
        options: List[str] | None = None,
        default: str | None = None,
    ) -> Any:
        modal = PromptScreen(
            kind,
            prompt,
            options=options,
            default=default,
            resolver=self._resolve,
        )
        self._screen.app.call_from_thread(self._screen.app.push_screen, modal)
        return self._wait()

    def _resolve(self, value: Any) -> None:
        self._answer = value
        self._event.set()

    def _wait(self) -> Any:
        self._event.wait()
        self._event.clear()
        answer = self._answer
        self._answer = None
        return answer
