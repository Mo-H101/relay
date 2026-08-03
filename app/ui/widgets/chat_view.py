"""Reusable chat conversation view for the Relay TUI."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import ScrollableContainer
from textual.widgets import Static

from app.ui.theme import theme

STREAM_MARKER = "\u258d"  # left one-eighth block, shown while streaming


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
        self._stream_parts: list[str] = []
        self._stream_provider = ""
        self._stream_model = ""
        self._stream_active = False

    def compose(self) -> ComposeResult:
        yield Static("No messages yet. Ask a question below.", classes="chat-muted")

    # ------------------------------------------------------------- messages

    def _append(self, content, classes: str) -> Static:
        try:
            placeholder = self.query_one("Static.chat-muted", Static)
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
        text.append("You", style=theme.accent)
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
    ) -> Static:
        text = Text()
        text.append(f"[{provider} \u00b7 {model}]", style=_badge_style(status))
        if latency_ms is not None:
            text.append(f"  {latency_ms}ms", style=theme.muted)
        text.append("\n\n")
        text.append(body, style=theme.text)
        return self._append(text, classes="chat-assistant")

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
        self._stream_parts = []
        self._stream_active = True
        self._stream_target = self._append(Text(), classes="chat-assistant")
        self._render_stream()

    def append_stream(self, chunk: str) -> None:
        self._stream_parts.append(chunk)
        self._render_stream()

    def _render_stream(self) -> None:
        target = self._stream_target
        if target is None:
            return
        text = Text()
        text.append(
            f"[{self._stream_provider} \u00b7 {self._stream_model}]",
            style=theme.accent,
        )
        if self._stream_active:
            text.append(" \u25cf", style=theme.accent)
        text.append("\n\n")
        text.append("".join(self._stream_parts), style=theme.text)
        if self._stream_active:
            text.append(STREAM_MARKER, style=theme.accent)
        target.update(text)
        self.call_after_refresh(self._scroll_to_end)

    def finalize_stream(
        self, *, latency_ms: int | None = None, error: str = ""
    ) -> None:
        target = self._stream_target
        if target is None:
            return
        provider, model = self._stream_provider, self._stream_model
        body = "".join(self._stream_parts)
        if not body.strip():
            body = "(empty response)"
        self._stream_target = None
        self._stream_parts = []
        self._stream_active = False

        text = Text()
        text.append(f"[{provider} \u00b7 {model}]", style=_badge_style("healthy"))
        if latency_ms is not None and not error:
            text.append(f"  {latency_ms}ms", style=theme.muted)
        text.append("\n\n")
        text.append(body, style=theme.text)
        if error:
            text.append("\n")
            text.append(f"Error: {error}", style=theme.error)
        target.update(text)
        self.call_after_refresh(self._scroll_to_end)


def _badge_style(status: str) -> str:
    return {
        "healthy": theme.ok,
        "available": theme.ok,
        "degraded": theme.warn,
        "overloaded": theme.warn,
        "unavailable": theme.error,
        "error": theme.error,
    }.get(status, theme.accent)
