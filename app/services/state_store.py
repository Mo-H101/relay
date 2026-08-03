"""
Persistent state storage for Relay intelligence.

StateStore is the only component that touches SQLite. It persists only
learned health degradation and telemetry aggregates/failure history:
never health snapshots, prompts, responses, API keys, or generated
content. The in-memory stores remain the source of truth for reads;
StateStore is a durable write-behind copy.

Time model: the in-memory stores use monotonic clocks. StateStore
persists wall-clock timestamps (failure events) and remaining-TTL
information (learned marks/counters). On load, the stores rebuild their
monotonic expiry timers from those values and drop anything already
expired during downtime.

NOTE: This component assumes a single-process, single-writer model. Multiple processes or threads writing to the same database file are not supported and may lead to corruption. The storage is designed for use within a single Relay instance only.
"""

from collections import deque
import json
import os
import shutil
import sqlite3
import threading
import time
from typing import Dict, List, Optional

from app.services.metrics import relay_metrics

MIGRATIONS: Dict[int, List[str]] = {
    1: [
        """
        CREATE TABLE IF NOT EXISTS learned_state (
            provider TEXT PRIMARY KEY,
            provider_status TEXT,
            provider_status_remaining_seconds REAL,
            model_marks TEXT NOT NULL,
            model_counts TEXT NOT NULL,
            provider_counts TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS telemetry (
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            request_count INTEGER NOT NULL,
            success_count INTEGER NOT NULL,
            failure_count INTEGER NOT NULL,
            total_latency_ms INTEGER NOT NULL,
            PRIMARY KEY (provider, model)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS telemetry_failures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            failure_type TEXT NOT NULL,
            ts REAL NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_telemetry_failures_pair
            ON telemetry_failures (provider, model)
        """,
    ],
    2: [
        """
        ALTER TABLE learned_state
            ADD COLUMN provider_status_expires_wall REAL
        """,
    ],
    3: [
        """
        ALTER TABLE telemetry
            ADD COLUMN ewma_success REAL
        """,
        """
        ALTER TABLE telemetry
            ADD COLUMN ewma_latency_ms REAL
        """,
        """
        ALTER TABLE telemetry
            ADD COLUMN last_updated_wall REAL
        """,
        """
        CREATE TABLE IF NOT EXISTS quality_aggregates (
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            sample_count INTEGER NOT NULL,
            positive_count INTEGER NOT NULL,
            negative_count INTEGER NOT NULL,
            ewma_score REAL,
            categories TEXT NOT NULL,
            last_updated_wall REAL,
            PRIMARY KEY (provider, model)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS decision_stats (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            decisions INTEGER NOT NULL,
            candidates INTEGER NOT NULL,
            selected TEXT NOT NULL,
            by_band TEXT NOT NULL,
            last_updated_wall REAL NOT NULL
        )
        """,
    ],
}


class StateStoreError(Exception):
    """Raised when state cannot be opened, migrated, or persisted."""


class StateStore:
    """
    SQLite-backed durable store for learned health and telemetry.

    Single guarded connection with WAL journaling and a busy timeout.
    Every mutation commits atomically in one transaction. SQLite is
    never accessed from chat request paths; Relay uses this store
    outside those paths.
    """

    SCHEMA_VERSION = 3

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path or ":memory:"
        self._lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._last_load_at: Optional[float] = None
        self._load_count = 0
        self._load_errors: deque = deque(maxlen=20)
        self._conn: Optional[sqlite3.Connection] = None
        self._schema_version: Optional[int] = None
        self._open()

    @property
    def path(self) -> str:
        return self._path

    def stats(self) -> dict:
        """
        Diagnostic information about persisted state access.
        """
        with self._stats_lock:
            return {
                "path": self._path,
                "schema_version": self._schema_version,
                "last_load_at": self._last_load_at,
                "load_count": self._load_count,
                "load_errors": list(self._load_errors),
            }

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ============================
    # Learned health state
    # ============================

    def save_learned_state(self, learned: Dict[str, dict]) -> None:
        """
        Replace all persisted learned health state.
        """
        self._ensure_open()

        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM learned_state")

                for provider, data in learned.items():
                    self._conn.execute(
                        "INSERT INTO learned_state ("
                        "  provider, provider_status,"
                        "  provider_status_remaining_seconds,"
                        "  provider_status_expires_wall,"
                        "  model_marks, model_counts, provider_counts"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            provider,
                            data.get("provider_status"),
                            data.get("provider_status_remaining_seconds"),
                            data.get("provider_status_expires_wall"),
                            json.dumps(data.get("model_marks", {})),
                            json.dumps(data.get("model_counts", {})),
                            json.dumps(data.get("provider_counts", {})),
                        ),
                    )

    def load_learned_state(self) -> Dict[str, dict]:
        """
        Return all persisted learned health state in export format.
        """
        try:
            self._ensure_open()

            with self._lock:
                rows = self._conn.execute(
                    "SELECT provider, provider_status,"
                    "  provider_status_remaining_seconds,"
                    "  provider_status_expires_wall, model_marks,"
                    "  model_counts, provider_counts"
                    " FROM learned_state ORDER BY provider"
                ).fetchall()

            result = {}

            for (
                provider,
                status,
                remaining,
                expires_wall,
                marks,
                counts,
                provider_counts,
            ) in rows:
                result[provider] = {
                    "provider_status": status,
                    "provider_status_remaining_seconds": remaining,
                    "provider_status_expires_wall": expires_wall,
                    "model_marks": self._decode_json(marks),
                    "model_counts": self._decode_json(counts),
                    "provider_counts": self._decode_json(provider_counts),
                }

            self._record_load_success()
            return result
        except Exception as exc:
            self._record_load_error("load_learned_state", exc)
            raise

    # ============================
    # Telemetry
    # ============================

    def save_telemetry(self, entries: List[dict]) -> None:
        """
        Replace all persisted telemetry aggregates and failure history.
        """
        self._ensure_open()

        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM telemetry")
                self._conn.execute("DELETE FROM telemetry_failures")

                for data in entries:
                    provider = data.get("provider")
                    model = data.get("model")

                    self._conn.execute(
                        "INSERT INTO telemetry ("
                        "  provider, model, request_count, success_count,"
                        "  failure_count, total_latency_ms, ewma_success,"
                        "  ewma_latency_ms, last_updated_wall"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            provider,
                            model,
                            int(data.get("request_count", 0)),
                            int(data.get("success_count", 0)),
                            int(data.get("failure_count", 0)),
                            int(data.get("total_latency_ms", 0)),
                            _opt_float(data.get("ewma_success")),
                            _opt_float(data.get("ewma_latency_ms")),
                            _opt_float(data.get("last_updated_wall")),
                        ),
                    )

                    for event in data.get("recent_failures", []) or []:
                        self._conn.execute(
                            "INSERT INTO telemetry_failures ("
                            "  provider, model, failure_type, ts"
                            ") VALUES (?, ?, ?, ?)",
                            (
                                provider,
                                model,
                                str(event.get("failure_type") or "unknown"),
                                float(event.get("ts") or 0.0),
                            ),
                        )

    def load_telemetry(self) -> List[dict]:
        """
        Return all persisted telemetry in export format.
        """
        try:
            self._ensure_open()

            with self._lock:
                rows = self._conn.execute(
                    "SELECT provider, model, request_count, success_count,"
                    "  failure_count, total_latency_ms, ewma_success,"
                    "  ewma_latency_ms, last_updated_wall"
                    " FROM telemetry ORDER BY provider, model"
                ).fetchall()

                result = []

                for (
                    provider,
                    model,
                    request_count,
                    success_count,
                    failure_count,
                    total,
                    ewma_success,
                    ewma_latency_ms,
                    last_updated_wall,
                ) in rows:
                    failures = self._conn.execute(
                        "SELECT failure_type, ts FROM telemetry_failures"
                        " WHERE provider = ? AND model = ? ORDER BY id",
                        (provider, model),
                    ).fetchall()

                    result.append(
                        {
                            "provider": provider,
                            "model": model,
                            "request_count": request_count,
                            "success_count": success_count,
                            "failure_count": failure_count,
                            "total_latency_ms": total,
                            "ewma_success": ewma_success,
                            "ewma_latency_ms": ewma_latency_ms,
                            "last_updated_wall": last_updated_wall,
                            "recent_failures": [
                                {"failure_type": failure_type, "ts": ts}
                                for failure_type, ts in failures
                            ],
                        }
                    )

            self._record_load_success()
            return result
        except Exception as exc:
            self._record_load_error("load_telemetry", exc)
            raise

    # ============================
    # Quality aggregates
    # ============================

    def save_quality(self, aggregates: List[dict]) -> None:
        """
        Replace all persisted quality feedback aggregates (Phase 7D/7F).
        Metadata only: provider/model identifiers, sample counts, tallies,
        EWMA score, category tallies, and the last-updated timestamp.
        """
        self._ensure_open()

        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM quality_aggregates")

                for data in aggregates:
                    provider = data.get("provider")
                    model = data.get("model")

                    if not provider or not model:
                        continue

                    self._conn.execute(
                        "INSERT INTO quality_aggregates ("
                        "  provider, model, sample_count, positive_count,"
                        "  negative_count, ewma_score, categories,"
                        "  last_updated_wall"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            provider,
                            model,
                            int(data.get("sample_count", 0)),
                            int(data.get("positive_count", 0)),
                            int(data.get("negative_count", 0)),
                            _opt_float(data.get("ewma_score")),
                            json.dumps(data.get("categories", {}) or {}),
                            _opt_float(data.get("last_updated_wall")),
                        ),
                    )

    def load_quality(self) -> List[dict]:
        """
        Return all persisted quality aggregates in export format.
        """
        try:
            self._ensure_open()

            with self._lock:
                rows = self._conn.execute(
                    "SELECT provider, model, sample_count, positive_count,"
                    "  negative_count, ewma_score, categories,"
                    "  last_updated_wall"
                    " FROM quality_aggregates ORDER BY provider, model"
                ).fetchall()

            result = []

            for (
                provider,
                model,
                sample_count,
                positive_count,
                negative_count,
                ewma_score,
                categories,
                last_updated_wall,
            ) in rows:
                result.append(
                    {
                        "provider": provider,
                        "model": model,
                        "sample_count": sample_count,
                        "positive_count": positive_count,
                        "negative_count": negative_count,
                        "ewma_score": ewma_score,
                        "categories": self._decode_json(categories),
                        "last_updated_wall": last_updated_wall,
                    }
                )

            self._record_load_success()
            return result
        except Exception as exc:
            self._record_load_error("load_quality", exc)
            raise

    # ============================
    # Decision statistics
    # ============================

    def save_decision_stats(self, stats: Optional[dict]) -> None:
        """
        Replace the persisted decision statistics row (Phase 7E/7F).

        Bounded numeric state only: decision/candidate totals plus
        per-pair selection tallies and per-band tallies. An empty or
        None snapshot clears the persisted row.
        """
        self._ensure_open()

        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM decision_stats")

                if not stats:
                    return

                self._conn.execute(
                    "INSERT INTO decision_stats ("
                    "  id, decisions, candidates, selected, by_band,"
                    "  last_updated_wall"
                    ") VALUES (1, ?, ?, ?, ?, ?)",
                    (
                        max(0, int(stats.get("decisions", 0))),
                        max(0, int(stats.get("candidates", 0))),
                        json.dumps(stats.get("selected", {}) or {}),
                        json.dumps(stats.get("by_band", {}) or {}),
                        time.time(),
                    ),
                )

    def load_decision_stats(self) -> Optional[dict]:
        """
        Return the persisted decision statistics row, or None when no
        decisions have been persisted yet.
        """
        try:
            self._ensure_open()

            with self._lock:
                row = self._conn.execute(
                    "SELECT decisions, candidates, selected, by_band,"
                    "  last_updated_wall"
                    " FROM decision_stats WHERE id = 1"
                ).fetchone()

            if row is None:
                self._record_load_success()
                return None

            decisions, candidates, selected, by_band, last_updated_wall = row
            result = {
                "decisions": decisions,
                "candidates": candidates,
                "selected": self._decode_json(selected),
                "by_band": self._decode_json(by_band),
                "last_updated_wall": last_updated_wall,
            }

            self._record_load_success()
            return result
        except Exception as exc:
            self._record_load_error("load_decision_stats", exc)
            raise

    # ============================
    # Retention
    # ============================

    def prune_retention(self, retention_days: int = 0) -> None:
        """
        Delete persisted telemetry failure history and quality aggregates
        older than retention_days. No-op when retention is disabled
        (<= 0). Decision statistics are a single bounded row with no
        history, so they are not pruned.
        """
        if retention_days <= 0:
            return

        self._ensure_open()
        cutoff = time.time() - retention_days * 86400

        with self._lock:
            with self._conn:
                self._conn.execute(
                    "DELETE FROM telemetry_failures WHERE ts < ?",
                    (cutoff,),
                )
                self._conn.execute(
                    "DELETE FROM quality_aggregates"
                    " WHERE last_updated_wall IS NOT NULL"
                    "   AND last_updated_wall < ?",
                    (cutoff,),
                )

    def memory_counts(self) -> dict:
        """
        Count of persisted rows per durable surface, for diagnostics.
        Metadata only; never exposes stored values.
        """
        self._ensure_open()

        with self._lock:
            learned = self._conn.execute(
                "SELECT count(*) FROM learned_state"
            ).fetchone()[0]
            telemetry = self._conn.execute(
                "SELECT count(*) FROM telemetry"
            ).fetchone()[0]
            quality = self._conn.execute(
                "SELECT count(*) FROM quality_aggregates"
            ).fetchone()[0]
            decision = self._conn.execute(
                "SELECT count(*) FROM decision_stats"
            ).fetchone()[0]

        return {
            "learned_providers": learned,
            "telemetry_pairs": telemetry,
            "quality_pairs": quality,
            "decision_stats_rows": decision,
        }

    # ============================
    # Internals
    # ============================

    def _record_load_success(self) -> None:
        with self._stats_lock:
            self._last_load_at = time.time()
            self._load_count += 1

    def _record_load_error(self, operation: str, exc: Exception) -> None:
        with self._stats_lock:
            self._load_errors.append(
                {
                    "operation": operation,
                    "at": time.time(),
                    "message": str(exc),
                }
            )

        relay_metrics.persistence_load_failures.inc()

    def _ensure_open(self) -> None:
        with self._lock:
            if self._conn is None:
                self._open()

    def _open(self) -> None:
        last_error: Optional[Exception] = None

        for attempt in range(2):
            conn = sqlite3.connect(self._path, check_same_thread=False)

            try:
                conn.execute("PRAGMA busy_timeout = 5000")
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
                self._migrate(conn)
            except StateStoreError:
                conn.close()
                raise
            except sqlite3.Error as exc:
                conn.close()
                last_error = exc

                if attempt == 0:
                    self._backup_corrupt()
                    continue

            else:
                self._conn = conn
                self._schema_version = conn.execute(
                    "PRAGMA user_version"
                ).fetchone()[0]
                return

        raise StateStoreError(f"cannot open state database: {last_error}")

    def _migrate(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]

        if version > self.SCHEMA_VERSION:
            raise StateStoreError(
                f"state database schema version {version} is newer than "
                f"supported version {self.SCHEMA_VERSION}; upgrade the app."
            )

        for target in range(version + 1, self.SCHEMA_VERSION + 1):
            statements = MIGRATIONS.get(target)

            if not statements:
                raise StateStoreError(
                    f"no migration defined for schema version {target}"
                )

            with conn:
                for statement in statements:
                    conn.execute(statement)

                conn.execute(f"PRAGMA user_version = {target}")

    def _backup_corrupt(self) -> None:
        if self._path == ":memory:":
            return

        backup_path = f"{self._path}.corrupt-{int(time.time())}.bak"

        try:
            if os.path.exists(self._path):
                shutil.copy2(self._path, backup_path)
                os.remove(self._path)
        except OSError:
            return

        for suffix in ("-wal", "-shm"):
            try:
                side = f"{self._path}{suffix}"

                if os.path.exists(side):
                    os.remove(side)
            except OSError:
                pass

    @staticmethod
    def _decode_json(text: Optional[str]) -> dict:
        if not text:
            return {}

        try:
            value = json.loads(text)
        except ValueError:
            return {}

        return value if isinstance(value, dict) else {}


def _opt_float(value) -> Optional[float]:
    """
    Coerce a persisted numeric value to float, tolerating None/absent
    keys so legacy exports (without EWMA/quality fields) persist
    cleanly.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
