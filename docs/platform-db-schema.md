# `platform.db` Schema

The consolidated Relay platform database at `state_dir/platform.db`
(P6.1). Replaces `relay_keys.db` and `relay_state.db`; `availability.json`
stays the live setup-scan source until P6.3.

- Schema owner: `app/services/platform_store.py` (`MIGRATIONS` +
  `SCHEMA_VERSION`).
- File concerns: WAL mode, `busy_timeout 5000`, `0600` on the database
  and its `-wal`/`-shm` sidecars (POSIX), in-process migration lock,
  corrupt-file backup-aside-and-reopen, and the legacy-unmigrated guard
  (D6) that blocks fresh creation while legacy sources exist.
- Consumers: `KeyStore` and `StateStore` delegate open/migrate/security
  to `PlatformStore`; their public APIs are unchanged.

## Tables

All DDL is the source DDL verbatim (replayed by the migration history).

### `api_keys` (schema v1)

From `key_store.py`:

```sql
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
```

`key_hash` is deliberately **unindexed**: lookup is a constant-time scan,
so the database cannot leak which key matched (unchanged from the legacy
store).

### State tables (schema v2 + v3)

`learned_state`, `telemetry`, `telemetry_failures`
(+ `idx_telemetry_failures_pair`), `quality_aggregates`, `decision_stats`
from `state_store.py`, verbatim. v2 creates the tables; v3 adds
`learned_state.provider_status_expires_wall`, the three EWMA/last-updated
columns on `telemetry`, and the v2/v3 `quality_aggregates` /
`decision_stats` tables.

### `model_status` (schema v4)

New in P6.1, seeded at migration time from the availability snapshot:

```sql
CREATE TABLE IF NOT EXISTS model_status (
    provider    TEXT NOT NULL,
    model       TEXT NOT NULL,
    status      TEXT NOT NULL,        -- available | degraded | unavailable
    latency_ms  REAL,
    status_code INTEGER,
    error       TEXT,
    probed_at   REAL,
    updated_at  REAL,
    PRIMARY KEY (provider, model)
)
```

Column mapping from `availability.json` `providers[pid].models[]` (see
`app/setup/persistence.py:iter_model_status`):

| `model_status` | source | notes |
| --- | --- | --- |
| `provider` | snapshot provider id | |
| `model` | `models[].model` | |
| `status` | `models[].status` | canonical 3-state mapping (D4): `available` → `available`, `overloaded` → `degraded`, `unavailable` → `unavailable`; missing/unknown → `unavailable` |
| `latency_ms` | `models[].latency_ms` | |
| `status_code` | `models[].status_code` | |
| `error` | `models[].error` | |
| `probed_at` | `models[].probed_at` | |
| `updated_at` | `providers[pid].generated_at` | |

## Privacy contract

`platform.db` stores **scrypt hashes only** for keys and **metadata only**
elsewhere. It never stores raw keys, prompts, responses, proxy
credentials, or correlation ids. `api_keys` holds only the hash/salt/kdf
material produced by the keyring; no provider key ever enters this
database.

`.env` is backed up by `relay migrate` but **never imported** (D5) —
provider keys stay in the keyring / legacy `.env` fallback until the P6.3
config swap.

## Migration history

| version | contents |
| --- | --- |
| 1 | `api_keys` (legacy `relay_keys.db` DDL) |
| 2 | `learned_state`, `telemetry`, `telemetry_failures`, `idx_telemetry_failures_pair` |
| 3 | `ALTER learned_state ADD provider_status_expires_wall`; `ALTER telemetry ADD ewma_success / ewma_latency_ms / last_updated_wall`; `quality_aggregates`, `decision_stats` |
| 4 | `model_status` |

Migrations run under an in-process lock and are idempotent (guarded by
`PRAGMA user_version`). A file declaring a newer version than
`SCHEMA_VERSION` is refused with an upgrade error; a corrupt file is
copied aside as `platform.db.corrupt-<ts>.bak` and reopened fresh.

## `relay migrate`

`relay migrate [--dry-run] [--yes] [--rollback <timestamp|last>] [--state-dir <dir>]`

1. Resolve the layout (`state_dir`, legacy `relay_state.db` location,
   `env_file`; `--state-dir` overrides for tests, `--data-dir` is an
   accepted alias).
2. `--dry-run`: print source paths, tables, and expected row counts;
   change nothing.
3. Re-run detection: a manifest with unchanged source digests → "already
   migrated". `platform.db` without a manifest → warn and require `--yes`.
4. Backup every source (`relay_keys.db`, `relay_state.db`,
   `availability.json`, `.env`, plus `-wal`/`-shm` sidecars) into
   `state_dir/backups/<ts>/`, `0600` on POSIX. Sources are copied, never
   moved or deleted.
5. Create `platform.db` via `PlatformStore` (migrations to v4).
6. Import: `api_keys` ← `relay_keys.db` 1:1; the five state tables ←
   `relay_state.db` 1:1; `model_status` ← `availability.json` (D4).
7. Verify: `PRAGMA integrity_check` = `ok` and per-table row counts equal
   source counts. Any mismatch → restore the backup, remove `platform.db`,
   abort with a clear error.
8. Commit `migration-manifest.json` in `state_dir`: `migrated_at`,
   `platform_schema_version`, `status`, per-source `sha256` and row
   counts. Raw key material is never printed.
9. `--rollback <ts|last>`: restore sources from the backup, checkpoint +
   remove `platform.db`, unlink the manifest.
10. Non-interactive runs require `--yes`.

## Timeline (not implemented in P6.1)

Future P6.3/P6.4 tables, listed for scope only: `providers` (non-secret
`.env` provider fields, **never API keys**), `request_log` (metadata
only), `events` (durable audit log), and `apps` (derived view, not a
stored table). No auto-purge runs during P6.1 (purge is a P6.3 concern).
