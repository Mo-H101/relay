"""
Availability snapshot persistence (``.relay/availability.json``).

Bounded by design: only the latest snapshot per provider is kept; raw
history moves to the ``relay.db`` ``availability`` table in P6 (see
``docs/platform-db-schema.md``). Writes are atomic (tmp + rename).
"""

import json
import time

from app.core.config import state_dir

SCHEMA = 1
_FILE = "availability.json"


def _path():
    return state_dir / _FILE


def read_all() -> dict:
    """
    Return the full snapshot document with safe defaults.
    """
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
