"""P2b chat screen: random and specific-model chat with streaming."""

from __future__ import annotations

import asyncio
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


class StreamError(Message):
    """The stream raised while being consumed."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class StreamFinished(Message):
    """The stream ended cleanly."""

    def __init__(self, latency_ms: int) -> None:
        super().__init__()
        self.latency_ms = latency_ms


class ChatScreen(Screen):
    """
    Tab 2. Two chat modes:

    * Random — Relay picks the provider (same candidate path as /chat)
      and fails over across its chat-testable models.
    * Model — chat against one specific (provider, model) with streaming
      response rendering.

    Also hosts the inline availability test (✓/⚠/✗) for the selected
    model via the live probe button.
    """

    BINDINGS = [
        Binding("r", "mode_random", "Random"),
        Binding("m", "mode_model", "Model"),
        Binding("ctrl+t", "probe", "Test model"),
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
                yield Button("Random", id="mode-random", variant="primary")
                yield Button("Model", id="mode-model")
                yield Select([], id="model-picker", prompt="Model\u2026", classes="hidden")
                yield Input(
                    placeholder="Message\u2026",
                    id="chat-input",
                )
                yield Button("Send", id="send", variant="success")
                yield Button("Test model", id="probe")
            yield ChatView(id="chat-view")
            yield Static("", id="chat-status")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_picker()

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
            model_button.variant = "default"
            picker.add_class("hidden")
        else:
            random_button.variant = "default"
            model_button.variant = "primary"
            picker.remove_class("hidden")

    # ------------------------------------------------------------- actions

    def action_mode_random(self) -> None:
        self._set_mode("random")

    def action_mode_model(self) -> None:
        self._set_mode("model")

    async def action_probe(self) -> None:
        await self._probe_selected()

    async def _probe_selected(self) -> None:
        if self._selected is None:
            self._set_status("Pick a model first (press m, then choose from the list).")
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
        self._set_status("Waiting for a provider\u2026")
        try:
            result = await asyncio.to_thread(self._facade.random_chat, message)
        except Exception as exc:  # noqa: BLE001 - surface in the transcript
            self._view().add_error(f"Chat failed: {exc}")
        else:
            self._handle_result(result)
        finally:
            self._set_busy(False)
            self._set_status("")

    async def _send_specific(self, message: str) -> None:
        if self._selected is None:
            self._view().add_error(
                "Pick a model first (press m, then choose from the list)."
            )
            return

        provider, model = self._selected
        self._set_busy(True)
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
        )

    def _handle_result(self, result: dict) -> None:
        if result.get("success"):
            self._view().add_assistant(
                result["provider"],
                result["model"],
                result["response"],
                latency_ms=result.get("latency_ms"),
                status="healthy",
            )
        else:
            self._view().add_error(result.get("error") or "Chat failed.")

    # ------------------------------------------------------------ streaming

    async def _consume_stream(self, stream_gen, provider: str, model: str) -> None:
        self._view().begin_stream(provider, model)
        started = time.perf_counter()

        def _run() -> None:
            try:
                for chunk in stream_gen:
                    pieces = []
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if content:
                            pieces.append(content)
                    if pieces:
                        self.app.call_from_thread(
                            self.post_message, StreamChunk("".join(pieces))
                        )
                latency_ms = int((time.perf_counter() - started) * 1000)
                self.app.call_from_thread(
                    self.post_message, StreamFinished(latency_ms)
                )
            except Exception as exc:  # noqa: BLE001 - surface in the transcript
                self.app.call_from_thread(
                    self.post_message, StreamError(str(exc))
                )

        await asyncio.to_thread(_run)

    def on_stream_chunk(self, event: StreamChunk) -> None:
        self._view().append_stream(event.text)

    def on_stream_finished(self, event: StreamFinished) -> None:
        self._view().finalize_stream(latency_ms=event.latency_ms)
        self._stream_teardown()

    def on_stream_error(self, event: StreamError) -> None:
        self._view().finalize_stream(error=event.text)
        self._stream_teardown()

    def _stream_teardown(self) -> None:
        self._set_busy(False)
        self._set_status("")
