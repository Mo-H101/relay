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

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.core.config import PROJECT_ROOT
from app.core.relay import relay
from app.services.reload import reload_config

router = APIRouter()

_ENV_PATH = PROJECT_ROOT / ".env"


@router.post("/admin/reload")
def admin_reload(
    dry_run: bool = Query(
        False,
        description="Preview the applied/unchanged fields without mutating state.",
    ),
):
    """
    Hot-reload configuration from the project .env.

    Applies only the reloadable allowlist and never reports secret
    values. Validation failures return 400; rollback failures return 500.
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
        return JSONResponse(status_code=status_code, content=result)

    return result
