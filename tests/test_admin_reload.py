"""
HTTP tests for the admin hot-reload endpoint (Phase 6E).

The endpoint delegates to reload_config and only maps outcomes to HTTP
status codes, so reload_config itself is mocked here to keep the tests
hermetic. Behavioral coverage of reload_config lives in test_reload.py.
"""

import pytest
from fastapi.testclient import TestClient

import app.api.admin as admin_module
from app.core.config import settings
from app.main import app as fastapi_app


@pytest.fixture
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


def _reload_result(**overrides):
    result = {
        "reloaded": True,
        "dry_run": False,
        "applied": ["request_timeout"],
        "unchanged": ["max_retries"],
        "failures": [],
    }
    result.update(overrides)
    return result


def test_reload_requires_api_key_when_enabled(client, monkeypatch):
    monkeypatch.setattr(settings, "relay_api_key", "secret-token")

    response = client.post("/admin/reload")

    assert response.status_code == 401
    assert "secret-token" not in str(response.json())


def test_reload_accepts_bearer_token(client, monkeypatch):
    monkeypatch.setattr(settings, "relay_api_key", "secret-token")
    monkeypatch.setattr(
        admin_module, "reload_config", lambda relay, **kwargs: _reload_result()
    )

    response = client.post(
        "/admin/reload",
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 200


def test_reload_success_shape_and_delegation(client, monkeypatch):
    captured = {}

    def fake_reload_config(relay, *, dry_run=False, dotenv_path=None):
        captured["dry_run"] = dry_run
        captured["dotenv_path"] = dotenv_path
        return _reload_result()

    monkeypatch.setattr(admin_module, "reload_config", fake_reload_config)

    response = client.post("/admin/reload")

    assert response.status_code == 200
    body = response.json()
    assert body["reloaded"] is True
    assert body["applied"] == ["request_timeout"]
    assert body["unchanged"] == ["max_retries"]
    assert body["failures"] == []
    assert captured["dry_run"] is False
    assert captured["dotenv_path"].endswith(".env")


def test_dry_run_query_passes_through(client, monkeypatch):
    captured = {}

    def fake_reload_config(relay, *, dry_run=False, dotenv_path=None):
        captured["dry_run"] = dry_run
        return _reload_result(dry_run=dry_run)

    monkeypatch.setattr(admin_module, "reload_config", fake_reload_config)

    response = client.post("/admin/reload?dry_run=true")

    assert response.status_code == 200
    assert captured["dry_run"] is True
    assert response.json()["dry_run"] is True


def test_validation_failure_maps_to_400(client, monkeypatch):
    monkeypatch.setattr(
        admin_module,
        "reload_config",
        lambda relay, **kwargs: _reload_result(
            reloaded=False,
            applied=[],
            unchanged=[],
            error_kind="validation",
            error="Invalid value for REQUEST_TIMEOUT",
        ),
    )

    response = client.post("/admin/reload")

    assert response.status_code == 400
    assert response.json()["error_kind"] == "validation"


def test_apply_failure_maps_to_500(client, monkeypatch):
    monkeypatch.setattr(
        admin_module,
        "reload_config",
        lambda relay, **kwargs: _reload_result(
            reloaded=False,
            applied=[],
            unchanged=[],
            error_kind="apply",
            error="rollback complete",
        ),
    )

    response = client.post("/admin/reload")

    assert response.status_code == 500
    assert response.json()["error_kind"] == "apply"


def test_unexpected_exception_maps_to_500(client, monkeypatch):
    def boom(relay, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(admin_module, "reload_config", boom)

    response = client.post("/admin/reload")

    assert response.status_code == 500
    body = response.json()
    assert body["reloaded"] is False
    assert "boom" not in str(body)


def test_failure_response_shape_is_bounded(client, monkeypatch):
    monkeypatch.setattr(
        admin_module,
        "reload_config",
        lambda relay, **kwargs: _reload_result(
            reloaded=False,
            applied=[],
            unchanged=[],
            error_kind="apply",
            error="rollback complete",
        ),
    )

    response = client.post("/admin/reload")

    assert response.status_code == 500
    assert set(response.json().keys()) == {
        "reloaded",
        "dry_run",
        "applied",
        "unchanged",
        "failures",
        "error_kind",
        "error",
    }
