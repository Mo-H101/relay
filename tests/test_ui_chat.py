"""
Headless TUI tests for the P2b Chat screen (textual.pilot driven).

Covers random-mode chat, specific-model streaming, unavailable-model
handling, picker availability glyphs, and the inline probe.
"""

import time

import pytest

from app.services import setup_state
from app.setup.scan import ScanResult
from app.ui.app import RelayApp
from app.ui.data import ChatCandidate, ServiceFacade
from app.ui.screens.chat import ChatScreen
from app.ui.widgets import ChatView
from textual.widgets import Input, Select, Static

from tests.ui_fakes import FakeProvider, make_relay


def _transcript(screen) -> str:
    view = screen.query_one("#chat-view", ChatView)
    return "\n".join(
        str(child.render()) for child in view.query(Static)
    )


def _status_text(screen) -> str:
    return str(screen.query_one("#chat-status", Static).render())


async def _wait_until(pilot, predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.02)
    assert predicate(), "condition not met within timeout"


async def _open_chat(pilot, facade: ServiceFacade):
    app = pilot.app
    await pilot.pause()
    await pilot.press("2")
    await pilot.pause()
    assert isinstance(app.screen, ChatScreen)
    return app.screen


async def _type_message(pilot, text: str) -> None:
    app = pilot.app
    input_widget = app.screen.query_one("#chat-input", Input)
    input_widget.focus()
    await pilot.pause()
    await pilot.press(*text)
    await pilot.press("enter")


@pytest.mark.asyncio
async def test_chat_random_mode_sends_and_renders(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])
    facade = ServiceFacade(relay_instance=relay)
    monkeypatch.setattr(
        facade,
        "random_chat",
        lambda message: {
            "success": True,
            "provider": "p1",
            "model": "m1",
            "response": "hello back",
            "latency_ms": 7,
            "attempts": [],
        },
    )

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        await _open_chat(pilot, facade)
        await _type_message(pilot, "hello")

        await _wait_until(pilot, lambda: "hello back" in _transcript(app.screen))

        transcript = _transcript(app.screen)
        assert "You" in transcript
        assert "hello" in transcript
        assert "[p1 \u00b7 m1]" in transcript
        assert "7ms" in transcript


@pytest.mark.asyncio
async def test_chat_specific_mode_streams(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])
    facade = ServiceFacade(relay_instance=relay)

    def fake_start_stream(provider_name, model, message, **kwargs):
        def gen():
            yield {"choices": [{"delta": {"content": "Hel"}}]}
            yield {"choices": [{"delta": {"content": "lo"}}]}
            yield {"choices": [{"delta": {"role": "assistant"}}]}

        return {
            "success": True,
            "provider": "p1",
            "model": "m1",
            "stream_gen": gen(),
            "error": None,
            "attempts": [],
        }

    monkeypatch.setattr(facade, "start_stream", fake_start_stream)
    monkeypatch.setattr(
        facade,
        "specific_model_candidates",
        lambda: [ChatCandidate("p1", "m1", "healthy")],
    )

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_chat(pilot, facade)

        await pilot.press("m")
        await pilot.pause()

        picker = screen.query_one("#model-picker", Select)
        picker.value = ("p1", "m1")
        screen._selected = ("p1", "m1")

        await _type_message(pilot, "hello")

        await _wait_until(pilot, lambda: "Hello" in _transcript(app.screen))

        transcript = _transcript(app.screen)
        assert "[p1 \u00b7 m1]" in transcript
        assert "Hello" in transcript
        assert "\u258d" not in transcript  # stream marker cleared


@pytest.mark.asyncio
async def test_chat_specific_mode_unavailable_model(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])
    facade = ServiceFacade(relay_instance=relay)
    monkeypatch.setattr(
        facade,
        "start_stream",
        lambda *args, **kwargs: {
            "success": False,
            "stream_gen": None,
            "error": "model m1 is unavailable",
            "attempts": [],
        },
    )
    monkeypatch.setattr(
        facade,
        "specific_model_candidates",
        lambda: [ChatCandidate("p1", "m1", "unavailable")],
    )

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_chat(pilot, facade)

        await pilot.press("m")
        await pilot.pause()

        picker = screen.query_one("#model-picker", Select)
        picker.value = ("p1", "m1")
        screen._selected = ("p1", "m1")

        await _type_message(pilot, "hello")

        await _wait_until(
            pilot, lambda: "model m1 is unavailable" in _transcript(app.screen)
        )
        assert "Error" in _transcript(app.screen)


@pytest.mark.asyncio
async def test_chat_picker_shows_availability_glyphs(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    facade = ServiceFacade(relay_instance=make_relay([]))
    monkeypatch.setattr(
        facade,
        "specific_model_candidates",
        lambda: [
            ChatCandidate("p1", "ok-model", "healthy"),
            ChatCandidate("p2", "slow-model", "degraded"),
            ChatCandidate("p3", "dead-model", "unavailable"),
        ],
    )

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_chat(pilot, facade)

        await pilot.press("m")
        await pilot.pause()

        picker = screen.query_one("#model-picker", Select)
        labels = [str(option[0]) for option in picker._options]

        assert any("\u2713" in label and "ok-model" in label for label in labels)
        assert any("\u26a0" in label and "slow-model" in label for label in labels)
        assert any("\u2717" in label and "dead-model" in label for label in labels)


@pytest.mark.asyncio
async def test_inline_probe_updates_status(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])
    facade = ServiceFacade(relay_instance=relay)
    monkeypatch.setattr(
        facade,
        "probe_model",
        lambda provider, model: ScanResult(
            model=model, status="available", latency_ms=12, status_code=200, error=""
        ),
    )
    monkeypatch.setattr(
        facade,
        "specific_model_candidates",
        lambda: [ChatCandidate("p1", "m1", "unknown")],
    )

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_chat(pilot, facade)

        await pilot.press("m")
        await pilot.pause()

        picker = screen.query_one("#model-picker", Select)
        picker.value = ("p1", "m1")
        screen._selected = ("p1", "m1")

        await pilot.press("ctrl+t")
        await pilot.pause()

        await _wait_until(
            pilot, lambda: "\u2713" in _status_text(app.screen)
        )

        status = _status_text(app.screen)
        assert "p1 / m1" in status
        assert "available" in status
        assert "12ms" in status
