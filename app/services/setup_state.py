"""
Setup state tracking.

Distinguishes the three lifecycle states the CLI needs to decide what to
do on launch:

- ``not_configured`` — installed but never configured (first run).
- ``configured`` — a completed, usable setup exists.
- ``incomplete`` — setup was attempted but not completed.

State is a small JSON document in ``config.state_dir`` (``.relay/`` by
default). It is intentionally independent of ``.env`` existence: a user
may have a ``.env`` without a completed setup, or vice versa.
"""

import json
import time
from typing import Literal

from app.core.config import state_dir

SetupState = Literal["not_configured", "configured", "incomplete"]

_STATE_FILE = "state.json"
_SCHEMA = 1
_VALID_STATES = ("configured", "incomplete")


def _state_path():
    return state_dir / _STATE_FILE


def read_setup_state() -> SetupState:
    """Return the persisted setup state, defaulting to ``not_configured``."""
    path = _state_path()

    if not path.exists():
        return "not_configured"

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "incomplete"

    if data.get("schema") != _SCHEMA:
        return "incomplete"

    state = data.get("setup_state")
    return state if state in _VALID_STATES else "not_configured"


def write_setup_state(
    state: SetupState,
    *,
    configured_providers: list[str] | None = None,
    last_setup_at: float | None = None,
) -> None:
    """
    Persist the setup state atomically.

    The extra fields are additive (schema stays ``1``) so older state
    files remain readable and the P0 reader behavior is unchanged.
    """
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "schema": _SCHEMA,
        "setup_state": state,
        "updated_at": time.time(),
    }

    if configured_providers is not None:
        payload["configured_providers"] = list(configured_providers)

    if last_setup_at is not None:
        payload["last_setup_at"] = last_setup_at

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def read_setup_details() -> dict:
    """
    Return the full setup-state document with safe defaults for any
    additive field, so callers never have to handle missing keys.
    """
    path = _state_path()

    if not path.exists():
        return {
            "schema": None,
            "setup_state": "not_configured",
            "updated_at": None,
            "configured_providers": [],
            "last_setup_at": None,
        }

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {
            "schema": None,
            "setup_state": "incomplete",
            "updated_at": None,
            "configured_providers": [],
            "last_setup_at": None,
        }

    configured = data.get("configured_providers")

    if not isinstance(configured, list):
        configured = []

    return {
        "schema": data.get("schema"),
        "setup_state": data.get("setup_state", "not_configured"),
        "updated_at": data.get("updated_at"),
        "configured_providers": [str(item) for item in configured],
        "last_setup_at": data.get("last_setup_at"),
    }
