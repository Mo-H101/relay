"""
Availability snapshot persistence (``.relay/availability.json``).

Bounded by design: only the latest snapshot per provider is kept. The
canonical ``model_status`` mapping (``available``/``degraded``/
``unavailable``) is seeded into ``platform.db`` at migration time (P6.1);
``availability.json`` stays the live setup-scan source until P6.3 (see
``docs/platform-db-schema.md``). Writes are atomic (tmp + rename).
"""

import json
import time
from pathlib import Path
from typing import Optional

from app.core.config import state_dir

SCHEMA = 1
_FILE = "availability.json"

# Canonical platform status mapping (D4): the snapshot uses
# ``available``/``overloaded``/``unavailable``; ``platform.db`` stores
# ``available``/``degraded``/``unavailable`` with ``overloaded`` mapped
# to ``degraded`` at import time.
_STATUS_MAP = {
    "available": "available",
    "overloaded": "degraded",
    "unavailable": "unavailable",
}


def _path():
    return state_dir / _FILE


def read_all(path: Optional[Path] = None) -> dict:
    """
    Return the full snapshot document with safe defaults. ``path`` is
    optional (defaults to the configured state directory), which lets the
    migration read an alternative availability file (P6.1 ``--state-dir``).
    """
    if path is None:
        path = _path()

    if not path.exists():
        return {"schema": SCHEMA, "generated_at": None, "providers": {}}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": SCHEMA, "generated_at": None, "providers": {}}

    if not isinstance(data, dict):
        data = {}

    providers = data.get("providers")

    if not isinstance(providers, dict):
        providers = {}

    return {
        "schema": data.get("schema", SCHEMA),
        "generated_at": data.get("generated_at"),
        "providers": providers,
    }


def read_snapshot(provider_id: str):
    """
    Return the latest snapshot for one provider, or None.
    """
    return read_all()["providers"].get(provider_id)


def write_snapshot(provider_id: str, results) -> None:
    """
    Replace the latest snapshot for ``provider_id`` atomically.
    """
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = read_all()
    payload["schema"] = SCHEMA
    payload["generated_at"] = time.time()
    payload["providers"][provider_id] = {
        "generated_at": time.time(),
        "models": [
            {
                "model": result.model,
                "status": result.status,
                "latency_ms": result.latency_ms,
                "status_code": result.status_code,
                "error": result.error,
                "probed_at": time.time(),
            }
            for result in results
        ],
    }

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def iter_model_status(path: Optional[Path] = None):
    """
    Yield ``model_status`` import rows from an availability snapshot.

    Read-only hook for ``relay migrate`` (P6.1). ``overloaded`` maps to
    ``degraded`` (D4); missing/unknown statuses default to ``unavailable``
    rather than being skipped. ``path`` defaults to the configured state
    directory's snapshot; the migration passes the specific legacy file it
    is importing. Nothing here writes state.
    """
    for provider_id, snapshot in read_all(path)["providers"].items():
        generated_at = snapshot.get("generated_at")

        for model in snapshot.get("models", []):
            yield {
                "provider": provider_id,
                "model": model.get("model"),
                "status": _STATUS_MAP.get(model.get("status"), "unavailable"),
                "latency_ms": model.get("latency_ms"),
                "status_code": model.get("status_code"),
                "error": model.get("error"),
                "probed_at": model.get("probed_at"),
                "updated_at": generated_at,
            }
