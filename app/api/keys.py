"""
Administrative API-key management endpoints (P5 Phase 4).

Exposes KeyStore lifecycle operations to operators who authenticate with
an admin-scoped (or bootstrap) key: create, list, inspect, revoke, and
permanently delete keys. The raw key is returned exactly once, from
``POST /admin/keys``; every other response carries metadata only and
never the hash or raw material.

All endpoints sit under ``/admin/keys`` and are therefore gated by the
``admin`` scope when store-backed authentication is enabled (the
bootstrap key always has full access). Responses never include the raw
key, the stored hash, or the reason behind a store failure.
"""

import time

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.security import auth as auth_module
from app.services.key_store import KeyStoreError
from app.services.metrics import relay_metrics
from app.services.ops_store import ops_store

router = APIRouter()

_VALID_SCOPES = frozenset({"admin", "chat", "v1"})


def _store():
    """
    Resolve the shared KeyStore through the auth module so tests can
    inject an isolated store with one monkeypatch.
    """
    return auth_module._key_store()


def _log():
    """
    Resolve the shared event log through the service module so tests can
    inject an isolated log with one monkeypatch.
    """
    from app.services import event_log as event_log_module

    return event_log_module.event_log()


def _actor_for(request: Request) -> str:
    """
    Opaque actor label for audit rows: the store key id that satisfied
    the request, or ``"bootstrap"`` for bootstrap-key requests. Never the
    raw key.
    """
    return request.scope.get("relay_key_id") or "bootstrap"


def _meta_public(meta: dict) -> dict:
    """
    Serialize one key's metadata for API responses. Opaque fields only;
    the hash and raw material are never included.
    """
    return {
        "id": meta["id"],
        "label": meta["label"],
        "scopes": meta["scopes"],
        "expires_at": meta["expires_at"],
        "expires_soon": bool(meta.get("expires_soon")),
        "created_at": meta["created_at"],
        "last_used_at": meta["last_used_at"],
        "revoked_at": meta["revoked_at"],
    }


def _audit_failure() -> JSONResponse:
    """
    Response for an admin mutation whose synchronous audit write failed.
    The action may already have happened; the 500 forces the operator to
    investigate rather than believe an un-recorded action succeeded.
    """
    return JSONResponse(
        status_code=500,
        content={"detail": "Audit write failed."},
    )


def _store_unavailable() -> JSONResponse:
    relay_metrics.record_key_action("key_store", "unavailable")
    return JSONResponse(
        status_code=500,
        content={"detail": "Key store unavailable."},
    )


@router.post("/admin/keys", status_code=201)
async def create_key(request: Request):
    """
    Create a new API key. Returns the raw key exactly once; store it now.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid JSON body."},
        )

    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=400,
            content={"detail": "Body must be a JSON object."},
        )

    label_raw = payload.get("label")

    if not isinstance(label_raw, str) or not label_raw.strip():
        return JSONResponse(
            status_code=400,
            content={"detail": "label is required."},
        )

    label = label_raw.strip()

    scopes = payload.get("scopes") or []

    if not isinstance(scopes, list) or not all(
        isinstance(scope, str) for scope in scopes
    ):
        return JSONResponse(
            status_code=400,
            content={"detail": "scopes must be a list of strings."},
        )

    scopes = list(dict.fromkeys(scope.strip() for scope in scopes))

    unknown = [scope for scope in scopes if scope not in _VALID_SCOPES]
    if unknown:
        return JSONResponse(
            status_code=400,
            content={"detail": f"unknown scopes: {', '.join(sorted(unknown))}"},
        )

    expires_at = payload.get("expires_at")

    if expires_at is not None:
        if not isinstance(expires_at, (int, float)):
            return JSONResponse(
                status_code=400,
                content={"detail": "expires_at must be a unix timestamp."},
            )
        if expires_at <= time.time():
            return JSONResponse(
                status_code=400,
                content={"detail": "expires_at must be in the future."},
            )

    try:
        key_id, raw_key = _store().create(
            label,
            scopes=scopes,
            expires_at=expires_at,
        )
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"detail": "label is required."},
        )
    except KeyStoreError:
        return _store_unavailable()

    relay_metrics.record_key_action("create", "ok")
    ops_store.record_key_action("create", key_id=key_id)

    try:
        _log().emit(
            "key.create",
            actor=_actor_for(request),
            target=key_id,
            outcome="ok",
            detail={"scope_count": len(scopes), "label": label},
            raise_on_error=True,
        )
    except Exception:  # noqa: BLE001 - audit failure surfaces as 500
        return _audit_failure()

    return {
        "id": key_id,
        "label": label,
        "scopes": scopes,
        "expires_at": expires_at,
        "created_at": time.time(),
        "key": raw_key,
    }


@router.get("/admin/keys")
def list_keys():
    """
    List every stored key's metadata, oldest created first.
    """
    try:
        entries = _store().list()
    except KeyStoreError:
        return _store_unavailable()

    relay_metrics.record_key_action("list", "ok")

    return {
        "total": len(entries),
        "keys": [_meta_public(entry) for entry in entries],
    }


@router.get("/admin/keys/{key_id}")
def get_key(key_id: str):
    """
    Inspect one key's metadata.
    """
    try:
        meta = _store().get_by_id(key_id)
    except KeyStoreError:
        return _store_unavailable()

    if meta is None:
        relay_metrics.record_key_action("inspect", "missing")
        return JSONResponse(
            status_code=404,
            content={"detail": "Key not found."},
        )

    relay_metrics.record_key_action("inspect", "ok")
    return _meta_public(meta)


@router.delete("/admin/keys/{key_id}")
def delete_key(
    request: Request,
    key_id: str,
    permanent: bool = Query(
        False,
        description="Permanently remove the key row instead of revoking it.",
    ),
):
    """
    Revoke a key (default) or permanently delete it (``?permanent=true``).
    """
    try:
        if permanent:
            deleted = _store().delete(key_id)
        else:
            revoked = _store().revoke(key_id)
    except KeyStoreError:
        return _store_unavailable()

    if not (deleted if permanent else revoked):
        relay_metrics.record_key_action("delete", "missing")
        return JSONResponse(
            status_code=404,
            content={"detail": "Key not found."},
        )

    relay_metrics.record_key_action("delete", "ok")
    ops_store.record_key_action("delete", key_id=key_id)

    action = "key.delete" if permanent else "key.revoke"

    try:
        _log().emit(
            action,
            actor=_actor_for(request),
            target=key_id,
            outcome="ok",
            raise_on_error=True,
        )
    except Exception:  # noqa: BLE001 - audit failure surfaces as 500
        return _audit_failure()

    if permanent:
        return {"deleted": True}

    return {"revoked": True}


@router.post("/admin/keys/{key_id}/rotate")
def rotate_key(request: Request, key_id: str):
    """
    Rotate a key: create a replacement and revoke the original. Returns
    the new raw key exactly once. A ``key.rotate`` event is written
    synchronously; an audit failure surfaces as 500.
    """
    try:
        meta = _store().get_by_id(key_id)
    except KeyStoreError:
        return _store_unavailable()

    if meta is None:
        relay_metrics.record_key_action("rotate", "missing")
        return JSONResponse(
            status_code=404,
            content={"detail": "Key not found."},
        )

    if meta["revoked_at"] is not None:
        relay_metrics.record_key_action("rotate", "denied")
        return JSONResponse(
            status_code=409,
            content={"detail": "Key already revoked."},
        )

    try:
        result = _store().rotate(key_id)
    except KeyStoreError:
        return _store_unavailable()

    if result is None:
        relay_metrics.record_key_action("rotate", "missing")
        return JSONResponse(
            status_code=404,
            content={"detail": "Key not found."},
        )

    new_id, raw_key = result
    relay_metrics.record_key_action("rotate", "ok")

    try:
        _log().emit(
            "key.rotate",
            actor=_actor_for(request),
            target=key_id,
            outcome="ok",
            detail={"new_key_id": new_id},
            raise_on_error=True,
        )
    except Exception:  # noqa: BLE001 - audit failure surfaces as 500
        return _audit_failure()

    return {
        "id": new_id,
        "label": meta["label"],
        "scopes": meta["scopes"],
        "expires_at": meta["expires_at"],
        "created_at": time.time(),
        "key": raw_key,
    }
