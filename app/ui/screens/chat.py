"""P2b chat screen: random and specific-model chat with streaming."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Select, Static

from app.ui.data import ServiceFacade, candidate_glyph, probe_glyph
from app.ui.theme import theme
from app.ui.widgets.chat_view import ChatView


class StreamChunk(Message):
    """A chunk of content arrived for the open stream."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class StreamStatus(Message):
    """Failover progress update raised by the facade worker thread."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class StreamError(Message):
    """The stream raised while being consumed."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class StreamFinished(Message):
    """The stream ended cleanly."""

    def __init__(
        self,
        latency_ms: int,
        *,
        first_token_ms: int | None = None,
        render_ms: int | None = None,
        overhead_ms: int | None = None,
    ) -> None:
        super().__init__()
        self.latency_ms = latency_ms
        self.first_token_ms = first_token_ms
        self.render_ms = render_ms
        self.overhead_ms = overhead_ms


def _copy_to_clipboard(text: str) -> bool:
    """Copy text to the system clipboard. Returns True on success."""
    for cmd in (
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
        ["wl-copy"],
        ["pbcopy"],
    ):
        if shutil.which(cmd[0]):
            try:
                proc = subprocess.run(
                    cmd, input=text.encode(), timeout=3, check=False
                )
                return proc.returncode == 0
            except Exception:  # noqa: BLE001
                return False
    return False


class ChatScreen(Screen):
    """
    Tab 2. Two chat modes:

    * Random — Relay picks the provider (same candidate path as /chat)
      and fails over across its chat-testable models.
    * Model — chat against one specific (provider, model) with streaming
      response rendering.

    Also hosts the inline availability test for the selected
    model via the live probe button.
    """

    BINDINGS = [
        Binding("r", "mode_random", "Random"),
        Binding("m", "mode_model", "Model"),
        Binding("ctrl+t", "probe", "Test model"),
        Binding("C", "copy_last", "Copy", priority=True, show=False),
    ]

    def __init__(self, facade: ServiceFacade) -> None:
        super().__init__()
        self._facade = facade
        self._mode = "random"
        self._selected: tuple[str, str] | None = None
        self._busy = False

    # ------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Chat", classes="screen-title")
        with Vertical(id="chat-root"):
            with Horizontal(id="chat-controls"):
                yield Button("\u25cf Random", id="mode-random", variant="primary")
                yield Button("\u25cb Model", id="mode-model")
                yield Select(
                    [], id="model-picker", prompt="Model\u2026", classes="hidden"
                )
                yield Input(
                    placeholder="Message\u2026",
                    id="chat-input",
                )
                yield Button("Send", id="send", variant="success")
                yield Button("Test", id="probe")
            yield ChatView(id="chat-view")
            yield Static("", id="chat-status")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_picker()
        self._input().focus()

    def on_screen_resume(self) -> None:
        self._refresh_picker()
        self._input().focus()

    # ------------------------------------------------------------- helpers

    def _view(self) -> ChatView:
        return self.query_one("#chat-view", ChatView)

    def _input(self) -> Input:
        return self.query_one("#chat-input", Input)

    def _status(self) -> Static:
        return self.query_one("#chat-status", Static)

    def _picker(self) -> Select:
        return self.query_one("#model-picker", Select)

    def _set_status(self, text: str) -> None:
        self._status().update(text)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.query_one("#send", Button).disabled = busy
        self._input().disabled = busy

    def _refresh_picker(self) -> None:
        candidates = self._facade.specific_model_candidates()
        options = [
            (
                f"{candidate_glyph(c.status)} {c.provider} / {c.model}",
                (c.provider, c.model),
            )
            for c in candidates
        ]
        picker = self._picker()
        picker.set_options(options)
        if options:
            current = self._selected
            if current in [value for _, value in options]:
                picker.value = current
        if not options:
            self._set_status("No chat-testable models configured.")

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        random_button = self.query_one("#mode-random", Button)
        model_button = self.query_one("#mode-model", Button)
        picker = self._picker()

        if mode == "random":
            random_button.variant = "primary"
            random_button.label = "\u25cf Random"
            model_button.variant = "default"
            model_button.label = "\u25cb Model"
            picker.add_class("hidden")
        else:
            random_button.variant = "default"
            random_button.label = "\u25cb Random"
            model_button.variant = "primary"
            model_button.label = "\u25cf Model"
            picker.remove_class("hidden")

    # ------------------------------------------------------------- actions

    def action_mode_random(self) -> None:
        self._set_mode("random")

    def action_mode_model(self) -> None:
        self._set_mode("model")

    async def action_probe(self) -> None:
        await self._probe_selected()

    def action_copy_last(self) -> None:
        text = self._view().last_assistant_text
        if not text:
            self._set_status("Nothing to copy yet.")
            return
        if _copy_to_clipboard(text):
            self._set_status("Copied to clipboard.")
        else:
            self._set_status("Clipboard unavailable in this environment.")

    async def _probe_selected(self) -> None:
        if self._selected is None:
            self._set_status(
                "Pick a model first (press m, then choose from the list)."
            )
            return

        provider, model = self._selected
        self._set_status(f"Testing {provider} / {model}\u2026")
        try:
            result = await asyncio.to_thread(
                self._facade.probe_model, provider, model
            )
        except Exception as exc:  # noqa: BLE001 - surface in the status line
            self._set_status(f"Probe failed: {exc}")
            return

        if result is None:
            self._set_status(f"Unknown provider: {provider}")
            return

        glyph = probe_glyph(result.status)
        color = {
            "available": theme.ok,
            "overloaded": theme.warn,
            "unavailable": theme.error,
        }.get(result.status, theme.muted)
        latency = f" {result.latency_ms}ms" if result.latency_ms else ""
        self._set_status(
            f"[{color}]{glyph}[/] {provider} / {model} "
            f"\u2014 {result.status}{latency}"
        )
        self._refresh_picker()

    # ------------------------------------------------------------ handlers

    @on(Button.Pressed, "#send")
    async def _on_send(self) -> None:
        await self._send()

    @on(Button.Pressed, "#mode-random")
    def _on_mode_random(self) -> None:
        self.action_mode_random()

    @on(Button.Pressed, "#mode-model")
    def _on_mode_model(self) -> None:
        self.action_mode_model()

    @on(Button.Pressed, "#probe")
    async def _on_probe(self) -> None:
        await self._probe_selected()

    @on(Select.Changed, "#model-picker")
    def _on_picker_changed(self, event: Select.Changed) -> None:
        self._selected = event.value if isinstance(event.value, tuple) else None

    @on(Input.Submitted, "#chat-input")
    async def _on_submitted(self) -> None:
        await self._send()

    # -------------------------------------------------------------- sending

    async def _send(self) -> None:
        if self._busy:
            return
        message = self._input().value.strip()
        if not message:
            return
        self._input().value = ""
        self._view().add_user(message)

        if self._mode == "random":
            await self._send_random(message)
        else:
            await self._send_specific(message)

    async def _send_random(self, message: str) -> None:
        self._set_busy(True)
        request_started = time.perf_counter()
        self._set_status("Waiting for a provider\u2026")

        def _progress(update: dict) -> None:
            stage = update.get("stage")
            index = update.get("index")
            total = update.get("total")
            provider = update.get("provider")
            model = update.get("model")

            if stage == "attempt":
                text = (
                    f"Trying {model} ({provider}) "
                    f"\u2014 candidate {index}/{total}\u2026"
                )
            elif stage == "failed":
                text = "Provider unavailable, switching\u2026"
            elif stage == "started":
                text = f"Streaming from {provider} / {model}\u2026"
            else:
                return
            self.app.call_from_thread(self.post_message, StreamStatus(text))

        try:
            result = await asyncio.to_thread(
                self._facade.start_random_stream, message, _progress
            )
        except Exception as exc:  # noqa: BLE001 - surface in the transcript
            self._view().add_error(f"Chat failed: {exc}")
            self._set_busy(False)
            self._set_status("")
            return

        if not result.get("success"):
            self._view().add_error(result.get("error") or "No provider could start.")
            self._set_busy(False)
            self._set_status("")
            return

        overhead_ms = (result.get("timing") or {}).get("request_ms")
        await self._consume_stream(
            result.get("stream_gen"),
            result.get("provider", ""),
            result.get("model", ""),
            request_started=request_started,
            overhead_ms=overhead_ms,
        )

    async def _send_specific(self, message: str) -> None:
        if self._selected is None:
            self._view().add_error(
                "Pick a model first (press m, then choose from the list)."
            )
            return

        provider, model = self._selected
        self._set_busy(True)
        request_started = time.perf_counter()
        self._set_status("Starting stream\u2026")
        try:
            result = await asyncio.to_thread(
                self._facade.start_stream, provider, model, message
            )
        except Exception as exc:  # noqa: BLE001 - surface in the transcript
            self._view().add_error(f"Chat failed: {exc}")
            self._set_busy(False)
            self._set_status("")
            return

        if not result.get("success"):
            self._view().add_error(result.get("error") or "Stream failed to start.")
            self._set_busy(False)
            self._set_status("")
            return

        stream_gen = result.get("stream_gen")
        await self._consume_stream(
            stream_gen,
            result.get("provider", provider),
            result.get("model", model),
            request_started=request_started,
        )

    # ------------------------------------------------------------ streaming

    async def _consume_stream(
        self,
        stream_gen,
        provider: str,
        model: str,
        *,
        request_started: float | None = None,
        overhead_ms: int | None = None,
    ) -> None:
        self._view().begin_stream(provider, model)
        started = time.perf_counter()
        first_token_at: float | None = None

        def _run() -> None:
            nonlocal first_token_at
            try:
                for chunk in stream_gen:
                    pieces = []
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if content:
                            pieces.append(content)
                    if pieces:
                        if first_token_at is None:
                            first_token_at = time.perf_counter()
                        self.app.call_from_thread(
                            self.post_message, StreamChunk("".join(pieces))
                        )
                finished = time.perf_counter()
                base = request_started or started
                total_ms = int((finished - base) * 1000)
                first_token_ms = (
                    int((first_token_at - base) * 1000)
                    if first_token_at is not None
                    else None
                )
                render_ms = (
                    int((finished - first_token_at) * 1000)
                    if first_token_at is not None
                    else None
                )
                self.app.call_from_thread(
                    self.post_message,
                    StreamFinished(
                        total_ms,
                        first_token_ms=first_token_ms,
                        render_ms=render_ms,
                        overhead_ms=overhead_ms,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - surface in the transcript
                self.app.call_from_thread(
                    self.post_message, StreamError(str(exc))
                )

        await asyncio.to_thread(_run)

    def on_stream_chunk(self, event: StreamChunk) -> None:
        self._view().append_stream(event.text)

    def on_stream_status(self, event: StreamStatus) -> None:
        self._set_status(event.text)

    def on_stream_finished(self, event: StreamFinished) -> None:
        self._view().finalize_stream(latency_ms=event.latency_ms)
        self._stream_teardown()
        self._set_timing_status(event)

    def on_stream_error(self, event: StreamError) -> None:
        self._view().finalize_stream(error=event.text)
        self._stream_teardown()

    def _set_timing_status(self, event: StreamFinished) -> None:
        parts = []
        if event.overhead_ms is not None:
            parts.append(f"provider start {event.overhead_ms}ms")
        if event.first_token_ms is not None:
            parts.append(f"first token {event.first_token_ms}ms")
        parts.append(f"total {event.latency_ms}ms")
        if event.render_ms is not None:
            parts.append(f"render {event.render_ms}ms")
        self._set_status(" \u00b7 ".join(parts))

    def _stream_teardown(self) -> None:
        self._set_busy(False)
        self._set_status("")
