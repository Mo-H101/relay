"""
``relay migrate``: one-time consolidation of the legacy P5 files into
``state_dir/platform.db``.

Sources are copied (never moved or deleted) into a timestamped backup,
then verified (integrity + per-table row counts) before a manifest is
written. Rollback restores the sources and removes ``platform.db``.
Raw key material is never printed; ``.env`` provider keys are backed up
but never imported into ``platform.db`` (that is P6.3's config swap).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from app.core.config import IS_SOURCE_CHECKOUT, PROJECT_ROOT, env_file, state_dir
from app.services import platform_store
from app.services.platform_store import PlatformStoreError, SCHEMA_VERSION

_MANIFEST_NAME = "migration-manifest.json"

# P6.2 post-migrate purge window (D4): terminal key rows younger than
# this many days are kept after import; older rows are removed.
_PRUNE_GRACE_DAYS = 30

# Columns imported 1:1 from the legacy stores. The platform schema replays
# the source DDL verbatim, so selecting explicit columns is order-safe.
_API_KEYS_COLUMNS = (
    "id, key_hash, key_salt, kdf, label, scopes, expires_at, "
    "created_at, last_used_at, revoked_at"
)

_STATE_TABLE_COLUMNS = {
    "learned_state": (
        "provider, provider_status, provider_status_remaining_seconds, "
        "provider_status_expires_wall, model_marks, model_counts, "
        "provider_counts"
    ),
    "telemetry": (
        "provider, model, request_count, success_count, failure_count, "
        "total_latency_ms, ewma_success, ewma_latency_ms, last_updated_wall"
    ),
    # The AUTOINCREMENT id is deliberately excluded so the import renumbers.
    "telemetry_failures": "provider, model, failure_type, ts",
    "quality_aggregates": (
        "provider, model, sample_count, positive_count, negative_count, "
        "ewma_score, categories, last_updated_wall"
    ),
    "decision_stats": (
        "id, decisions, candidates, selected, by_band, last_updated_wall"
    ),
}


def add_migrate_flags(parser) -> None:
    """
    Attach the ``relay migrate`` flags to the migrate subparser.
    """
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the migration plan and exit without changing anything.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm non-interactively (required when stdin is not a TTY).",
    )
    parser.add_argument(
        "--rollback",
        default=None,
        metavar="<timestamp|last>",
        help="Restore sources from a backup and remove platform.db.",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Override the state/data directory (tests, custom layouts).",
    )


# ============================
# Layout
# ============================

def _resolve_layout(data_dir_override: Optional[str]) -> dict:
    """
    Resolve the migration layout. ``--data-dir`` uses an installed-style
    layout rooted at the override (all legacy files inside it), which is
    what tests exercise; otherwise the real config layout applies.
    """
    if data_dir_override:
        data = Path(data_dir_override)
        state = data
        env = data / ".env"
        legacy_state_db = data / "relay_state.db"
    else:
        state = state_dir
        env = env_file
        legacy_state_db = (
            PROJECT_ROOT / "relay_state.db"
            if IS_SOURCE_CHECKOUT
            else state / "relay_state.db"
        )

    return {
        "state_dir": state,
        "env_file": env,
        "platform_db": state / "platform.db",
        "keys_db": state / "relay_keys.db",
        "legacy_state_db": legacy_state_db,
        "availability": state / "availability.json",
        "backups_dir": state / "backups",
    }


def _source_specs(layout: dict) -> List[dict]:
    """
    Existing legacy sources in backup order. ``.env`` is backed up but
    never imported (D5); ``availability.json`` is backed up and imported
    into ``model_status``.
    """
    specs = []

    for label, path in (
        ("keys", layout["keys_db"]),
        ("state", layout["legacy_state_db"]),
        ("availability", layout["availability"]),
        ("env", layout["env_file"]),
    ):
        if path.exists():
            specs.append({"label": label, "path": path})

    return specs


def _files_for(path: Path):
    """Yield ``path`` plus any existing SQLite WAL/SHM sidecars."""
    yield path

    for suffix in ("-wal", "-shm"):
        side = Path(f"{path}{suffix}")

        if side.exists():
            yield side


# ============================
# Backup / restore
# ============================

def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _backup(layout: dict, specs: List[dict]) -> str:
    """Copy every source (plus sidecars) into a timestamped backup dir."""
    ts = _iso_now()
    backup_dir = layout["backups_dir"] / ts
    backup_dir.mkdir(parents=True, exist_ok=True)

    for spec in specs:
        dest = backup_dir / spec["path"].name
        shutil.copy2(spec["path"], dest)

        if os.name != "nt":
            try:
                os.chmod(dest, 0o600)
            except OSError:
                pass

        for suffix in ("-wal", "-shm"):
            src = Path(f"{spec['path']}{suffix}")

            if src.exists():
                shutil.copy2(src, Path(f"{dest}{suffix}"))

    return ts


def _restore_sources(layout: dict, ts: str, specs: List[dict]) -> None:
    """Copy the backed-up sources back to their original locations."""
    backup_dir = layout["backups_dir"] / ts

    for spec in specs:
        dest = spec["path"]
        src = backup_dir / dest.name

        if not src.exists():
            continue

        shutil.copy2(src, dest)

        for suffix in ("-wal", "-shm"):
            side_src = Path(f"{src}{suffix}")

            if side_src.exists():
                shutil.copy2(side_src, Path(f"{dest}{suffix}"))


def _remove_platform_db(path: Path) -> None:
    for target in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            if target.exists():
                os.remove(target)
        except OSError:
            pass


def _checkpoint_and_remove(path: Path) -> None:
    """WAL-checkpoint then remove platform.db (and sidecars)."""
    try:
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except sqlite3.Error:
        pass

    _remove_platform_db(path)


# ============================
# Import / verify
# ============================

def _import_api_keys(platform, keys_db: Path) -> dict:
    src = sqlite3.connect(f"file:{keys_db}?mode=ro", uri=True)

    try:
        rows = src.execute(
            f"SELECT {_API_KEYS_COLUMNS} FROM api_keys"
        ).fetchall()
    except sqlite3.Error as exc:
        raise PlatformStoreError(
            f"cannot read api_keys from {keys_db}: {exc}"
        ) from exc
    finally:
        src.close()

    placeholders = ",".join("?" * 10)

    with platform:
        platform.execute("DELETE FROM api_keys")
        platform.executemany(
            f"INSERT INTO api_keys ({_API_KEYS_COLUMNS})"
            f" VALUES ({placeholders})",
            rows,
        )

    return {"api_keys": len(rows)}


def _import_state_tables(platform, state_db: Path) -> dict:
    src = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    counts: Dict[str, int] = {}

    try:
        present = {
            row[0]
            for row in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        for table, columns in _STATE_TABLE_COLUMNS.items():
            if table not in present:
                continue

            rows = src.execute(
                f"SELECT {columns} FROM {table}"
            ).fetchall()
            placeholders = ",".join("?" * len(columns.split(",")))

            with platform:
                platform.execute(f"DELETE FROM {table}")
                platform.executemany(
                    f"INSERT INTO {table} ({columns})"
                    f" VALUES ({placeholders})",
                    rows,
                )

            counts[table] = len(rows)
    except sqlite3.Error as exc:
        raise PlatformStoreError(
            f"cannot read legacy state from {state_db}: {exc}"
        ) from exc
    finally:
        src.close()

    return counts


def _import_model_status(platform, availability: Path) -> dict:
    rows = []

    if availability.exists():
        from app.setup import persistence

        rows = list(persistence.iter_model_status(availability))

    with platform:
        platform.execute("DELETE FROM model_status")

        for row in rows:
            platform.execute(
                "INSERT INTO model_status ("
                "  provider, model, status, latency_ms, status_code,"
                "  error, probed_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["provider"],
                    row["model"],
                    row["status"],
                    row["latency_ms"],
                    row["status_code"],
                    row["error"],
                    row["probed_at"],
                    row["updated_at"],
                ),
            )

    return {"model_status": len(rows)}


def _import_all(platform, layout: dict, specs: List[dict]) -> dict:
    """
    Import every legacy source into the platform database. Returns
    per-source per-table row counts (``{label: {table: count}}``).
    ``.env`` is never imported.
    """
    source_counts: Dict[str, dict] = {}

    for spec in specs:
        label = spec["label"]

        if label == "keys":
            source_counts[label] = _import_api_keys(platform, spec["path"])
        elif label == "state":
            source_counts[label] = _import_state_tables(platform, spec["path"])
        elif label == "availability":
            source_counts[label] = _import_model_status(
                platform, spec["path"]
            )

    return source_counts


def _verify(platform, source_counts: dict) -> None:
    """Integrity check plus per-table row-count verification."""
    result = platform.execute("PRAGMA integrity_check").fetchone()[0]

    if result != "ok":
        raise PlatformStoreError(f"integrity check failed: {result}")

    for label, counts in source_counts.items():
        for table, expected in counts.items():
            actual = platform.execute(
                f"SELECT count(*) FROM {table}"
            ).fetchone()[0]

            if actual != expected:
                raise PlatformStoreError(
                    f"row count mismatch for {table}: "
                    f"expected {expected}, got {actual}"
                )


def _prune_after_migrate(layout: dict) -> dict:
    """
    Purge terminal key rows older than the grace window (D4).

    Runs after import/verify and before the manifest commit; mirrors
    ``KeyStore.prune`` so the predicate has a single source of truth.
    Returns ``{"removed": ..., "scanned": ...}``.
    """
    from app.services.key_store import KeyStore, _PRUNE_GRACE_DAYS

    try:
        store = KeyStore(str(layout["platform_db"]))

        try:
            removed, scanned = store.prune(
                time.time() - _PRUNE_GRACE_DAYS * 86400
            )
        finally:
            store.close()
    except Exception:  # noqa: BLE001 - a failed purge must not fail the run
        return {"removed": 0, "scanned": 0}

    return {"removed": removed, "scanned": scanned}


def _emit_migrate_events(layout: dict, prune_info: dict) -> None:
    """
    Record the ``key.prune`` and ``migrate.run`` audit events in the
    migrated database through the EventLog service (best-effort).
    """
    from app.services.event_log import EventLog

    try:
        log = EventLog(str(layout["platform_db"]))

        try:
            log.emit(
                "key.prune",
                actor="system",
                outcome="ok",
                detail={
                    "removed": prune_info["removed"],
                    "scanned": prune_info["scanned"],
                    "source": "migrate",
                },
            )
            log.emit(
                "migrate.run",
                actor="system",
                outcome="ok",
                detail={"platform_schema_version": SCHEMA_VERSION},
            )
        finally:
            log.close()
    except Exception:  # noqa: BLE001 - audit failure must not fail the run
        pass


# ============================
# Manifest
# ============================

def _digest(path: Path) -> str:
    sha = hashlib.sha256()

    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            sha.update(chunk)

    return sha.hexdigest()


def _manifest_path(layout: dict) -> Path:
    return layout["state_dir"] / _MANIFEST_NAME


def _write_manifest(
    layout: dict, specs: List[dict], source_counts: dict, status: str
) -> None:
    sources = {}

    for spec in specs:
        sources[str(spec["path"])] = {
            "sha256": _digest(spec["path"]),
            "rows": source_counts.get(spec["label"]),
        }

    manifest = {
        "migrated_at": _iso_now(),
        "platform_schema_version": SCHEMA_VERSION,
        "status": status,
        "sources": sources,
    }

    layout["state_dir"].mkdir(parents=True, exist_ok=True)
    _manifest_path(layout).write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def _already_migrated(layout: dict, specs: List[dict]) -> bool:
    """
    True when a successful manifest matches the current sources and
    platform.db exists. Missing or changed sources invalidate it.
    """
    path = _manifest_path(layout)

    if not path.exists():
        return False

    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False

    if manifest.get("status") != "ok":
        return False

    if not layout["platform_db"].exists():
        return False

    sources = manifest.get("sources", {})

    for spec in specs:
        entry = sources.get(str(spec["path"]))

        if entry is None or entry.get("sha256") != _digest(spec["path"]):
            return False

    for source_path in sources:
        if not Path(source_path).exists():
            return False

    return True


# ============================
# Commands
# ============================

def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def _confirm(args, action: str) -> None:
    if args.yes:
        return

    if sys.stdin.isatty():
        answer = input(f"{action} [y/N] ")

        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            raise SystemExit(0)

        return

    print(
        f"Refusing to run non-interactively: pass --yes to confirm "
        f"{action.lower()}.",
        file=sys.stderr,
    )
    raise SystemExit(1)


def _expected_counts(layout: dict, specs: List[dict]) -> dict:
    """
    Read-only row counts for the dry-run plan, mirroring ``_import_all``.
    """
    counts = {}

    for spec in specs:
        label = spec["label"]

        if label == "keys":
            src = sqlite3.connect(f"file:{spec['path']}?mode=ro", uri=True)

            try:
                count = src.execute(
                    "SELECT count(*) FROM api_keys"
                ).fetchone()[0]
            except sqlite3.Error:
                count = None
            finally:
                src.close()

            counts[label] = {"api_keys": count}
        elif label == "state":
            src = sqlite3.connect(f"file:{spec['path']}?mode=ro", uri=True)

            try:
                present = {
                    row[0]
                    for row in src.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                counts[label] = {
                    table: src.execute(
                        f"SELECT count(*) FROM {table}"
                    ).fetchone()[0]
                    for table in _STATE_TABLE_COLUMNS
                    if table in present
                }
            finally:
                src.close()
        elif label == "availability":
            from app.setup import persistence

            counts[label] = {
                "model_status": len(
                    list(persistence.iter_model_status(spec["path"]))
                )
            }

    return counts


def _do_dry_run(layout: dict, specs: List[dict]) -> None:
    if _already_migrated(layout, specs):
        print("Already migrated (manifest matches current sources).")
        return

    if not specs:
        print("No legacy sources found; nothing to migrate.")
        return

    print(f"Target: {layout['platform_db']}")
    print(f"Backup: {layout['backups_dir']}/<timestamp>")
    print("Sources:")

    for spec in specs:
        note = ""

        if spec["label"] == "env":
            note = "  (backed up only; never imported)"
        elif spec["label"] == "availability":
            note = "  (seeds model_status)"

        print(f"  {spec['path']}{note}")

    counts = _expected_counts(layout, specs)

    for label, tables in counts.items():
        if not tables:
            continue

        detail = ", ".join(
            f"{table}={count if count is not None else '?'}"
            for table, count in sorted(tables.items())
        )
        print(f"  import: {label} -> {detail}")

    print("Dry run: nothing changed.")


def _do_run(layout: dict, specs: List[dict]) -> None:
    if _already_migrated(layout, specs):
        print("Already migrated (manifest matches current sources).")
        return

    if not specs:
        print("No legacy sources found; nothing to migrate.")
        return

    if layout["platform_db"].exists():
        print(
            "warning: platform.db exists without a matching manifest; "
            "re-importing (--yes was required).",
            file=sys.stderr,
        )

    # 1. Backup first, before any modification.
    ts = _backup(layout, specs)

    try:
        # 2. Create + migrate the platform database (PlatformStore runs
        #    the combined history to SCHEMA_VERSION).
        platform = platform_store.open_connection(str(layout["platform_db"]))

        try:
            # 3. Import sources 1:1.
            source_counts = _import_all(platform, layout, specs)
            # 4. Verify integrity + per-table row counts.
            _verify(platform, source_counts)
        finally:
            platform.close()
    except Exception as exc:  # noqa: BLE001 - abort path reports short message
        print(f"migration failed: {exc}", file=sys.stderr)
        _write_manifest(layout, specs, {}, status="failed")
        _restore_sources(layout, ts, specs)
        _remove_platform_db(layout["platform_db"])
        print(f"restored sources from {layout['backups_dir'] / ts}", file=sys.stderr)
        raise SystemExit(1)

    # 4b. Post-import housekeeping (D4): purge terminal keys older than
    #     the grace window, then record audit events in the migrated DB.
    prune_info = _prune_after_migrate(layout)
    _emit_migrate_events(layout, prune_info)

    # 5. Commit the manifest.
    _write_manifest(layout, specs, source_counts, status="ok")
    print(f"Migrated into {layout['platform_db']}")
    print(f"Backup kept at {layout['backups_dir'] / ts}")
    print(
        "Legacy files were copied, not deleted; leave them in place until "
        "you have verified the migration and can downgrade at any time."
    )


def _do_rollback(layout: dict, ref: str) -> None:
    if ref == "last":
        dirs = sorted(
            (p for p in layout["backups_dir"].glob("*") if p.is_dir())
        )

        if not dirs:
            _fail("No backups found; nothing to roll back.")

        ref = dirs[-1].name

    backup_dir = layout["backups_dir"] / ref

    if not backup_dir.is_dir():
        _fail(f"Backup not found: {ref}")

    # Restore every source recorded in the manifest (falling back to the
    # current layout's existing sources when the manifest is unavailable).
    manifest_path = _manifest_path(layout)
    specs: List[dict] = []

    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            for path_str in manifest.get("sources", {}):
                path = Path(path_str)
                specs.append({"label": path.name, "path": path})
        except (OSError, ValueError):
            specs = []

    if not specs:
        specs = _source_specs(layout)

    _restore_sources(layout, ref, specs)
    _checkpoint_and_remove(layout["platform_db"])

    try:
        if manifest_path.exists():
            manifest_path.unlink()
    except OSError:
        pass

    print(f"Restored sources from {backup_dir}")
    print(f"Removed {layout['platform_db']}")
    print(
        "The runtime now expects the legacy files again. To keep "
        "platform.db, re-run 'relay migrate'."
    )


def _run_migrate(args, parser) -> None:
    """Dispatch one ``relay migrate`` invocation."""
    layout = _resolve_layout(args.data_dir)
    specs = _source_specs(layout)

    if args.rollback:
        _do_rollback(layout, args.rollback)
        return

    if args.dry_run:
        _do_dry_run(layout, specs)
        return

    if not args.yes:
        _confirm(args, "Run the migration")

    _do_run(layout, specs)
