"""
Headless TUI tests for the P2c Models screen (textual.pilot driven).

Covers merged availability glyphs, priority reordering restricted to
available models, and the persist-through-config_store + reload path.
"""

import time

import pytest

from app.services import setup_state
from app.ui.app import RelayApp
from app.ui.data import ServiceFacade
from app.ui.screens.models import ModelsScreen
from textual.widgets import DataTable, Static

from tests.ui_fakes import (
    FakeProvider,
    FakeRelay,
    FakeHealthModel,
    FakeReport,
    make_relay,
)


def _status_text(screen) -> str:
    return str(screen.query_one("#models-status", Static).render())


def _table_text(screen) -> str:
    table = screen.query_one("#models-table", DataTable)
    return " ".join(
        str(cell)
        for index in range(table.row_count)
        for cell in table.get_row_at(index)
    )


async def _wait_until(pilot, predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.02)
    assert predicate(), "condition not met within timeout"


async def _open_models(pilot, facade: ServiceFacade) -> ModelsScreen:
    app = pilot.app
    await pilot.pause()
    await pilot.press("3")
    await pilot.pause()
    assert isinstance(app.screen, ModelsScreen)
    return app.screen


def _nvidia_relay() -> FakeRelay:
    relay = FakeRelay()
    provider = FakeProvider(
        "NVIDIA", api_key="k", models=["m1", "m2", "m3"]
    )
    relay.provider_manager.register(provider)
    relay.health_store.set(
        FakeReport(
            "NVIDIA",
            [
                FakeHealthModel("m1", "healthy"),
                FakeHealthModel("m2", "healthy"),
                FakeHealthModel("m3", "unavailable"),
            ],
        )
    )
    return relay


@pytest.mark.asyncio
async def test_models_table_renders_merged_glyphs(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    from app.setup import persistence

    monkeypatch.setattr(persistence, "state_dir", tmp_path)
    relay = FakeRelay()
    provider = FakeProvider("NVIDIA", api_key="k", models=["ok-model", "dead-model"])
    relay.provider_manager.register(provider)
    relay.health_store.set(
        FakeReport(
            "NVIDIA",
            [
                FakeHealthModel("ok-model", "healthy"),
                FakeHealthModel("dead-model", "unavailable"),
            ],
        )
    )
    facade = ServiceFacade(relay_instance=relay)

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_models(pilot, facade)
        text = _table_text(screen)
        assert "ok-model" in text and "dead-model" in text
        assert "\u2713" in text and "\u2717" in text


@pytest.mark.asyncio
async def test_priority_reorder_restricted_to_available(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    facade = ServiceFacade(relay_instance=_nvidia_relay())

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_models(pilot, facade)

        screen._selected = ("NVIDIA", "m3")  # unavailable
        await pilot.press("ctrl+up")
        await pilot.pause()
        assert "not available" in _status_text(screen)

        screen._selected = ("NVIDIA", "m2")
        await pilot.press("ctrl+up")
        await pilot.pause()

        status = _status_text(screen)
        assert status.startswith("Priority for NVIDIA:")
        assert status.index("m2") < status.index("m1")


@pytest.mark.asyncio
async def test_apply_priority_persists_and_reloads(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    captured: dict = {}
    facade = ServiceFacade(relay_instance=_nvidia_relay())

    def fake_set_model_priority(defn_id, priority):
        captured["defn_id"] = defn_id
        captured["priority"] = list(priority)
        return {
            "reloaded": True,
            "dry_run": False,
            "applied": ["nvidia_model_priority"],
            "unchanged": [],
            "failures": [],
        }

    monkeypatch.setattr(facade, "set_model_priority", fake_set_model_priority)

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_models(pilot, facade)
        screen._selected = ("NVIDIA", "m1")
        await screen._apply_priority()
        await pilot.pause()

        assert captured["defn_id"] == "nvidia"
        assert captured["priority"] == ["m1", "m2"]
        assert "saved and applied" in _status_text(screen)


@pytest.mark.asyncio
async def test_provider_toggle_persists_enabled_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    captured: dict = {}
    facade = ServiceFacade(relay_instance=make_relay(
        [FakeProvider("NVIDIA", api_key="k", models=["m1"])]
    ))

    def fake_set_provider_enabled(defn_id, enabled):
        captured["toggle"] = (defn_id, enabled)
        return {
            "reloaded": True,
            "dry_run": False,
            "applied": ["nvidia_enabled"],
            "unchanged": [],
            "failures": [],
        }

    monkeypatch.setattr(facade, "set_provider_enabled", fake_set_provider_enabled)

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_models(pilot, facade)
        await screen._toggle_provider()
        await pilot.pause()

        assert captured["toggle"] == ("nvidia", False)  # enabled -> disabled
        assert "saved and applied" in _status_text(screen)
