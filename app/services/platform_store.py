"""
Single SQLite surface for the Relay platform database (P6.1).

``PlatformStore`` owns the file-level concerns shared by ``KeyStore``
and ``StateStore``:

* the default path ``state_dir/platform.db``,
* the combined schema migration history (``MIGRATIONS`` +
  ``PRAGMA user_version``) replaying the legacy ``relay_keys.db`` (v1)
  and ``relay_state.db`` (v1-v3) steps plus the ``model_status``
  table (v4), the durable ``events`` audit table (v5), the
  metadata-only ``request_log`` table (v6), and the project-continuity
  tables (v7),
* an in-process migration lock so concurrent opens of the same file
  cannot race a migration,
* user-only file permissions on the database plus its ``-wal``/``-shm``
  sidecars and ``.corrupt-*.bak`` backups (POSIX),
* corrupt-file backup-and-reopen, and
* the legacy-unmigrated guard (D6): refuse to create a fresh
  ``platform.db`` when legacy sources still exist and no path override
  was given, so an upgraded install can never silently orphan keys.

Each consumer (``KeyStore``/``StateStore``) keeps its own guarded
connection and lock; the shared piece here is the migration runner and
file hygiene. Raw key material and provider keys are never written to
this database: ``api_keys`` holds only scrypt hashes and the other
tables hold metadata only.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import List, Optional

from app.core.config import IS_SOURCE_CHECKOUT, PROJECT_ROOT, state_dir

# Combined schema version: api_keys (v1), the legacy state tables (v2),
# the v2/v3 state additions (v3), model_status (v4), events (v5), the
# metadata-only request_log (v6), and the project-continuity tables (v7:
# conversations, conversation_turns, summaries, compaction_records,
# project_state). See docs/platform-db-schema.md for the full DDL and
# privacy contract.
SCHEMA_VERSION = 7

MIGRATIONS: dict = {
    1: [
        """
        CREATE TABLE IF NOT EXISTS api_keys (
            id TEXT PRIMARY KEY,
            key_hash BLOB NOT NULL,
            key_salt BLOB NOT NULL,
            kdf TEXT NOT NULL,
            label TEXT NOT NULL,
            scopes TEXT NOT NULL,
            expires_at REAL,
            created_at REAL NOT NULL,
            last_used_at REAL,
            revoked_at REAL
        )
        """,
    ],
    2: [
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
    3: [
        """
        ALTER TABLE learned_state
            ADD COLUMN provider_status_expires_wall REAL
        """,
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
    4: [
        """
        CREATE TABLE IF NOT EXISTS model_status (
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            status TEXT NOT NULL,        -- available | degraded | unavailable
            latency_ms REAL,
            status_code INTEGER,
            error TEXT,
            probed_at REAL,
            updated_at REAL,
            PRIMARY KEY (provider, model)
        )
        """,
    ],
    5: [
        """
        CREATE TABLE IF NOT EXISTS events (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      REAL NOT NULL,
            actor   TEXT NOT NULL,       -- "bootstrap" | opaque key_id | "cli" | "system"
            action  TEXT NOT NULL,       -- bounded vocabulary (see D2)
            target  TEXT NOT NULL,       -- opaque id / provider id / path label
            outcome TEXT NOT NULL,       -- "ok" | "failed" | "denied"
            detail  TEXT NOT NULL        -- JSON; redacted, no secrets
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts)
        """,
    ],
    6: [
        """
        CREATE TABLE IF NOT EXISTS request_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           REAL NOT NULL,
            route        TEXT NOT NULL,
            method       TEXT,
            status       INTEGER,
            latency_ms   INTEGER,
            key_id       TEXT,           -- opaque key id; NULL for unauth'd traffic
            client_bucket TEXT NOT NULL, -- cline | opencode | continue | other
            ua           TEXT,           -- trimmed User-Agent, metadata only
            auth_scheme  TEXT            -- public | bearer | header | none
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_request_log_ts ON request_log (ts)
        """,
    ],
    7: [
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id            TEXT PRIMARY KEY,  -- conversation_id (uuid)
            key_id        TEXT NOT NULL,     -- opaque app key id (scope)
            client_bucket TEXT NOT NULL,     -- cline | opencode | continue | other
            project_key   TEXT NOT NULL,     -- key-scoped opaque project id
            status        TEXT NOT NULL,     -- active | archived
            model_chain   TEXT NOT NULL,     -- bounded JSON array of model ids
            token_budget  INTEGER,
            created_at    REAL NOT NULL,
            updated_at    REAL NOT NULL,
            last_turn_ts  REAL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS conversation_turns (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL REFERENCES conversations(id),
            seq           INTEGER NOT NULL,
            provider      TEXT,
            model         TEXT,
            outcome       TEXT NOT NULL,     -- ok | failed | denied
            task          TEXT,              -- routing task classification
            tokens_in     INTEGER,
            tokens_out    INTEGER,
            latency_ms    INTEGER,
            resume_token  TEXT,              -- stored as a hash, never raw
            ts            REAL NOT NULL,
            UNIQUE (conversation_id, seq)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS summaries (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL REFERENCES conversations(id),
            up_to_seq     INTEGER NOT NULL,
            version       INTEGER NOT NULL,  -- SUMMARY_VERSION
            method        TEXT NOT NULL,     -- extractive | llm
            content       TEXT NOT NULL,     -- derived, redacted, bounded
            tokens_in     INTEGER,
            tokens_out    INTEGER,
            created_at    REAL NOT NULL,
            UNIQUE (conversation_id, up_to_seq)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS compaction_records (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL REFERENCES conversations(id),
            at            REAL NOT NULL,
            reason        TEXT NOT NULL,     -- preflight | overflow | manual
            method        TEXT NOT NULL,     -- summary+tail | tail-only
            from_tokens   INTEGER,
            to_tokens     INTEGER,
            summary_id    INTEGER REFERENCES summaries(id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS project_state (
            project_key   TEXT NOT NULL,
            key_id        TEXT NOT NULL,
            last_models   TEXT NOT NULL,     -- bounded JSON array
            counters      TEXT NOT NULL,     -- bounded JSON counters
            last_seen     REAL NOT NULL,
            PRIMARY KEY (project_key, key_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_conversations_key
            ON conversations (key_id, status)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_conversations_project
            ON conversations (project_key, key_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_turns_cid
            ON conversation_turns (conversation_id, seq)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_compaction_cid
            ON compaction_records (conversation_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_project_state_key
            ON project_state (key_id, last_seen)
        """,
    ],
}

# In-process lock so concurrent opens of the same file cannot race a
# migration. Migration steps are idempotent (guarded by user_version),
# but the lock keeps multiple connections from interleaving DDL.
_migration_lock = threading.Lock()


class PlatformStoreError(Exception):
    """Raised when the platform database cannot be opened, migrated, or read."""


def default_path() -> Path:
    """The canonical platform database location: ``state_dir/platform.db``."""
    return state_dir / "platform.db"


def legacy_sources() -> List[Path]:
    """
    Paths that a pre-P6.1 install could have left behind, in the order
    they are consulted by the D6 guard and by ``relay migrate``.
    """
    sources = [state_dir / "relay_keys.db"]
    state_db = (
        PROJECT_ROOT / "relay_state.db"
        if IS_SOURCE_CHECKOUT
        else state_dir / "relay_state.db"
    )
    sources.append(state_db)
    return sources


def check_legacy_unmigrated(target: Path) -> None:
    """
    D6 guard: refuse to create a fresh ``platform.db`` when legacy sources
    still exist.

    An explicit ``PERSISTENCE_PATH`` or ``RELAY_DATA_DIR`` override
    bypasses the guard (the operator is choosing a legacy/compat layout),
    as does opening any path other than the canonical default. The guard
    only fires when the canonical target does not exist yet, so a
    post-migration runtime (or a fresh install with no legacy files) is
    never blocked.
    """
    if os.getenv("RELAY_DATA_DIR") or os.getenv("PERSISTENCE_PATH"):
        return

    if target != default_path():
        return

    if target.exists():
        return

    if any(source.exists() for source in legacy_sources()):
        raise PlatformStoreError(
            "legacy state detected - run `relay migrate` first"
        )


def open_connection(path: str) -> sqlite3.Connection:
    """
    Open (creating if needed) a migrated connection to ``path``.

    Runs the combined migration history under the in-process lock,
    secures POSIX permissions, and reopens once after backing up a
    corrupt file. Raises ``PlatformStoreError`` when the file declares a
    newer schema than supported or when the D6 guard blocks creation.
    """
    check_legacy_unmigrated(Path(path))

    last_error: Optional[Exception] = None

    for attempt in range(2):
        conn = sqlite3.connect(path, check_same_thread=False)

        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            migrate(conn)
        except PlatformStoreError:
            conn.close()
            raise
        except sqlite3.Error as exc:
            conn.close()
            last_error = exc

            if attempt == 0:
                _backup_corrupt(path)
                continue
        else:
            _secure_permissions(path)
            return conn

    raise PlatformStoreError(f"cannot open platform database: {last_error}")


def migrate(conn: sqlite3.Connection) -> int:
    """
    Apply pending migrations to ``conn`` and return the resulting schema
    version. Safe to call on an already-migrated connection (no-op).
    """
    with _migration_lock:
        version = conn.execute("PRAGMA user_version").fetchone()[0]

        if version > SCHEMA_VERSION:
            raise PlatformStoreError(
                f"platform database schema version {version} is newer than "
                f"supported version {SCHEMA_VERSION}; upgrade the app."
            )

        for target in range(version + 1, SCHEMA_VERSION + 1):
            statements = MIGRATIONS.get(target)

            if not statements:
                raise PlatformStoreError(
                    f"no migration defined for schema version {target}"
                )

            with conn:
                for statement in statements:
                    conn.execute(statement)

                conn.execute(f"PRAGMA user_version = {target}")

        return SCHEMA_VERSION


def _secure_permissions(path: str) -> None:
    if path == ":memory:" or os.name == "nt":
        return

    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    # WAL sidecars inherit the database mode and would leak through the
    # same directory listing; tighten them alongside the main file.
    for suffix in ("-wal", "-shm"):
        try:
            side = f"{path}{suffix}"

            if os.path.exists(side):
                os.chmod(side, 0o600)
        except OSError:
            pass


def _backup_corrupt(path: str) -> None:
    if path == ":memory:":
        return

    backup_path = f"{path}.corrupt-{int(time.time())}.bak"

    try:
        if os.path.exists(path):
            shutil.copy2(path, backup_path)

            if os.name != "nt":
                try:
                    os.chmod(backup_path, 0o600)
                except OSError:
                    pass

            os.remove(path)
    except OSError:
        return

    for suffix in ("-wal", "-shm"):
        try:
            side = f"{path}{suffix}"

            if os.path.exists(side):
                os.remove(side)
        except OSError:
            pass
