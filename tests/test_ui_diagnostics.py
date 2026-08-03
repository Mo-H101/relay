"""
Diagnostics screen tests (Feature C of P2d).

Covers the ops/log tails, per-provider health deep view, explicit test
connection, and the export path. The export tests are the security gate:
any secret-shaped value in the snapshot must never reach the file.
"""

import json
import re

import pytest

from app.providers.base import ModelProbe
from app.services.ops_store import ops_store
from app.ui.data import ServiceFacade
from tests.ui_fakes import FakeClient, FakeProvider, FakeRelay, make_relay


@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    from app.services import setup_state
    from app.setup import persistence

    monkeypatch.setattr(setup_state, "state_dir", tmp_path)
    monkeypatch.setattr(persistence, "state_dir", tmp_path)
    return tmp_path


def _facade(relay: FakeRelay | None = None) -> ServiceFacade:
    return ServiceFacade(relay_instance=relay or FakeRelay())


def test_ops_tail_newest_first():
    ops_store.clear()
    ops_store.record_http("GET", "/health", 200, 5.0)
    ops_store.record_chat("/chat", False, "p1", "m1", True, False, 10.0, attempts=1)

    events = _facade().ops_tail(limit=10)

    assert len(events) == 2
    assert events[0].kind == "chat"
    assert events[0].provider == "p1"
    assert events[0].model == "m1"
    assert events[1].kind == "http"
    assert events[1].method == "GET"

    ops_store.clear()


def test_log_tail_unconfigured():
    result = _facade().log_tail()
    assert result["available"] is False
    assert result["entries"] == []


def test_log_tail_redacts_and_truncates(monkeypatch, tmp_path):
    from app.core.config import settings

    log_file = tmp_path / "relay.log"
    log_file.write_text(
        json.dumps(
            {
                "ts": "2026-01-01T00:00:00",
                "level": "INFO",
                "logger": "relay",
                "event": "chat",
                "data": {"api_key": "sk-abcdefghij", "model": "gpt-4o"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "log_file", str(log_file))

    result = _facade().log_tail(limit=10)

    assert result["available"] is True
    assert len(result["entries"]) == 1
    entry = result["entries"][0]
    assert entry.event == "chat"
    assert "sk-abcdefghij" not in entry.data
    assert "<redacted>" in entry.data
    assert len(entry.data) <= 200


def test_log_tail_missing_file(monkeypatch, tmp_path):
    from app.core.config import settings

    monkeypatch.setattr(settings, "log_file", str(tmp_path / "missing.log"))
    result = _facade().log_tail()
    assert result["available"] is False
    assert "not found" in result["error"]


def test_provider_health_deep_unknown_provider():
    deep = _facade().provider_health_deep("ghost")
    assert deep["found"] is False


def test_provider_health_deep_joins_report_and_snapshot(isolated_state):
    relay = make_relay([FakeProvider("NVIDIA", api_key="k", models=["m1", "m2"])])
    deep = _facade(relay).provider_health_deep("NVIDIA")

    assert deep["found"] is True
    assert deep["has_api_key"] is True
    assert {m["name"]: m["health"] for m in deep["models"]} == {
        "m1": "healthy",
        "m2": "healthy",
    }


def test_test_connection_unknown_provider():
    result = _facade().test_connection("ghost")
    assert result["ok"] is False
    assert "Unknown provider" in result["error"]


def test_test_connection_probes_chat_model():
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])
    relay.chat_service.registry.register(
        "p1", FakeClient(probe_result=ModelProbe(healthy=True, latency_ms=12))
    )

    result = _facade(relay).test_connection("p1")

    assert result["ok"] is True
    assert result["provider"] == "p1"
    assert result["model"] == "m1"
    assert result["status"] == "available"
    assert result["latency_ms"] == 12


def test_test_connection_skips_non_chat_models():
    relay = make_relay([FakeProvider("p1", api_key="k", models=["embedding-1"])])

    result = _facade(relay).test_connection("p1")

    assert result["ok"] is False
    assert "chat-testable" in result["error"]


def test_export_diagnostics_is_redacted_and_atomic(monkeypatch, tmp_path):
    from app.ui.data import DiagnosticsService

    snapshot = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "api_key": "sk-abcdefghijklmnop",
        "headers": {"Authorization": "Bearer secrettoken12345"},
        "x-relay-api-key": "nvapi-aaaaaaaaaaaaaaaa",
        "prompt": "write a poem about the sea",
        "providers": [],
    }
    monkeypatch.setattr(
        DiagnosticsService, "build_snapshot", lambda self, relay, task=None: snapshot
    )

    target = tmp_path / "diag.json"
    report = _facade().export_diagnostics(str(target))

    assert report["ok"] is True
    assert report["path"] == str(target)
    assert report["bytes"] > 0

    text = target.read_text(encoding="utf-8")
    for secret in (
        "sk-abcdefghijklmnop",
        "secrettoken12345",
        "nvapi-aaaaaaaaaaaaaaaa",
    ):
        assert secret not in text, secret
    assert "write a poem about the sea" in text
    assert "<redacted>" in text
    assert not (tmp_path / "diag.json.tmp").exists()
    json.loads(text)


def test_export_diagnostics_failure_reports_error(tmp_path):
    report = _facade().export_diagnostics(str(tmp_path / "nope" / "diag.json"))
    assert report["ok"] is False
    assert report["error"]


def test_export_diagnostics_end_to_end(tmp_path):
    report = _facade(FakeRelay()).export_diagnostics(str(tmp_path / "diag.json"))
    assert report["ok"] is True
    text = (tmp_path / "diag.json").read_text(encoding="utf-8")
    assert re.search(r"sk-[A-Za-z0-9_-]{8,}", text) is None
    assert "Bearer " not in text


@pytest.mark.asyncio
async def test_diagnostics_screen_smoke():
    from app.ui.app import RelayApp

    app = RelayApp(facade=_facade(), start_server=False)

    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        await pilot.press("7")
        await pilot.pause()
        assert app.screen.query_one("#ops-table") is not None
        assert app.screen.query_one("#log-table") is not None
        assert app.screen.query_one("#export-path") is not None
        await pilot.press("q")
        await pilot.pause()
