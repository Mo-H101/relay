"""Reusable chat conversation view for the Relay TUI."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import ScrollableContainer
from textual.widgets import Static

from app.ui.theme import theme

_STREAM_FRAMES = ("\u25cf\u25cb\u25cb", "\u25cb\u25cf\u25cb", "\u25cb\u25cb\u25cf")


class ChatView(ScrollableContainer):
    """
    Scrollable conversation transcript rendered as Rich text bubbles.

    The latest assistant bubble is kept as the streaming target so chunks
    arriving off the UI thread can be appended in place.
    """

    DEFAULT_CSS = """
    ChatView {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
        background: $surface;
    }
    ChatView > Static {
        width: 100%;
        padding: 0 0 0 0;
        margin: 0 0 1 0;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._stream_target: Static | None = None
        self._stream_body: str = ""
        self._stream_provider = ""
        self._stream_model = ""
        self._stream_active = False
        self._stream_frame = 0
        self._last_assistant_text = ""

    def compose(self) -> ComposeResult:
        yield Static(self._empty_state_text(), classes="chat-empty-state")

    def _empty_state_text(self) -> Text:
        text = Text()
        text.append("Relay Chat\n", style=f"bold {theme.accent}")
        text.append(
            "Ask anything \u2014 Relay routes your message to the best available provider.\n\n",
            style=theme.text,
        )
        text.append("  ", style="")
        text.append("r", style=f"bold {theme.accent}")
        text.append(" Random  ", style=theme.text_muted)
        text.append("Relay picks the provider\n", style=theme.text_subtle)
        text.append("  ", style="")
        text.append("m", style=f"bold {theme.accent}")
        text.append(" Model   ", style=theme.text_muted)
        text.append("Choose a specific provider\n", style=theme.text_subtle)
        text.append("  ", style="")
        text.append("Ctrl+T", style=f"bold {theme.accent}")
        text.append("          ", style=theme.text_muted)
        text.append("Test selected model\n", style=theme.text_subtle)
        return text

    # ------------------------------------------------------------- messages

    def _append(self, content, classes: str) -> Static:
        try:
            placeholder = self.query_one(".chat-empty-state", Static)
            placeholder.remove()
        except Exception:
            pass
        bubble = Static(content, classes=classes)
        self.mount(bubble)
        self.call_after_refresh(self._scroll_to_end)
        return bubble

    def _scroll_to_end(self) -> None:
        try:
            self.scroll_end(animate=False)
        except Exception:
            pass

    def add_user(self, message: str) -> None:
        text = Text()
        text.append("You", style=f"bold {theme.accent}")
        text.append("\n")
        text.append(message, style=theme.text)
        self._append(text, classes="chat-user")

    def add_assistant(
        self,
        provider: str,
        model: str,
        body: str,
        *,
        latency_ms: int | None = None,
        status: str = "unknown",
    ) -> None:
        content = Text()
        content.append(f"{provider} \u00b7 {model}", style=_badge_style(status))
        if latency_ms is not None:
            content.append(f"  {latency_ms}ms", style=theme.muted)
        content.append("\n\n")
        content.append(body, style=theme.text)
        self._append(content, classes="chat-assistant")

    def add_error(self, message: str) -> None:
        text = Text()
        text.append("Error", style=theme.error)
        text.append("\n")
        text.append(message, style=theme.error)
        self._append(text, classes="chat-error")

    def add_system(self, message: str) -> None:
        text = Text()
        text.append(message, style=theme.muted)
        self._append(text, classes="chat-system")

    # ------------------------------------------------------------- streaming

    def begin_stream(self, provider: str, model: str) -> None:
        self._stream_provider = provider
        self._stream_model = model
        self._stream_body = ""
        self._stream_active = True
        self._stream_frame = 0
        self._stream_target = self._append(Text(), classes="chat-assistant")
        self._render_stream()

    def append_stream(self, chunk: str) -> None:
        self._stream_body += chunk
        self._render_stream()

    def _render_stream(self) -> None:
        target = self._stream_target
        if target is None:
            return
        text = Text()
        text.append(
            f"{self._stream_provider} \u00b7 {self._stream_model}",
            style=theme.accent,
        )
        if self._stream_active:
            frame = _STREAM_FRAMES[self._stream_frame % len(_STREAM_FRAMES)]
            self._stream_frame += 1
            text.append(" ", style="")
            text.append(frame, style=theme.accent)
        text.append("\n\n")
        text.append(self._stream_body, style=theme.text)
        target.update(text)
        self.call_after_refresh(self._scroll_to_end)

    def finalize_stream(
        self, *, latency_ms: int | None = None, error: str = ""
    ) -> None:
        target = self._stream_target
        if target is None:
            return
        provider, model = self._stream_provider, self._stream_model
        body = self._stream_body
        if not body.strip():
            body = "(empty response)"
        self._stream_target = None
        self._stream_body = ""
        self._stream_active = False

        self._last_assistant_text = body

        header = Text()
        header.append(
            f"{provider} \u00b7 {model}", style=_badge_style("healthy")
        )
        if latency_ms is not None and not error:
            header.append(f"  {latency_ms}ms", style=theme.muted)

        target.update(header)
        if error:
            error_line = Text()
            error_line.append(f"Error: {error}", style=theme.error)
            self._append(error_line, classes="chat-error")
        else:
            from textual.widgets import Markdown
            self.mount(Markdown(body))
        self.call_after_refresh(self._scroll_to_end)

    @property
    def last_assistant_text(self) -> str:
        return self._last_assistant_text


def _badge_style(status: str) -> str:
    return {
        "healthy": theme.ok,
        "available": theme.ok,
        "degraded": theme.warn,
        "overloaded": theme.warn,
        "unavailable": theme.error,
        "error": theme.error,
    }.get(status, theme.accent)
