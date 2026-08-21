"""
Headless TUI tests for the P2c Providers screen (textual.pilot driven).

Covers the catalog projection (no secret leakage), the add/re-run setup
flows behind the wizard adapter, per-provider rescan, enable/disable
persistence, and the key-validation-before-persist rule.
"""

import asyncio
import time

import pytest

from app.services import setup_state
from app.ui.app import RelayApp
from app.ui.data import ServiceFacade
from app.ui.screens.providers import ProvidersScreen
from app.ui.setup_adapter import PromptScreen, SetupAdapter
from textual.widgets import DataTable, Static

from tests.ui_fakes import (
    FakeProvider,
    FakeClient,
    FakeHealthModel,
    FakeReport,
    FakeRelay,
    FakeStore,
    make_relay,
)


def _status_text(screen) -> str:
    return str(screen.query_one("#providers-status", Static).render())


def _table_text(screen) -> str:
    table = screen.query_one("#providers-table", DataTable)
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


async def _open_providers(pilot, facade: ServiceFacade) -> ProvidersScreen:
    app = pilot.app
    await pilot.pause()
    await pilot.press("4")
    await pilot.pause()
    assert isinstance(app.screen, ProvidersScreen)
    return app.screen


@pytest.mark.asyncio
async def test_providers_table_shows_catalog_without_secrets(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    relay = make_relay(
        [FakeProvider("NVIDIA", api_key="super-secret-key", models=["m1"])]
    )
    facade = ServiceFacade(relay_instance=relay)

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_providers(pilot, facade)
        text = _table_text(screen)

        assert "NVIDIA NIM" in text
        assert "active" in text  # Stage C: compound status uses "active"
        assert "set" in text
        assert "super-secret-key" not in text
        assert "super-secret" not in _status_text(screen)


@pytest.mark.asyncio
async def test_health_column_uses_runtime_provider_name(monkeypatch, tmp_path):
    """
    Regression test for Stage C DEFECT 1: the health summary must match
    ModelInfo.provider (the runtime name, e.g. "NVIDIA") — NOT
    ProviderCatalogEntry.display_name (e.g. "NVIDIA NIM").

    This test verifies health renders correctly for providers whose
    display_name differs from their runtime provider_name: NVIDIA,
    LM Studio, and Ollama.
    """
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")

    relay = FakeRelay()

    # Register providers whose display_name != runtime name
    for name, models, statuses in [
        ("NVIDIA", ["m1", "m2"], ["healthy", "degraded"]),
        ("LM Studio", ["local-m1"], ["healthy"]),
        ("Ollama", ["oll-m1", "oll-m2"], ["unavailable", "unavailable"]),
    ]:
        provider = FakeProvider(name, api_key="k", models=models)
        relay.provider_manager.register(provider)
        relay.health_store.set(
            FakeReport(
                name,
                [FakeHealthModel(m, s) for m, s in zip(models, statuses)],
            )
        )

    facade = ServiceFacade(relay_instance=relay)
    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_providers(pilot, facade)
        table = screen.query_one("#providers-table", DataTable)

        # Verify health column shows correct data for each provider
        health_by_provider: dict[str, str] = {}
        for index in range(table.row_count):
            row = table.get_row_at(index)
            # Columns: "", Provider, Kind, Status, API key, Models, Health
            provider_display = str(row[1])
            health_cell = str(row[6])
            health_by_provider[provider_display] = health_cell

        # NVIDIA NIM: 1 healthy, 1 degraded → "● partial"
        assert "NVIDIA NIM" in health_by_provider
        assert "partial" in health_by_provider["NVIDIA NIM"]

        # LM Studio (local): 1 healthy → "● good"
        assert "LM Studio (local)" in health_by_provider
        assert "good" in health_by_provider["LM Studio (local)"]

        # Ollama (local): 0 healthy, 2 unavailable → "● poor"
        assert "Ollama (local)" in health_by_provider
        assert "poor" in health_by_provider["Ollama (local)"]


@pytest.mark.asyncio
async def test_add_provider_runs_wizard_restricted_to_unconfigured(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    captured: dict = {}
    facade = ServiceFacade(relay_instance=make_relay(
        [FakeProvider("NVIDIA", api_key="k", models=["m1"])]
    ))

    def fake_run_setup(ui, *, menu=None, store=None):
        captured["ui"] = ui
        captured["menu"] = menu
        return type(
            "SetupResult",
            (),
            {
                "completed": True,
                "usable": True,
                "configured": ["openai"],
                "state": "configured",
            },
        )()

    monkeypatch.setattr(facade, "run_setup", fake_run_setup)

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_providers(pilot, facade)
        await screen._on_add_provider()
        await pilot.pause()

        ui = captured["ui"]
        for method in (
            "notice",
            "ask",
            "ask_yes_no",
            "menu",
            "confirm",
            "retry_or_skip",
            "progress",
        ):
            assert callable(getattr(ui, method)), method

        menu_ids = [defn.id for defn in captured["menu"]]
        assert "nvidia" not in menu_ids  # already configured
        assert "openai" in menu_ids
        assert "Setup complete: openai" in _status_text(screen)


@pytest.mark.asyncio
async def test_rerun_setup_uses_full_provider_menu(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    captured: dict = {}
    facade = ServiceFacade(relay_instance=make_relay([]))

    def fake_run_setup(ui, *, menu=None, store=None):
        captured["menu"] = menu
        return type(
            "SetupResult",
            (),
            {"completed": True, "usable": False, "configured": [], "state": "incomplete"},
        )()

    monkeypatch.setattr(facade, "run_setup", fake_run_setup)

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_providers(pilot, facade)
        await screen._on_rerun_setup()
        await pilot.pause()

        menu_ids = [defn.id for defn in captured["menu"]]
        assert menu_ids == [
            "nvidia",
            "openai",
            "anthropic",
            "gemini",
            "lmstudio",
            "ollama",
        ]
        assert "without usable providers" in _status_text(screen)


@pytest.mark.asyncio
async def test_rescan_selected_provider_writes_snapshot(monkeypatch, tmp_path):
    from app.providers.base import ModelProbe
    from app.services import platform_store
    from app.setup import persistence

    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    monkeypatch.setattr(persistence, "state_dir", tmp_path)
    monkeypatch.setattr(platform_store, "state_dir", tmp_path)

    relay = make_relay(
        [FakeProvider("NVIDIA", api_key="k", models=["m1", "m2"])]
    )
    relay.chat_service.registry.register(
        "NVIDIA", FakeClient(probe_result=ModelProbe(True, 12))
    )
    facade = ServiceFacade(relay_instance=relay)

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_providers(pilot, facade)
        assert screen._selected == "nvidia"
        await screen._on_rescan()
        await pilot.pause()

        assert "Scanned NVIDIA NIM: 2/2 available" in _status_text(screen)
        statuses = persistence.read_model_status(tmp_path / "platform.db")
        assert statuses == {"nvidia": {"m1": "available", "m2": "available"}}


def test_setup_adapter_progress_emits_notices():
    from app.providers.availability import AVAILABLE, UNAVAILABLE
    from app.setup.scan import ScanResult

    adapter = SetupAdapter(object())  # type: ignore[arg-type]
    reporter = adapter.progress()

    reporter.begin_scan(3)
    reporter.update(1, 3, "m1", [])
    reporter.update(2, 3, "m2", [])
    reporter.update(3, 3, "m3", [])

    results = [
        ScanResult(model="m1", status=AVAILABLE, latency_ms=1, status_code=200, error=None),
        ScanResult(model="m2", status=UNAVAILABLE, latency_ms=0, status_code=None, error="boom"),
        ScanResult(model="m3", status=AVAILABLE, latency_ms=2, status_code=200, error=None),
    ]
    reporter.end_scan(results)

    assert "Scanning availability: 1/3 \u2014 m1" in adapter.notices
    assert "Scanning availability: 3/3 \u2014 m3" in adapter.notices
    assert not any("m2" in line for line in adapter.notices)  # throttled mid-update
    assert "Scan complete: 2 available, 1 unavailable" in adapter.notices
    assert ("update", 2, 3, "m2") in reporter.transcript  # recording preserved


@pytest.mark.asyncio
async def test_rescan_progress_does_not_clobber_final_status(monkeypatch, tmp_path):
    from app.services import setup_state

    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    facade = ServiceFacade(relay_instance=make_relay(
        [FakeProvider("NVIDIA", api_key="k", models=["m1", "m2"])]
    ))

    delivered = []

    def fake_rescan(defn_id, on_progress=None):
        assert callable(on_progress)
        on_progress(1, 2, "m1")
        on_progress(2, 2, "m2")
        delivered.append(True)
        return {
            "ok": True,
            "provider": "NVIDIA NIM",
            "models": 2,
            "available": 2,
            "unavailable": 0,
        }

    monkeypatch.setattr(facade, "rescan_models", fake_rescan)

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_providers(pilot, facade)
        await screen._on_rescan()
        await pilot.pause()
        await pilot.pause()

        assert delivered
        # queued progress updates must not overwrite the final result
        assert "Scanned NVIDIA NIM: 2/2 available." in _status_text(screen)


@pytest.mark.asyncio
async def test_toggle_provider_persists(monkeypatch, tmp_path):
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
        screen = await _open_providers(pilot, facade)
        await screen._on_toggle_provider()
        await pilot.pause()

        assert captured["toggle"] == ("nvidia", False)
        assert "saved and applied" in _status_text(screen)


@pytest.mark.asyncio
async def test_setup_adapter_prompt_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    facade = ServiceFacade(relay_instance=make_relay([]))

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_providers(pilot, facade)
        adapter = SetupAdapter(screen)
        loop = asyncio.get_event_loop()
        future = loop.run_in_executor(
            None, lambda: adapter.ask("Type a value", "dflt")
        )

        await _wait_until(pilot, lambda: isinstance(app.screen, PromptScreen))
        await pilot.press("h", "i")
        await pilot.press("enter")

        result = await asyncio.wait_for(future, timeout=5.0)
        assert result == "hi"
        assert isinstance(app.screen, ProvidersScreen)


@pytest.mark.asyncio
async def test_setup_adapter_masks_key_input(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    facade = ServiceFacade(relay_instance=make_relay([]))

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_providers(pilot, facade)
        adapter = SetupAdapter(screen)
        loop = asyncio.get_event_loop()
        future = loop.run_in_executor(
            None, lambda: adapter.ask("API key for OpenAI (blank to skip)", "")
        )

        try:
            await _wait_until(pilot, lambda: isinstance(app.screen, PromptScreen))
            modal = app.screen
            from textual.widgets import Input

            assert modal.query_one("#prompt-input", Input).password is True
            await pilot.press("escape")
        finally:
            if isinstance(app.screen, PromptScreen):
                await pilot.press("escape")

        result = await asyncio.wait_for(future, timeout=5.0)
        assert result == ""  # cancel resolves as an empty key
        assert isinstance(app.screen, ProvidersScreen)


@pytest.mark.asyncio
async def test_setup_adapter_invalid_input_shows_feedback(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_state, "state_dir", tmp_path / ".relay")
    facade = ServiceFacade(relay_instance=make_relay([]))

    app = RelayApp(facade=facade, start_server=False)
    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        screen = await _open_providers(pilot, facade)
        adapter = SetupAdapter(screen)
        loop = asyncio.get_event_loop()
        future = loop.run_in_executor(
            None, lambda: adapter.ask_yes_no("Enable nvidia?", True)
        )

        await _wait_until(pilot, lambda: isinstance(app.screen, PromptScreen))
        modal = app.screen
        await pilot.press("x")
        await pilot.press("enter")
        await pilot.pause()

        error = modal.query_one("#prompt-error", Static)
        assert "y or n" in str(error.render())
        assert isinstance(app.screen, PromptScreen)  # modal stays open

        await pilot.press("backspace")  # clear the invalid "x", value kept
        await pilot.press("y")
        await pilot.press("enter")

        result = await asyncio.wait_for(future, timeout=5.0)
        assert result is True
        assert isinstance(app.screen, ProvidersScreen)


# --------------------------------------------------------- key validation

@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    from app.services import setup_state
    from app.services import platform_store
    from app.setup import persistence

    monkeypatch.setattr(setup_state, "state_dir", tmp_path)
    monkeypatch.setattr(persistence, "state_dir", tmp_path)
    monkeypatch.setattr(platform_store, "state_dir", tmp_path)
    return tmp_path


def test_provider_flow_validates_key_before_persist(monkeypatch, isolated_state):
    from app.providers.availability import AVAILABLE
    from app.providers.base import ModelProbe
    from app.providers.openai_client import OpenAIClient
    from app.setup.ui import ScriptedUI

    def fake_key_check(self, provider):
        if provider.api_key == "sk-bad":
            return 401, "invalid key"
        return 200, "ok"

    monkeypatch.setattr(OpenAIClient, "key_check", fake_key_check)
    monkeypatch.setattr(
        OpenAIClient, "list_models", lambda self, provider: ["m1"]
    )
    monkeypatch.setattr(
        OpenAIClient,
        "probe_model",
        lambda self, provider, model: ModelProbe(True, 5),
    )

    store = FakeStore()
    facade = ServiceFacade(relay_instance=make_relay([]), store=store)
    ui = ScriptedUI(["sk-bad", "r", "sk-good", "n", "n"])

    assert facade.configure_provider(ui, "openai") is True
    assert store.writes == [
        ("openai", {"enabled": True, "api_key": "sk-good"})
    ]
    assert any("Authentication successful" in n for n in ui.notices)

    # A skipped invalid key never persists.
    store2 = FakeStore()
    facade2 = ServiceFacade(relay_instance=make_relay([]), store=store2)
    ui2 = ScriptedUI(["sk-bad", "s"])

    assert facade2.configure_provider(ui2, "openai") is False
    assert store2.writes == []
