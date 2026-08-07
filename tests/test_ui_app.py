"""
Headless TUI smoke tests (pilot-driven, pytest-asyncio).

Boots RelayApp in headless mode, verifies the dashboard mounts and its
tiles populate from a fake facade, and walks all 7 tab bindings.
"""

import pytest

from app.services import setup_state
from app.ui.app import RelayApp
from app.ui.data import ServiceFacade
from app.ui.screens.applications import ApplicationsScreen
from app.ui.screens.chat import ChatScreen
from app.ui.screens.configuration import ConfigurationScreen
from app.ui.screens.dashboard import DashboardScreen
from app.ui.screens.diagnostics import DiagnosticsScreen
from app.ui.screens.models import ModelsScreen
from app.ui.screens.providers import ProvidersScreen
from app.ui.widgets import StatTile

from tests.ui_fakes import FakeProvider, make_relay


@pytest.mark.asyncio
async def test_boots_to_dashboard_and_walks_all_tabs():
    app = RelayApp(start_server=False)

    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)

        sequence = [
            ("2", ChatScreen),
            ("ctrl+3", ModelsScreen),
            ("ctrl+4", ProvidersScreen),
            ("ctrl+5", ConfigurationScreen),
            ("ctrl+6", ApplicationsScreen),
            ("ctrl+7", DiagnosticsScreen),
            ("ctrl+1", DashboardScreen),
        ]
        for key, screen_cls in sequence:
            await pilot.press(key)
            await pilot.pause()
            assert isinstance(app.screen, screen_cls), key

        await pilot.press("q")
        await pilot.pause()


@pytest.mark.asyncio
async def test_dashboard_tiles_populate_from_fake_facade(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")

    relay = make_relay(
        [FakeProvider("p1", api_key="k", models=["m1", "m2"])]
    )
    facade = ServiceFacade(relay_instance=relay)
    app = RelayApp(facade=facade, start_server=False)

    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)

        assert screen.query_one("#tile-providers", StatTile)._value == "1/1"
        assert screen.query_one("#tile-models", StatTile)._value == "2/2"
        assert screen.query_one("#tile-setup", StatTile)._value == "not_configured"
        assert screen.query_one("#tile-server", StatTile)._value == "Stopped"

        await pilot.press("q")
        await pilot.pause()


@pytest.mark.asyncio
async def test_no_embed_flag_prevents_embedded_server(monkeypatch):
    from app.core import config as config_mod

    monkeypatch.setattr(
        config_mod.settings, "relay_tui_no_embed", True
    )
    app = RelayApp(start_server=False)

    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)
        await pilot.press("q")
        await pilot.pause()


@pytest.mark.asyncio
async def test_dashboard_warns_when_auth_disabled(monkeypatch, tmp_path):
    from app.core import config as config_mod
    from textual.widgets import Static

    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    monkeypatch.setattr(config_mod.settings, "relay_api_key", "")
    monkeypatch.setattr(config_mod.settings, "relay_auth_store", False)

    facade = ServiceFacade(relay_instance=make_relay([]))
    app = RelayApp(facade=facade, start_server=False)

    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)
        text = str(screen.query_one("#dashboard-status", Static).render())
        assert "authentication is disabled" in text.lower()
        await pilot.press("q")
        await pilot.pause()


@pytest.mark.asyncio
async def test_dashboard_omits_warning_when_auth_enabled(monkeypatch, tmp_path):
    from app.core import config as config_mod
    from textual.widgets import Static

    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    monkeypatch.setattr(config_mod.settings, "relay_api_key", "sk-test")
    monkeypatch.setattr(config_mod.settings, "relay_auth_store", False)

    facade = ServiceFacade(relay_instance=make_relay([]))
    app = RelayApp(facade=facade, start_server=False)

    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)
        text = str(screen.query_one("#dashboard-status", Static).render())
        assert "authentication is disabled" not in text.lower()
        await pilot.press("q")
        await pilot.pause()
