"""
Headless TUI tests for the P2b Chat screen (textual.pilot driven).

Covers random-mode chat, specific-model streaming, unavailable-model
handling, picker availability glyphs, the inline probe, copy
functionality, empty state, and markdown rendering.
"""

import asyncio
import time

import pytest

from app.services import setup_state
from app.setup.scan import ScanResult
from app.ui.app import RelayApp
from app.ui.data import ChatCandidate, ServiceFacade
from app.ui.screens.chat import ChatScreen
from app.ui.widgets import ChatView
from textual.widgets import Input, Markdown, Select, Static

from tests.ui_fakes import FakeProvider, make_relay


def _transcript(screen) -> str:
    view = screen.query_one("#chat-view", ChatView)
    parts = []
    for child in view.query(Static):
        try:
            content = child.content
            if content is not None:
                parts.append(str(content))
                continue
        except Exception:
            pass
        parts.append(str(child.render()))
    for child in view.query(Markdown):
        parts.append(child.source)
    return "\n".join(parts)


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


# ------------------------------------------------------------- streaming


@pytest.mark.asyncio
async def test_chat_random_mode_streams_and_renders(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])
    facade = ServiceFacade(relay_instance=relay)

    def fake_start_random_stream(message, on_progress=None, **kwargs):
        def gen():
            yield {"choices": [{"delta": {"content": "hello"}}]}
            yield {"choices": [{"delta": {"content": " back"}}]}

        return {
            "success": True,
            "provider": "p1",
            "model": "m1",
            "stream_gen": gen(),
            "error": None,
            "attempts": [],
            "timing": {"request_ms": 5, "candidate_count": 1},
        }

    monkeypatch.setattr(facade, "start_random_stream", fake_start_random_stream)

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
        assert "p1" in transcript and "m1" in transcript

        status = _status_text(app.screen)
        assert "provider start" in status
        assert "first token" in status
        assert "total" in status


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

        screen.action_mode_model()
        await pilot.pause()

        picker = screen.query_one("#model-picker", Select)
        picker.value = ("p1", "m1")
        screen._selected = ("p1", "m1")

        await _type_message(pilot, "hello")

        await _wait_until(pilot, lambda: "Hello" in _transcript(app.screen))

        transcript = _transcript(app.screen)
        assert "p1" in transcript and "m1" in transcript
        assert "Hello" in transcript


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

        screen.action_mode_model()
        await pilot.pause()

        picker = screen.query_one("#model-picker", Select)
        picker.value = ("p1", "m1")
        screen._selected = ("p1", "m1")

        await _type_message(pilot, "hello")

        await _wait_until(
            pilot, lambda: "model m1 is unavailable" in _transcript(app.screen)
        )
        assert "Error" in _transcript(app.screen)


# -------------------------------------------------------- picker / probe


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

        screen.action_mode_model()
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

        screen.action_mode_model()
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


# ----------------------------------------------------------- navigation


async def _focus_chat_input(screen, pilot) -> None:
    screen.query_one("#chat-input", Input).focus()
    await pilot.pause()


@pytest.mark.asyncio
async def test_ctrl_digit_navigates_while_input_focused(monkeypatch, tmp_path):
    from app.ui.screens.models import ModelsScreen

    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])
    facade = ServiceFacade(relay_instance=relay)
    app = RelayApp(facade=facade, start_server=False)

    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_chat(pilot, facade)
        await _focus_chat_input(screen, pilot)

        await pilot.press("ctrl+3")
        await pilot.pause()
        assert isinstance(app.screen, ModelsScreen)

        await pilot.press("ctrl+2")
        await pilot.pause()
        assert isinstance(app.screen, ChatScreen)

        await pilot.press("ctrl+1")
        await pilot.pause()
        from app.ui.screens.dashboard import DashboardScreen

        assert isinstance(app.screen, DashboardScreen)


@pytest.mark.asyncio
async def test_chat_input_focused_on_entry(monkeypatch, tmp_path):
    from app.ui.screens.dashboard import DashboardScreen

    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])
    facade = ServiceFacade(relay_instance=relay)
    app = RelayApp(facade=facade, start_server=False)

    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_chat(pilot, facade)
        assert app.focused is screen.query_one("#chat-input", Input)

        await pilot.press("ctrl+1")
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)

        await pilot.press("2")
        await pilot.pause()
        assert isinstance(app.screen, ChatScreen)
        assert app.focused is app.screen.query_one("#chat-input", Input)


@pytest.mark.asyncio
async def test_plain_digit_types_in_focused_input(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])
    facade = ServiceFacade(relay_instance=relay)
    app = RelayApp(facade=facade, start_server=False)

    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_chat(pilot, facade)
        input_widget = screen.query_one("#chat-input", Input)
        input_widget.focus()
        await pilot.pause()

        await pilot.press("3", "1")
        await pilot.pause()

        assert isinstance(app.screen, ChatScreen)
        assert input_widget.value == "31"


@pytest.mark.asyncio
async def test_escape_returns_to_dashboard_while_input_focused(monkeypatch, tmp_path):
    from app.ui.screens.dashboard import DashboardScreen

    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])
    facade = ServiceFacade(relay_instance=relay)
    app = RelayApp(facade=facade, start_server=False)

    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_chat(pilot, facade)
        await _focus_chat_input(screen, pilot)

        await pilot.press("escape")
        await pilot.pause()

        assert isinstance(app.screen, DashboardScreen)


@pytest.mark.asyncio
async def test_ctrl_nav_works_after_send(monkeypatch, tmp_path):
    from app.ui.screens.dashboard import DashboardScreen

    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])
    facade = ServiceFacade(relay_instance=relay)

    def fake_start_random_stream(message, on_progress=None, **kwargs):
        def gen():
            yield {"choices": [{"delta": {"content": "hello back"}}]}

        return {
            "success": True,
            "provider": "p1",
            "model": "m1",
            "stream_gen": gen(),
            "error": None,
            "attempts": [],
        }

    monkeypatch.setattr(facade, "start_random_stream", fake_start_random_stream)

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        await _open_chat(pilot, facade)
        await _type_message(pilot, "hello")

        await _wait_until(pilot, lambda: "hello back" in _transcript(app.screen))

        await pilot.press("ctrl+1")
        await pilot.pause()

        assert isinstance(app.screen, DashboardScreen)


# ----------------------------------------------------------- copy


@pytest.mark.asyncio
async def test_copy_last_shows_unavailable_when_no_clipboard(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])
    facade = ServiceFacade(relay_instance=relay)

    def fake_start_random_stream(message, on_progress=None, **kwargs):
        def gen():
            yield {"choices": [{"delta": {"content": "response text"}}]}

        return {
            "success": True,
            "provider": "p1",
            "model": "m1",
            "stream_gen": gen(),
            "error": None,
            "attempts": [],
            "timing": {"request_ms": 5, "candidate_count": 1},
        }

    monkeypatch.setattr(facade, "start_random_stream", fake_start_random_stream)

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        await _open_chat(pilot, facade)
        await _type_message(pilot, "hello")

        await _wait_until(
            pilot, lambda: "total" in _status_text(app.screen)
        )

        screen = app.screen
        screen.action_copy_last()
        await pilot.pause()

        status = _status_text(app.screen)
        assert "Clipboard" in status or "copy" in status.lower()


@pytest.mark.asyncio
async def test_copy_last_shows_nothing_when_no_response(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])
    facade = ServiceFacade(relay_instance=relay)

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        await _open_chat(pilot, facade)

        screen = app.screen
        screen.action_copy_last()
        await pilot.pause()

        status = _status_text(app.screen)
        assert "Nothing to copy" in status


# -------------------------------------------------------- empty state


@pytest.mark.asyncio
async def test_empty_state_shows_guidance(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])
    facade = ServiceFacade(relay_instance=relay)

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_chat(pilot, facade)

        transcript = _transcript(screen)
        assert "Relay Chat" in transcript
        assert "Random" in transcript
        assert "Model" in transcript


# --------------------------------------------------- mode switcher


@pytest.mark.asyncio
async def test_mode_switcher_updates_button_labels(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])
    facade = ServiceFacade(relay_instance=relay)

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_chat(pilot, facade)

        random_btn = screen.query_one("#mode-random")
        assert random_btn.variant == "primary"
        assert "\u25cf" in str(random_btn.label)

        screen.action_mode_model()
        await pilot.pause()

        model_btn = screen.query_one("#mode-model")
        assert model_btn.variant == "primary"
        assert "\u25cf" in str(model_btn.label)

        picker = screen.query_one("#model-picker", Select)
        assert "hidden" not in picker.classes


# --------------------------------------------------- markdown rendering


@pytest.mark.asyncio
async def test_markdown_rendering_in_finalized_response(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])
    facade = ServiceFacade(relay_instance=relay)

    def fake_start_random_stream(message, on_progress=None, **kwargs):
        def gen():
            yield {"choices": [{"delta": {"content": "# Hello\n\n"}}]}
            yield {"choices": [{"delta": {"content": "**bold** text"}}]}

        return {
            "success": True,
            "provider": "p1",
            "model": "m1",
            "stream_gen": gen(),
            "error": None,
            "attempts": [],
            "timing": {"request_ms": 5, "candidate_count": 1},
        }

    monkeypatch.setattr(facade, "start_random_stream", fake_start_random_stream)

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        await _open_chat(pilot, facade)
        await _type_message(pilot, "hello")

        await _wait_until(
            pilot, lambda: "total" in _status_text(app.screen)
        )

        await pilot.pause()
        await pilot.pause()

        view = app.screen.query_one("#chat-view", ChatView)
        md_widgets = list(view.query(Markdown))
        assert len(md_widgets) >= 1, f"Expected Markdown widget, found: {list(view.children)}"
