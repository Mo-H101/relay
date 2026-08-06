"""
Availability persistence (P6.5 F3).

Setup-scan results persist to the durable ``model_status`` table in the
shared ``platform.db``; the live ``availability.json`` snapshot-file write
was retired. ``read_all`` / ``iter_model_status`` remain as the read-only
legacy-import hooks for ``relay migrate`` (decision B), which can still
import a pre-existing ``availability.json`` file.
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


def _platform_path(path: Optional[Path] = None) -> str:
    """
    Resolve the platform database path, or a caller-provided override.
    """
    if path is not None:
        return str(path)

    from app.services import platform_store

    return str(platform_store.default_path())


def read_model_status(path: Optional[Path] = None) -> dict:
    """
    Read the durable ``model_status`` table as ``{provider_id: {model:
    status}}``. Best-effort: a missing or unopenable platform database
    degrades to an empty mapping and never raises.
    """
    from app.services import platform_store

    target = Path(_platform_path(path))

    if not target.exists():
        return {}

    try:
        conn = platform_store.open_connection(str(target))
    except Exception:  # noqa: BLE001 - read path is best-effort
        return {}

    try:
        rows = conn.execute(
            "SELECT provider, model, status FROM model_status"
        ).fetchall()
    except Exception:  # noqa: BLE001 - read path is best-effort
        return {}
    finally:
        conn.close()

    result: dict = {}

    for provider, model, status in rows:
        result.setdefault(provider, {})[model] = status

    return result


def write_model_status(
    provider_id: str,
    results,
    path: Optional[Path] = None,
) -> int:
    """
    Replace the ``model_status`` rows for ``provider_id`` with the scan
    ``results``. Statuses map through the canonical D4 mapping
    (``overloaded`` -> ``degraded``). Returns the number of rows written.
    """
    from app.services import platform_store

    conn = platform_store.open_connection(_platform_path(path))

    try:
        with conn:
            conn.execute(
                "DELETE FROM model_status WHERE provider = ?", (provider_id,)
            )

            for result in results:
                conn.execute(
                    "INSERT INTO model_status ("
                    "  provider, model, status, latency_ms, status_code,"
                    "  error, probed_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        provider_id,
                        result.model,
                        _STATUS_MAP.get(result.status, "unavailable"),
                        result.latency_ms,
                        result.status_code,
                        result.error,
                        time.time(),
                        time.time(),
                    ),
                )
    finally:
        conn.close()

    return len(results)


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
