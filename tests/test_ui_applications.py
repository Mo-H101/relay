"""
Applications screen tests (Feature B of P2d).

Exercises the client-activity / auth-status view-models and the screen
smoke path. All assertions stay metadata-only: nothing here may ever
render an Authorization value, API key, body, prompt, or response.
"""

import pytest

from app.services.client_tracking import client_tracking
from app.services.metrics import relay_metrics
from app.ui.data import ServiceFacade
from tests.ui_fakes import FakeRelay


@pytest.fixture
def clear_tracking():
    client_tracking.clear()
    yield
    client_tracking.clear()


def _facade() -> ServiceFacade:
    return ServiceFacade(relay_instance=FakeRelay())


def test_client_activity_projects_rows(clear_tracking):
    client_tracking.record("cline", "Cline/3.0", "/chat", 200, "bearer")
    client_tracking.record("cline", "Cline/3.0", "/chat", 500, "bearer")
    client_tracking.record("opencode", "opencode/0.1", "/v1/chat/completions", 200, "none")

    rows = _facade().client_activity()

    assert len(rows) == 2
    cline = next(r for r in rows if r.bucket == "cline")
    assert cline.requests == 2
    assert cline.successes == 1
    assert cline.failures == 1
    assert cline.auth_schemes == ("bearer",)
    assert cline.ua == "Cline/3.0"


def test_client_activity_never_leaks_authorization_value(clear_tracking):
    client_tracking.record("cline", "Cline/3.0", "/chat", 200, "bearer")

    rendered = repr(_facade().client_activity())
    assert "Authorization" not in rendered
    assert "Bearer" not in rendered
    assert "sk-" not in rendered


def test_auth_status_reflects_metrics_and_tracking(clear_tracking, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "sk-test-key")

    before_failures = relay_metrics.auth_failures.total()
    relay_metrics.record_auth(enabled=True, granted=False, method="bearer")
    relay_metrics.record_auth(enabled=True, granted=True, method="header")
    client_tracking.record("opencode", "ua", "/chat", 200, "none")

    auth = _facade().auth_status()

    assert auth.enabled is True
    assert auth.failures == before_failures + 1
    assert auth.authenticated >= 1
    assert auth.by_method["header"] >= 1
    assert auth.by_reason["missing"] == relay_metrics.auth_failures.value(
        reason="missing"
    )
    assert auth.presented.get("none") == 1
    assert "sk-test-key" not in repr(auth)


def test_auth_status_disabled_when_no_key(clear_tracking, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "")
    assert _facade().auth_status().enabled is False


def test_endpoint_status_from_ops_window():
    from app.services.ops_store import ops_store

    ops_store.clear()
    ops_store.record_http("GET", "/health", 200, 5.0)
    ops_store.record_http("GET", "/health", 500, 8.0)

    status = _facade().endpoint_status()

    assert status["requests"] == 2
    assert status["successes"] == 1
    assert status["failures"] == 1

    ops_store.clear()


@pytest.mark.asyncio
async def test_applications_screen_smoke():
    from app.ui.app import RelayApp

    app = RelayApp(facade=_facade(), start_server=False)

    async with app.run_test(
        headless=True, size=(100, 30), notifications=False
    ) as pilot:
        await pilot.press("6")
        await pilot.pause()
        assert app.screen.query_one("#client-table") is not None
        assert app.screen.query_one("#auth-line") is not None
        await pilot.press("q")
        await pilot.pause()
