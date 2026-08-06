"""
Durable security-event log backed by the shared ``platform.db``.

``EventLog`` writes rows into the ``events`` table (schema v5) through
its own guarded connection, following the established multi-connection
WAL model (``busy_timeout``/``_migration_lock`` covered by
``PlatformStore``). The log is metadata-only: no prompts, responses,
raw keys, hash material, proxy credentials, or correlation ids. Every
``detail`` payload passes through ``redact_dict`` before insert, so even
unexpected raw-key-shaped content cannot survive into a row.

Write semantics (D1):
* best-effort on hot paths (auth): ``emit`` never raises and never
  blocks the request; a failed write increments
  ``relay_events_failed_total``.
* fail-visible on admin paths: ``emit(raise_on_error=True)`` re-raises
  so an operator cannot believe an un-recorded action happened.

Actor identity: store-backed requests carry the opaque ``key_id`` from
``request.scope["relay_key_id"]``; bootstrap-key requests record
``"bootstrap"``; CLI writes record ``"cli"``; migration/purge record
``"system"``. Never the raw key.

The module-level ``event_log()`` singleton is built lazily (tests inject
an isolated store by monkeypatching the accessor), matching the auth
KeyStore pattern.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Optional

from app.services import platform_store
from app.services.metrics import relay_metrics
from app.services.redaction import redact_dict

# Bounded action vocabulary (D2). Kept as a module constant for tests and
# documentation; callers pass action strings directly.
EVENT_ACTIONS = frozenset(
    {
        "key.create",
        "key.rotate",
        "key.revoke",
        "key.delete",
        "key.prune",
        "provider_key.set",
        "provider_key.remove",
        "provider_key.migrate",
        "config.reload",
        "config.set",
        "config.unset",
        "auth.success",
        "auth.failure",
        "store.open",
        "store.close",
        "migrate.run",
        # P9 project continuity.
        "continuity.create",
        "continuity.resume",
        "continuity.switch",
        "continuity.compact",
        "continuity.archive",
        "continuity.prune",
        "continuity.denied",
        "continuity.reconcile",
    }
)

_OUTCOMES = frozenset({"ok", "failed", "denied"})

# Bounded read ceiling so the admin/CLI surfaces never dump the table.
_MAX_QUERY_LIMIT = 500


class EventLog:
    """
    SQLite-backed durable security-event log.

    Single guarded connection with WAL journaling, mirroring ``KeyStore``
    and ``StateStore``. Opening is best-effort: a failure leaves the
    connection None and every operation retries the open, so the auth hot
    path degrades to the failure counter instead of raising.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path or str(platform_store.default_path())
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._open()

    @property
    def path(self) -> str:
        return self._path

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def emit(
        self,
        action: str,
        *,
        actor: str = "system",
        target: str = "",
        outcome: str = "ok",
        detail: Optional[dict] = None,
        raise_on_error: bool = False,
    ) -> bool:
        """
        Write one event row. Returns True on success.

        ``detail`` is passed through ``redact_dict`` before insert and
        stored as JSON. On the best-effort path (default) a failure is
        swallowed and ``relay_events_failed_total`` is incremented; with
        ``raise_on_error=True`` the underlying exception propagates so
        admin actions surface the audit failure.
        """
        try:
            safe_detail = redact_dict(detail or {})
            payload = json.dumps(safe_detail, separators=(",", ":"))

            if not self._ensure_open():
                raise OSError("events store is not available")

            with self._lock:
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO events (ts, actor, action, target,"
                        "  outcome, detail) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            time.time(),
                            actor,
                            action,
                            target,
                            outcome,
                            payload,
                        ),
                    )
        except Exception:
            relay_metrics.events_failed.inc()

            if raise_on_error:
                raise

            return False

        relay_metrics.events_written.inc()
        return True

    def query(
        self,
        action: Optional[str] = None,
        outcome: Optional[str] = None,
        limit: int = 50,
    ) -> list:
        """
        Return the newest ``limit`` rows matching ``action``/``outcome``.

        Bounded reads only (``limit`` clamped to ``_MAX_QUERY_LIMIT``);
        rows are newest first. ``detail`` is returned as the parsed dict.
        """
        limit = max(1, min(int(limit), _MAX_QUERY_LIMIT))

        clauses: list = []
        params: list = []

        if action:
            clauses.append("action = ?")
            params.append(action)
        if outcome:
            clauses.append("outcome = ?")
            params.append(outcome)

        where = " WHERE " + " AND ".join(clauses) if clauses else ""

        if not self._ensure_open():
            raise OSError("events store is not available")

        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, actor, action, target, outcome, detail"
                f" FROM events{where}"
                " ORDER BY ts DESC, id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()

        result = []

        for row in rows:
            raw_detail = row[6]

            try:
                parsed = json.loads(raw_detail) if raw_detail else {}
            except ValueError:
                parsed = {}

            result.append(
                {
                    "id": row[0],
                    "ts": row[1],
                    "actor": row[2],
                    "action": row[3],
                    "target": row[4],
                    "outcome": row[5],
                    "detail": parsed if isinstance(parsed, dict) else {},
                }
            )

        return result

    def prune_retention(self, days: int) -> int:
        """
        Delete events older than ``days`` days and return the number
        removed. ``days <= 0`` disables pruning (returns 0).
        """
        if days <= 0:
            return 0

        cutoff = time.time() - int(days) * 86400

        if not self._ensure_open():
            raise OSError("events store is not available")

        with self._lock:
            with self._conn:
                cursor = self._conn.execute(
                    "DELETE FROM events WHERE ts < ?", (cutoff,)
                )

        return cursor.rowcount

    def count(self) -> int:
        """
        Total number of event rows, for diagnostics and tests.
        """
        if not self._ensure_open():
            raise OSError("events store is not available")

        with self._lock:
            return self._conn.execute(
                "SELECT count(*) FROM events"
            ).fetchone()[0]

    # ============================
    # Internals
    # ============================

    def _ensure_open(self) -> bool:
        if self._conn is not None:
            return True

        with self._lock:
            if self._conn is None:
                self._open()

        return self._conn is not None

    def _open(self) -> None:
        try:
            self._conn = platform_store.open_connection(self._path)
        except Exception:
            self._conn = None


_LOG_SINGLETON: Optional[EventLog] = None


def event_log() -> EventLog:
    """
    The process-wide event log (lazily built). Tests monkeypatch this
    accessor to point at an isolated store.
    """
    global _LOG_SINGLETON

    if _LOG_SINGLETON is None:
        _LOG_SINGLETON = EventLog()

    return _LOG_SINGLETON


def reset_event_log() -> None:
    """
    Close and drop the event-log singleton (test isolation).
    """
    global _LOG_SINGLETON

    if _LOG_SINGLETON is not None:
        _LOG_SINGLETON.close()
        _LOG_SINGLETON = None
