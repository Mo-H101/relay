"""
Connected-applications projection over ``api_keys`` x ``request_log``.

Derived view (P6.5): labeled API keys joined onto the durable request log
by opaque ``key_id``, plus ``none`` (unauthenticated) traffic bucketed by
User-Agent. Replaces the retired in-memory ``client_tracking`` store.

Two surfaces share one projection:

* ``apps()`` - one row per (identity, route) for ``relay apps``: label,
  opaque key id, bucket, route, counters, auth schemes, last-seen.
* ``client_activity()`` - one row per (bucket, route) aggregated for the
  TUI Applications screen, which reads only through the facade and is
  unchanged.

Both are read-only, bounded, and best-effort: an unavailable request log
or key store degrades to an empty view. The output exposes only opaque
key ids, labels, and metadata counters - never secrets.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Optional

from app.services import request_log as request_log_module

# Bounded read ceiling shared with the store's query limit.
_MAX_QUERY_LIMIT = 5000

# Synthetic identity label for traffic with no store-backed key.
_NO_KEY_LABEL = "none"


@dataclass(frozen=True)
class AppActivityEntry:
    """
    Read-only snapshot of one connected-application activity row.

    For store-backed traffic ``label``/``key_id`` identify the key and
    ``bucket`` is the User-Agent bucket; for unauthenticated traffic
    ``label`` is ``"none"``, ``key_id`` is None, and ``bucket`` is the
    User-Agent bucket. ``last_seen`` is the wall-clock timestamp of the
    newest request in the group.
    """

    label: str
    key_id: Optional[str]
    bucket: str
    ua: str
    route: str
    requests: int
    successes: int
    failures: int
    auth_schemes: tuple[str, ...]
    last_seen: float


@dataclass(frozen=True)
class ClientActivityEntry:
    """
    Read-only snapshot of one (bucket, route) row for the TUI screen.
    ``last_seen`` is monotonic so the screen's age rendering is correct.
    """

    bucket: str
    ua: str
    route: str
    requests: int
    successes: int
    failures: int
    auth_schemes: tuple[str, ...]
    last_seen: float


def _key_store():
    """
    The process-wide KeyStore used to resolve key ids to labels.
    Module-level hook so tests can inject an isolated store.
    """
    from app.core.config import state_dir
    from app.services.key_store import KeyStore

    state_dir.mkdir(parents=True, exist_ok=True)
    return KeyStore()


def _key_labels() -> dict:
    """
    Opaque key id -> label. Best-effort: an unavailable store yields an
    empty mapping and unknown ids fall back to their short id.
    """
    try:
        return {
            entry["id"]: entry["label"]
            for entry in _key_store().list()
        }
    except Exception:  # noqa: BLE001 - projection is best-effort
        return {}


def _rows() -> list:
    """
    Newest-first request-log rows, bounded. An unavailable store
    degrades to an empty list so the facade never raises.
    """
    try:
        return request_log_module.request_log().query(limit=_MAX_QUERY_LIMIT)
    except Exception:  # noqa: BLE001 - projection is best-effort
        return []


def apps() -> list[AppActivityEntry]:
    """
    Per (identity, route) rows for ``relay apps``. Authenticated traffic
    is grouped by opaque key id and labeled from ``api_keys``;
    unauthenticated traffic groups under the ``none`` identity.
    """
    labels = _key_labels()
    groups: dict[tuple, dict] = {}

    for row in _rows():
        key_id = row.get("key_id")
        identity = key_id if key_id else _NO_KEY_LABEL
        key = (identity, row.get("route") or "unmatched")

        group = groups.setdefault(
            key,
            {
                "label": labels.get(key_id, (key_id or "")[:8] or _NO_KEY_LABEL)
                if key_id
                else _NO_KEY_LABEL,
                "key_id": key_id,
                "bucket": row.get("client_bucket") or "other",
                "ua": row.get("ua") or "",
                "requests": 0,
                "successes": 0,
                "failures": 0,
                "auth_schemes": set(),
                "last_seen": 0.0,
            },
        )
        group["requests"] += 1
        group["successes"] += 1 if (row.get("status") or 500) < 400 else 0
        group["failures"] += 1 if (row.get("status") or 500) >= 400 else 0
        group["auth_schemes"].add(row.get("auth_scheme") or "none")
        group["last_seen"] = max(group["last_seen"], row.get("ts") or 0.0)

    entries = [
        AppActivityEntry(
            label=group["label"],
            key_id=group["key_id"],
            bucket=group["bucket"],
            ua=group["ua"],
            route=key[1],
            requests=group["requests"],
            successes=group["successes"],
            failures=group["failures"],
            auth_schemes=tuple(sorted(group["auth_schemes"])),
            last_seen=group["last_seen"],
        )
        for key, group in groups.items()
    ]

    entries.sort(
        key=lambda entry: (entry.last_seen, entry.label, entry.route),
        reverse=True,
    )
    return entries


def client_activity() -> list[ClientActivityEntry]:
    """
    One row per (bucket, route), aggregated across keys, for the TUI
    Applications screen. Newest bucket first; ``last_seen`` is converted
    to a monotonic timestamp so the screen renders ages correctly even
    after a restart.
    """
    groups: dict[tuple, dict] = {}

    for row in _rows():
        bucket = row.get("client_bucket") or "other"
        key = (bucket, row.get("route") or "unmatched")

        group = groups.setdefault(
            key,
            {
                "bucket": bucket,
                "ua": "",
                "requests": 0,
                "successes": 0,
                "failures": 0,
                "auth_schemes": set(),
                "last_seen": 0.0,
            },
        )
        group["requests"] += 1
        group["successes"] += 1 if (row.get("status") or 500) < 400 else 0
        group["failures"] += 1 if (row.get("status") or 500) >= 400 else 0
        group["auth_schemes"].add(row.get("auth_scheme") or "none")
        group["ua"] = row.get("ua") or group["ua"]
        group["last_seen"] = max(group["last_seen"], row.get("ts") or 0.0)

    entries = [
        ClientActivityEntry(
            bucket=group["bucket"],
            ua=group["ua"],
            route=key[1],
            requests=group["requests"],
            successes=group["successes"],
            failures=group["failures"],
            auth_schemes=tuple(sorted(group["auth_schemes"])),
            last_seen=_monotonic(group["last_seen"]),
        )
        for key, group in groups.items()
    ]

    entries.sort(key=lambda entry: entry.last_seen, reverse=True)
    return entries


def auth_totals() -> dict:
    """
    Counts of requests by presented auth-scheme label. Metadata only.
    """
    try:
        return request_log_module.request_log().auth_totals()
    except Exception:  # noqa: BLE001 - projection is best-effort
        return {}


def _monotonic(wall_ts: float) -> float:
    """
    Convert a wall-clock timestamp to its monotonic-clock equivalent so
    ``time.monotonic() - last_seen`` renders a correct age. Zero (no
    rows) stays zero.
    """
    if not wall_ts:
        return 0.0
    age = max(0.0, time.time() - wall_ts)
    return time.monotonic() - age
