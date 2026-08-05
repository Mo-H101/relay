"""
Tests for the P5 Phase 4 administrative key API (``/admin/keys``).

Exercises create/list/inspect/revoke/permanent-delete against an
isolated temp-path KeyStore injected through ``app.security.auth
._key_store``. The bootstrap key authenticates the requests; the raw key
is asserted to appear only in the create response, never again.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.security.auth import _reset_key_store
from app.services.key_store import KeyStore
from app.services.metrics import relay_metrics

_AUTH = {"Authorization": "Bearer bootstrap-secret"}


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
def admin(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "bootstrap-secret")
    return _AUTH


@pytest.fixture
def store(monkeypatch, tmp_path):
    instance = KeyStore(tmp_path / "relay_keys.db")
    monkeypatch.setattr("app.security.auth._key_store", lambda: instance)
    yield instance
    instance.close()


# ------------------------------------------------------------ create


def test_create_returns_raw_key_exactly_once(admin, store, client):
    response = client.post(
        "/admin/keys",
        headers=_AUTH,
        json={"label": "ci", "scopes": ["chat", "v1"]},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["label"] == "ci"
    assert body["scopes"] == ["chat", "v1"]
    assert body["key"].startswith("rl_")
    assert len(body["key"]) == 46


def test_created_key_authenticates_end_to_end(admin, store, client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_auth_store", True)
    body = client.post(
        "/admin/keys", headers=_AUTH, json={"label": "ci"}
    ).json()
    response = client.get(
        "/providers", headers={"Authorization": f"Bearer {body['key']}"}
    )
    assert response.status_code == 200


def test_create_requires_label(admin, store, client):
    response = client.post("/admin/keys", headers=_AUTH, json={})
    assert response.status_code == 400
    assert response.json()["detail"] == "label is required."


def test_create_requires_label_object(admin, store, client):
    response = client.post("/admin/keys", headers=_AUTH, json={"label": 12})
    assert response.status_code == 400


def test_create_rejects_non_list_scopes(admin, store, client):
    response = client.post(
        "/admin/keys", headers=_AUTH, json={"label": "x", "scopes": "chat"}
    )
    assert response.status_code == 400


def test_create_rejects_unknown_scopes(admin, store, client):
    response = client.post(
        "/admin/keys", headers=_AUTH, json={"label": "x", "scopes": ["sudo"]}
    )
    assert response.status_code == 400
    assert "sudo" in response.json()["detail"]


def test_create_rejects_past_expiry(admin, store, client):
    response = client.post(
        "/admin/keys",
        headers=_AUTH,
        json={"label": "x", "expires_at": 1},
    )
    assert response.status_code == 400


def test_create_rejects_bad_json(admin, store, client):
    response = client.post(
        "/admin/keys",
        headers=_AUTH,
        content=b"not-json",
    )
    assert response.status_code == 400


# ----------------------------------------------------------- list


def test_list_returns_metadata_only(admin, store, client):
    key_id, _ = store.create("alpha", scopes=["chat"])
    response = client.get("/admin/keys", headers=_AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["keys"][0]["id"] == key_id
    assert body["keys"][0]["label"] == "alpha"
    assert "key" not in body["keys"][0]
    assert "key_hash" not in str(body)


def test_list_empty_store(admin, store, client):
    response = client.get("/admin/keys", headers=_AUTH)
    assert response.status_code == 200
    assert response.json() == {"total": 0, "keys": []}


# ---------------------------------------------------------- inspect


def test_inspect_returns_metadata(admin, store, client):
    key_id, _ = store.create("alpha", scopes=["chat", "v1"])
    response = client.get(f"/admin/keys/{key_id}", headers=_AUTH)
    assert response.status_code == 200
    assert response.json()["id"] == key_id
    assert response.json()["scopes"] == ["chat", "v1"]
    assert "key" not in response.json()


def test_inspect_unknown_is_404(admin, store, client):
    response = client.get("/admin/keys/0000000000000000", headers=_AUTH)
    assert response.status_code == 404


# ---------------------------------------------------------- revoke


def test_revoke_flips_key_to_inactive(admin, store, client):
    key_id, raw_key = store.create("alpha")
    response = client.delete(f"/admin/keys/{key_id}", headers=_AUTH)
    assert response.status_code == 200
    assert response.json() == {"revoked": True}
    assert store.get_by_id(key_id)["revoked_at"] is not None
    assert store.verify(raw_key) is None


def test_revoke_unknown_is_404(admin, store, client):
    response = client.delete("/admin/keys/0000000000000000", headers=_AUTH)
    assert response.status_code == 404


# ------------------------------------------------- permanent delete


def test_permanent_delete_removes_row(admin, store, client):
    key_id, _ = store.create("alpha")
    response = client.delete(
        f"/admin/keys/{key_id}?permanent=true", headers=_AUTH
    )
    assert response.status_code == 200
    assert response.json() == {"deleted": True}
    assert store.get_by_id(key_id) is None


def test_permanent_delete_of_revoked_key(admin, store, client):
    key_id, _ = store.create("alpha")
    store.revoke(key_id)
    response = client.delete(
        f"/admin/keys/{key_id}?permanent=true", headers=_AUTH
    )
    assert response.status_code == 200


def test_permanent_delete_unknown_is_404(admin, store, client):
    response = client.delete(
        "/admin/keys/0000000000000000?permanent=true", headers=_AUTH
    )
    assert response.status_code == 404


# ------------------------------------------------------- redaction


def test_raw_key_never_reappears(admin, store, client):
    created = client.post(
        "/admin/keys", headers=_AUTH, json={"label": "ci"}
    ).json()
    raw_key = created["key"]

    for call in (client.get("/admin/keys", headers=_AUTH),):
        assert raw_key not in str(call.json())

    key_id = created["id"]
    inspect = client.get(f"/admin/keys/{key_id}", headers=_AUTH)
    assert raw_key not in str(inspect.json())


def test_no_hash_or_salt_leak(admin, store, client):
    created = client.post(
        "/admin/keys", headers=_AUTH, json={"label": "ci"}
    ).json()
    listing = client.get("/admin/keys", headers=_AUTH)
    assert "key_hash" not in str(listing.json())
    assert "key_salt" not in str(listing.json())
    assert created["key"] not in str(listing.json())


# ----------------------------------------------------- metrics / scopes


def test_key_admin_metrics_recorded(admin, store, client):
    created = client.post(
        "/admin/keys", headers=_AUTH, json={"label": "ci"}
    ).json()
    client.get("/admin/keys", headers=_AUTH)
    client.delete(f"/admin/keys/{created['id']}", headers=_AUTH)
    assert relay_metrics.key_admin_actions.value(action="create", outcome="ok") == 1
    assert relay_metrics.key_admin_actions.value(action="list", outcome="ok") == 1
    assert relay_metrics.key_admin_actions.value(action="delete", outcome="ok") == 1


def test_key_admin_metrics_missing(admin, store, client):
    client.get("/admin/keys/0000000000000000", headers=_AUTH)
    assert relay_metrics.key_admin_actions.value(
        action="inspect", outcome="missing"
    ) == 1


def test_admin_keys_requires_admin_scope(store, client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "")
    monkeypatch.setattr(settings, "relay_auth_store", True)
    _, raw_key = store.create("chat-only", scopes=["chat", "v1"])
    response = client.get(
        "/admin/keys", headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert response.status_code == 403


def test_ops_event_recorded_for_create(admin, store, client):
    from app.services.ops_store import ops_store

    ops_store.clear()
    created = client.post(
        "/admin/keys", headers=_AUTH, json={"label": "ci"}
    ).json()
    events = [e for e in ops_store.events() if e.kind == "key_admin"]
    assert len(events) == 1
    assert events[0].route == "keys/create"
    assert events[0].key_id == created["id"]


# ---------------------------------------------- P6.2 audit surface


def test_create_records_key_create_event(admin, store, client, isolated_event_log):
    created = client.post(
        "/admin/keys", headers=_AUTH, json={"label": "ci"}
    ).json()

    events = isolated_event_log.query(action="key.create")
    assert len(events) == 1
    assert events[0]["actor"] == "bootstrap"
    assert events[0]["target"] == created["id"]
    assert events[0]["detail"]["label"] == "ci"
    assert created["key"] not in str(events[0])


def test_admin_events_endpoint_returns_newest_first(
    admin, store, client, isolated_event_log
):
    client.post("/admin/keys", headers=_AUTH, json={"label": "one"})
    client.post("/admin/keys", headers=_AUTH, json={"label": "two"})

    body = client.get("/admin/events", headers=_AUTH).json()
    assert body["total"] >= 2
    assert all(
        "action" in e and "actor" in e and "detail" in e
        for e in body["events"]
    )
    timestamps = [e["ts"] for e in body["events"]]
    assert timestamps == sorted(timestamps, reverse=True)


def test_admin_events_action_filter(admin, store, client, isolated_event_log):
    client.post("/admin/keys", headers=_AUTH, json={"label": "one"})

    body = client.get("/admin/events?action=key.create", headers=_AUTH).json()
    assert body["total"] == 1
    assert all(e["action"] == "key.create" for e in body["events"])

    body = client.get("/admin/events?action=key.prune", headers=_AUTH).json()
    assert body["total"] == 0
    assert body["events"] == []


def test_admin_events_invalid_outcome_is_400(admin, store, client):
    response = client.get("/admin/events?outcome=bogus", headers=_AUTH)
    assert response.status_code == 400
    assert "outcome" in response.json()["detail"]


def test_admin_events_requires_admin_scope(store, client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "")
    monkeypatch.setattr(settings, "relay_auth_store", True)
    _, raw_key = store.create("chat-only", scopes=["chat", "v1"])
    response = client.get(
        "/admin/events", headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert response.status_code == 403


def test_create_audit_failure_returns_500(admin, store, client, monkeypatch):
    from app.services import event_log as event_log_module
    from app.services.event_log import EventLog

    broken = EventLog(str(store.path).rsplit(".db", 1)[0] + "-audit.db")
    monkeypatch.setattr(broken, "_ensure_open", lambda: False)
    monkeypatch.setattr(event_log_module, "event_log", lambda: broken)

    response = client.post(
        "/admin/keys", headers=_AUTH, json={"label": "ci"}
    )
    assert response.status_code == 500
    assert response.json()["detail"] == "Audit write failed."


def test_revoke_audit_failure_returns_500(admin, store, client, monkeypatch):
    from app.services import event_log as event_log_module
    from app.services.event_log import EventLog

    key_id, _ = store.create("ci")
    broken = EventLog(str(store.path).rsplit(".db", 1)[0] + "-audit.db")
    monkeypatch.setattr(broken, "_ensure_open", lambda: False)
    monkeypatch.setattr(event_log_module, "event_log", lambda: broken)

    response = client.delete(f"/admin/keys/{key_id}", headers=_AUTH)
    assert response.status_code == 500
    assert response.json()["detail"] == "Audit write failed."


def test_rotate_audit_failure_returns_500(admin, store, client, monkeypatch):
    from app.services import event_log as event_log_module
    from app.services.event_log import EventLog

    key_id, _ = store.create("ci")
    broken = EventLog(str(store.path).rsplit(".db", 1)[0] + "-audit.db")
    monkeypatch.setattr(broken, "_ensure_open", lambda: False)
    monkeypatch.setattr(event_log_module, "event_log", lambda: broken)

    response = client.post(f"/admin/keys/{key_id}/rotate", headers=_AUTH)
    assert response.status_code == 500
    assert response.json()["detail"] == "Audit write failed."
