"""
Tests for P5 Phase 4 store-backed authentication.

Covers the tier-2 KeyStore lookup added to ``require_api_key``: valid
store keys, scope enforcement, expiry/revocation, store outages, and
metric accounting. Regression tests for the unchanged bootstrap tier
live in ``test_auth.py``; these tests assert the tier-1 path stays
byte-identical when the store tier is enabled alongside it.

The store is injected per-test through ``app.security.auth._key_store``
(the same hook the auth dependency resolves at request time), pointing
at a temp-path KeyStore.
"""

import time

import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.security.auth import _reset_key_store
from app.services.key_store import KeyStore, KeyStoreError
from app.services.metrics import relay_metrics

ADMIN_PATHS = ["/admin/keys"]
CHAT_PATHS = ["/v1/models"]
OPEN_PATHS = ["/providers", "/metrics", "/diagnostics", "/health/deep"]


@pytest.fixture(autouse=True)
def reset_state():
    relay_metrics.reset()
    _reset_key_store()
    yield
    relay_metrics.reset()
    _reset_key_store()


@pytest.fixture
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


@pytest.fixture
def store(monkeypatch, tmp_path):
    instance = KeyStore(tmp_path / "relay_keys.db")
    monkeypatch.setattr("app.security.auth._key_store", lambda: instance)
    yield instance
    instance.close()


@pytest.fixture
def store_auth(monkeypatch, store):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "")
    monkeypatch.setattr(settings, "relay_auth_store", True)
    return store


def _create_key(store, scopes=None, expires_at=None):
    return store.create("test", scopes=scopes, expires_at=expires_at)


# -------------------------------------------------- store-backed auth


def test_store_key_grants_access(store_auth, client):
    _, raw_key = _create_key(store_auth)
    response = client.get(
        "/providers", headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert response.status_code == 200


def test_store_key_via_x_relay_header(store_auth, client):
    _, raw_key = _create_key(store_auth)
    response = client.get(
        "/providers", headers={"X-Relay-API-Key": raw_key}
    )
    assert response.status_code == 200


def test_unknown_store_key_is_401(store_auth, client):
    response = client.get(
        "/providers", headers={"Authorization": "Bearer rl_wrong"}
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_store_auth_records_key_metric(store_auth, client):
    key_id, raw_key = _create_key(store_auth)
    client.get("/providers", headers={"Authorization": f"Bearer {raw_key}"})
    assert relay_metrics.auth_by_key.value(key_id=key_id) == 1


def test_bootstrap_key_works_when_store_enabled(store_auth, client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "bootstrap-secret")
    response = client.get(
        "/providers", headers={"Authorization": "Bearer bootstrap-secret"}
    )
    assert response.status_code == 200


def test_bootstrap_key_is_not_counted_per_key(store_auth, client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "bootstrap-secret")
    _, raw_key = _create_key(store_auth)
    client.get("/providers", headers={"Authorization": "Bearer bootstrap-secret"})
    client.get("/providers", headers={"Authorization": f"Bearer {raw_key}"})
    assert relay_metrics.auth_by_key.total() == 1


def test_bootstrap_always_grants_full_access(store_auth, client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "bootstrap-secret")
    for path in ADMIN_PATHS + CHAT_PATHS + OPEN_PATHS:
        response = client.get(
            path, headers={"Authorization": "Bearer bootstrap-secret"}
        )
        assert response.status_code == 200


# ------------------------------------------------------------ scopes


def test_empty_scopes_grant_full_access(store_auth, client):
    _, raw_key = _create_key(store_auth, scopes=[])
    for path in ADMIN_PATHS + CHAT_PATHS + OPEN_PATHS:
        response = client.get(
            path, headers={"Authorization": f"Bearer {raw_key}"}
        )
        assert response.status_code == 200, path


def test_admin_scope_covers_admin_paths(store_auth, client):
    _, raw_key = _create_key(store_auth, scopes=["admin"])
    for path in ADMIN_PATHS:
        response = client.get(
            path, headers={"Authorization": f"Bearer {raw_key}"}
        )
        assert response.status_code == 200


def test_admin_scope_rejected_on_chat(store_auth, client):
    _, raw_key = _create_key(store_auth, scopes=["admin"])
    response = client.get(
        "/v1/models", headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert response.status_code == 403


def test_chat_scope_covers_chat_paths(store_auth, client):
    _, raw_key = _create_key(store_auth, scopes=["chat", "v1"])
    for path in CHAT_PATHS:
        response = client.get(
            path, headers={"Authorization": f"Bearer {raw_key}"}
        )
        assert response.status_code == 200


def test_chat_scope_covers_feedback(store_auth, client):
    _, raw_key = _create_key(store_auth, scopes=["chat", "v1"])
    response = client.post(
        "/feedback",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={"provider": "openai", "model": "gpt-4o", "rating": 5},
    )
    assert response.status_code == 202


def test_chat_scope_rejected_on_admin(store_auth, client):
    _, raw_key = _create_key(store_auth, scopes=["chat", "v1"])
    response = client.get(
        "/admin/keys", headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert response.status_code == 403


def test_v1_only_scope_rejected_on_chat(store_auth, client):
    _, raw_key = _create_key(store_auth, scopes=["v1"])
    response = client.get(
        "/v1/models", headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert response.status_code == 403


def test_scope_failure_does_not_leak_scopes(store_auth, client):
    _, raw_key = _create_key(store_auth, scopes=["chat", "v1"])
    response = client.get(
        "/admin/keys", headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert response.json() == {"detail": "Forbidden"}


# -------------------------------------------------- expiry / revocation


def test_revoked_store_key_is_401(store_auth, client):
    key_id, raw_key = _create_key(store_auth)
    store_auth.revoke(key_id)
    response = client.get(
        "/providers", headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert response.status_code == 401


def test_expired_store_key_is_401(store_auth, client):
    _, raw_key = _create_key(store_auth, expires_at=time.time() - 10)
    response = client.get(
        "/providers", headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert response.status_code == 401


def test_revoked_key_other_key_still_works(store_auth, client):
    revoked_id, revoked_raw = _create_key(store_auth)
    _, active_raw = _create_key(store_auth)
    store_auth.revoke(revoked_id)
    assert client.get(
        "/providers", headers={"Authorization": f"Bearer {active_raw}"}
    ).status_code == 200
    assert client.get(
        "/providers", headers={"Authorization": f"Bearer {revoked_raw}"}
    ).status_code == 401


# ----------------------------------------------------- failure modes


def test_store_open_failure_fails_closed(monkeypatch, client):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "")
    monkeypatch.setattr(settings, "relay_auth_store", True)

    def _broken_store():
        raise KeyStoreError("db unavailable")

    monkeypatch.setattr("app.security.auth._key_store", _broken_store)

    response = client.get(
        "/providers", headers={"Authorization": "Bearer rl_anything"}
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


# ------------------------------------------- auth events (P6.2, D8)

def test_store_outage_auth_failure_event_best_effort(
    monkeypatch, client, isolated_event_log
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "")
    monkeypatch.setattr(settings, "relay_auth_store", True)

    def _broken_store():
        raise KeyStoreError("db unavailable")

    monkeypatch.setattr("app.security.auth._key_store", _broken_store)

    response = client.get(
        "/providers", headers={"Authorization": "Bearer rl_anything"}
    )
    assert response.status_code == 401

    events = isolated_event_log.query(action="auth.failure")
    assert len(events) == 1
    assert events[0]["outcome"] == "failed"
    assert events[0]["detail"]["reason"] == "store_unavailable"


def test_store_outage_broken_audit_log_does_not_break_auth(
    monkeypatch, client, tmp_path
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "")
    monkeypatch.setattr(settings, "relay_auth_store", True)

    def _broken_store():
        raise KeyStoreError("db unavailable")

    monkeypatch.setattr("app.security.auth._key_store", _broken_store)

    # A real EventLog whose store is unavailable: emit() fails, bumps
    # relay_events_failed_total, and the auth hot path must not break.
    from app.services import event_log as event_log_module
    from app.services.event_log import EventLog

    broken = EventLog(str(tmp_path / "audit.db"))
    monkeypatch.setattr(broken, "_ensure_open", lambda: False)
    monkeypatch.setattr(event_log_module, "event_log", lambda: broken)

    response = client.get(
        "/providers", headers={"Authorization": "Bearer rl_anything"}
    )
    assert response.status_code == 401
    assert relay_metrics.events_failed.value() >= 1


def test_auth_success_event_for_store_key(store_auth, client, isolated_event_log):
    _, raw_key = _create_key(store_auth)
    response = client.get(
        "/providers", headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert response.status_code == 200

    events = isolated_event_log.query(action="auth.success")
    assert len(events) == 1
    assert events[0]["actor"] not in ("bootstrap", "")
    assert events[0]["detail"]["method"] in ("bearer", "x-relay-api-key")


def test_auth_success_event_for_bootstrap(
    store_auth, client, monkeypatch, isolated_event_log
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "bootstrap-secret")
    response = client.get(
        "/providers", headers={"Authorization": "Bearer bootstrap-secret"}
    )
    assert response.status_code == 200

    events = isolated_event_log.query(action="auth.success")
    assert any(e["actor"] == "bootstrap" for e in events)


def test_failure_bodies_are_identical(store_auth, client):
    key_id, revoked_raw = _create_key(store_auth)
    store_auth.revoke(key_id)
    _, expired_raw = _create_key(store_auth, expires_at=time.time() - 10)

    scenarios = [
        {},  # missing
        {"Authorization": "Bearer rl_totally-wrong"},  # invalid
        {"Authorization": f"Bearer {revoked_raw}"},  # revoked
        {"Authorization": f"Bearer {expired_raw}"},  # expired
    ]

    for headers in scenarios:
        response = client.get("/providers", headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "Unauthorized"}
        assert response.headers.get("www-authenticate") == "Bearer"


# ------------------------------------------------- backwards compatibility


def test_store_disabled_preserves_open_access(client):
    response = client.get("/providers")
    assert response.status_code == 200


def test_store_disabled_preserves_bootstrap_only(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "secret-token")
    monkeypatch.setattr(settings, "relay_auth_store", False)
    assert client.get("/providers").status_code == 401
    assert client.get(
        "/providers", headers={"Authorization": "Bearer secret-token"}
    ).status_code == 200
    assert client.get(
        "/providers", headers={"Authorization": "Bearer rl_anything"}
    ).status_code == 401


def test_public_paths_stay_public_with_store(store_auth, client):
    for path in ("/", "/health"):
        assert client.get(path).status_code == 200
