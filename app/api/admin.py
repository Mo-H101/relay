"""
Admin endpoints for Relay.

Exposes hot configuration reload via POST /admin/reload. The endpoint is
protected by the global Phase 6D API-key dependency like every other
route, so no per-route authentication is needed here.

Responses contain only field names and redacted errors; secrets (API
keys, proxy credentials, generated values) are never included. Invalid
configuration maps to HTTP 400; unexpected apply failures roll back and
map to HTTP 500.
"""

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.core.config import env_file
from app.core.relay import relay
from app.services.reload import reload_config

router = APIRouter()

_ENV_PATH = env_file


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
    the request, or ``"bootstrap"`` for bootstrap-key requests.
    """
    return request.scope.get("relay_key_id") or "bootstrap"


@router.post("/admin/reload")
def admin_reload(
    request: Request,
    dry_run: bool = Query(
        False,
        description="Preview the applied/unchanged fields without mutating state.",
    ),
):
    """
    Hot-reload configuration from the project .env.

    Applies only the reloadable allowlist and never reports secret
    values. Validation failures return 400; rollback failures return 500.
    A ``config.reload`` audit event is written best-effort.
    """
    try:
        result = reload_config(
            relay,
            dry_run=dry_run,
            dotenv_path=str(_ENV_PATH),
        )
    except Exception:
        result = {
            "reloaded": False,
            "dry_run": bool(dry_run),
            "applied": [],
            "unchanged": [],
            "failures": [],
            "error_kind": "apply",
            "error": "Reload failed unexpectedly.",
        }

    if not result.get("reloaded"):
        status_code = (
            400 if result.get("error_kind") == "validation" else 500
        )
    else:
        status_code = 200

    _emit_reload(request, result, status_code)

    if status_code != 200:
        return JSONResponse(status_code=status_code, content=result)

    return result


@router.get("/admin/events")
def list_events(
    action: str | None = Query(None, description="Filter by action."),
    outcome: str | None = Query(None, description="Filter by outcome."),
    limit: int = Query(50, ge=1, le=500, description="Max rows to return."),
):
    """
    Tail the durable security-event log, newest first.

    Rows are redacted at write time, so responses never carry secrets.
    ``action``/``outcome`` are optional bounded filters.
    """
    from app.services.event_log import _OUTCOMES

    if outcome is not None and outcome not in _OUTCOMES:
        return JSONResponse(
            status_code=400,
            content={"detail": "outcome must be one of: ok, failed, denied."},
        )

    try:
        rows = _log().query(action=action, outcome=outcome, limit=limit)
    except Exception:  # noqa: BLE001 - log outage surfaces as 500
        return JSONResponse(
            status_code=500,
            content={"detail": "Event log unavailable."},
        )

    return {"total": len(rows), "events": rows}


def _emit_reload(request: Request, result: dict, status_code: int) -> None:
    """
    Best-effort ``config.reload`` audit event. Never changes the reload
    response; a failed audit write only increments the failure counter.
    """
    from app.services import event_log as event_log_module

    try:
        event_log_module.event_log().emit(
            "config.reload",
            actor=_actor_for(request),
            outcome="ok" if status_code == 200 else "failed",
            detail={
                "dry_run": bool(result.get("dry_run")),
                "applied": len(result.get("applied") or []),
                "failures": len(result.get("failures") or []),
            },
        )
    except Exception:  # noqa: BLE001 - audit failure must not change reload
        pass
