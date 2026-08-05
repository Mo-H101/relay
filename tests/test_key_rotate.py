"""
P6.2 key rotation tests (D4).

Covers ``KeyStore.rotate`` round-trips (new key accepted, old key revoked),
the ``relay keys rotate`` CLI surface (new key printed once, ``--yes``
guard, revoked/unknown handling), and the ``POST /admin/keys/{id}/rotate``
endpoint (new key once, 404/409, admin scope, ``key.rotate`` audit event,
audit-failure 500).
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.security.auth import _reset_key_store
from app.services.key_store import KeyStore
from app.services.metrics import relay_metrics

_AUTH = {"Authorization": "Bearer bootstrap-secret"}


# ------------------------------------------------------------------ store

@pytest.fixture
def store(tmp_path):
    instance = KeyStore(tmp_path / "relay_keys.db")
    yield instance
    instance.close()


def test_rotate_rejects_old_and_accepts_new(store):
    key_id, raw = store.create("ci", scopes=["chat", "v1"])
    new_id, new_raw = store.rotate(key_id)

    assert new_id != key_id
    assert new_raw != raw
    assert store.verify(new_raw)["id"] == new_id

    classified = store.classify(raw)
    assert classified["status"] == "revoked"
    assert store.verify(raw) is None


def test_rotate_copies_label_scopes_and_expiry(store):
    import time

    expires_at = time.time() + 3600
    key_id, _ = store.create("ci", scopes=["chat", "v1"], expires_at=expires_at)
    new_id, _ = store.rotate(key_id)

    meta = store.get_by_id(new_id)
    assert meta["label"] == "ci"
    assert meta["scopes"] == ["chat", "v1"]
    assert meta["expires_at"] == expires_at


def test_rotate_unknown_key_returns_none(store):
    assert store.rotate("does-not-exist") is None


def test_rotate_revoked_key(store):
    key_id, _ = store.create("ci")
    store.revoke(key_id)
    result = store.rotate(key_id)
    # The store rotation does not gate on revocation state (the CLI and
    # API layers refuse); it returns a fresh key and revokes again (no-op).
    assert result is not None
    assert store.get_by_id(key_id)["revoked_at"] is not None


# ------------------------------------------------------------------- CLI

@pytest.fixture
def run_cli(capsys):
    from app.cli import main

    def _run(argv):
        main(argv)
        out, err = capsys.readouterr()
        return out, err

    return _run


@pytest.fixture
def cli_store(monkeypatch, tmp_path):
    instance = KeyStore(tmp_path / "relay_keys.db")
    monkeypatch.setattr("app.cli.keys._store", lambda: instance)
    yield instance
    instance.close()


def test_cli_rotate_prints_new_key_once_and_revokes_old(cli_store, run_cli):
    key_id, raw = cli_store.create("ci", scopes=["chat"])
    out, _ = run_cli(["keys", "rotate", key_id, "--yes"])

    new_raw = next(
        line for line in out.splitlines() if line.startswith("API Key: ")
    ).split(": ", 1)[1]

    assert out.count(new_raw) == 1
    assert raw not in out
    assert cli_store.verify(new_raw) is not None
    assert cli_store.verify(raw) is None
    assert cli_store.get_by_id(key_id)["revoked_at"] is not None


def test_cli_rotate_json_includes_new_key_once(cli_store, run_cli):
    key_id, raw = cli_store.create("ci")
    out, _ = run_cli(["keys", "rotate", key_id, "--yes", "--json"])

    payload = json.loads(out)
    new_raw = payload["api_key"]
    assert new_raw.startswith("rl_")
    assert out.count(new_raw) == 1
    assert raw not in out


def test_cli_rotate_requires_yes_noninteractive(cli_store, run_cli):
    key_id, raw = cli_store.create("ci")

    with pytest.raises(SystemExit) as exc:
        run_cli(["keys", "rotate", key_id])
    assert exc.value.code == 1
    assert cli_store.verify(raw) is not None


def test_cli_rotate_unknown_key(cli_store, run_cli):
    with pytest.raises(SystemExit) as exc:
        run_cli(["keys", "rotate", "deadbeef", "--yes"])
    assert exc.value.code == 1


def test_cli_rotate_refuses_revoked_key(cli_store, run_cli):
    key_id, _ = cli_store.create("ci")
    cli_store.revoke(key_id)

    with pytest.raises(SystemExit) as exc:
        run_cli(["keys", "rotate", key_id, "--yes"])
    assert exc.value.code == 1


def test_cli_rotate_accepts_shortened_id(cli_store, run_cli):
    key_id, raw = cli_store.create("ci")
    out, _ = run_cli(["keys", "rotate", key_id[:8], "--yes"])
    assert cli_store.verify(raw) is None


# ------------------------------------------------------------------- API

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
def api_store(monkeypatch, tmp_path):
    instance = KeyStore(tmp_path / "relay_keys.db")
    monkeypatch.setattr("app.security.auth._key_store", lambda: instance)
    yield instance
    instance.close()


def test_api_rotate_returns_new_key_once(admin, api_store, client, isolated_event_log):
    key_id, raw = api_store.create("ci", scopes=["chat"])
    response = client.post(f"/admin/keys/{key_id}/rotate", headers=_AUTH)
    assert response.status_code == 200

    body = response.json()
    new_raw = body["key"]
    assert body["id"] != key_id
    assert body["label"] == "ci"
    assert new_raw.startswith("rl_")
    assert new_raw != raw

    listing = client.get("/admin/keys", headers=_AUTH).json()
    assert new_raw not in str(listing)

    assert api_store.verify(new_raw) is not None
    assert api_store.verify(raw) is None

    events = isolated_event_log.query(action="key.rotate")
    assert len(events) == 1
    assert events[0]["actor"] == "bootstrap"
    assert events[0]["target"] == key_id
    assert events[0]["detail"]["new_key_id"] == body["id"]


def test_api_rotate_unknown_is_404(admin, api_store, client):
    response = client.post(
        "/admin/keys/0000000000000000/rotate", headers=_AUTH
    )
    assert response.status_code == 404


def test_api_rotate_revoked_is_409(admin, api_store, client):
    key_id, _ = api_store.create("ci")
    api_store.revoke(key_id)
    response = client.post(f"/admin/keys/{key_id}/rotate", headers=_AUTH)
    assert response.status_code == 409
    assert response.json()["detail"] == "Key already revoked."


def test_api_rotate_requires_admin_scope(api_store, client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "")
    monkeypatch.setattr(settings, "relay_auth_store", True)
    key_id, raw = api_store.create("chat-only", scopes=["chat", "v1"])
    response = client.post(
        f"/admin/keys/{key_id}/rotate",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert response.status_code == 403


def test_api_rotate_audit_failure_returns_500(admin, api_store, client, monkeypatch):
    def _broken(action, **kwargs):
        raise RuntimeError("audit db gone")

    monkeypatch.setattr(
        "app.services.event_log.event_log",
        lambda: type("Broken", (), {"emit": _broken})(),
    )

    key_id, _ = api_store.create("ci")
    response = client.post(f"/admin/keys/{key_id}/rotate", headers=_AUTH)
    assert response.status_code == 500
    assert response.json()["detail"] == "Audit write failed."
