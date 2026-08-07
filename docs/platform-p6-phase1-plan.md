# P6.1 — Phase Plan: `platform.db` Foundation + `relay migrate`

Status: **Phase-planning only. No code yet.** Approved P6 design
(`docs/platform-p6-plan.md`, §2, §7.1, §8) broken into its first safe sub-phase.
Implementation starts only after this mini-plan is approved.

Source: `docs/platform-p6-plan.md` (approved, decisions A–K carried). Prior intent:
`docs/platform-implementation-roadmap.md` §P6 (rev 2), `docs/platform-p5-plan.md`
(`api_keys` folded into `platform.db` in P6), `docs/platform-p5-phase4-plan.md`
(KeyStore schema "folded unchanged"), `docs/platform-p1-plan.md` §8.2.

Constraints (standing): **no code until this plan is approved**; no `PROJECT_LOG.md`
edits, ever; no commit without explicit approval; plan docs stay untracked; run the
suite with `.venv\Scripts\python.exe`.

Exit-gate convention (from P6 plan §6.1, §8): every sub-phase ends with the full
suite green. The 28 pre-existing stale failures in `tests/test_rc_validation.py`
(monkeypatching `create_nvidia_provider` onto `app.core.relay`, which never existed)
are a **known pre-existing caveat**: P6.1's gate is *no new failures beyond those 28*
plus all P6.1 tests green. The RC rewrite lands in P6.2.

---

## 0. Objective

Make `state_dir/platform.db` the single SQLite surface of the application and
provide a safe, reversible one-time migration from the P5 layout, while preserving
every existing public API and every runtime behavior:

- Fold `relay_keys.db` (schema v1, `api_keys`) and `relay_state.db` (schema v3,
  learned/telemetry/quality/decision tables) into one `platform.db`.
- Seed `model_status` (new table) from `availability.json` at migration time.
- Add `relay migrate` (one-shot, idempotent, dry-run/rollback/verify) and its
  schema doc `docs/platform-db-schema.md`.
- Rename the persistence path to `state_dir/platform.db` and standardize all
  docstrings on `platform.db`.
- Re-point `KeyStore`/`StateStore` internals to the shared platform store, keeping
  their public class names and methods unchanged.

What this phase does **not** do: `providers` table (P6.3 config swap), `events` /
`request_log` (P6.3), `apps` view + `client_tracking` retirement (P6.4), legacy
provider shim deletion / `RUNTIME_READY` / RC-test rewrite / keyring-blind fixes
(P6.2), `.env`-backed provider-key removal (P6.3, Decision E).

---

## 1. Current-state recap (facts the plan is built on)

| Surface | File | Default path | Owner today |
| --- | --- | --- | --- |
| Relay key hashes | `relay_keys.db` | `state_dir / "relay_keys.db"` (`key_store.py:160`) | `KeyStore` |
| Learned/telemetry/quality/decision | `relay_state.db` | `_resolve_persistence_path()` (`config.py:83-87`) | `StateStore` |
| Availability snapshots | `availability.json` | `state_dir / "availability.json"` (`setup/persistence.py:15`) | `persistence` |
| Config | `.env` | `env_file` (`config.py:37-59`) | `config_store` |
| Setup marker | `state.json` | `state_dir / "state.json"` | `setup_state` (stays JSON, Decision I) |

- Both stores already share the `MIGRATIONS` dict + `PRAGMA user_version` convention,
  WAL, `busy_timeout = 5000`, corrupt-file backup-and-reopen, and (POSIX) `0600` on
  the DB + `-wal`/`-shm` + `.corrupt-*.bak` (`key_store.py:453-549`,
  `state_store.py:587-661`).
- `state_dir` = `.relay/` in a source checkout, per-user data dir when installed
  (`config.py:69-77`). The legacy `relay_state.db` lives at the **project root**
  (source checkout) or the user data dir (installed) — not inside `state_dir`.
- Runtime wiring: `relay.py:92-132` gates `StateStore` + `StateFlusher` on
  `settings.persistence_enabled` / `settings.persistence_path`; auth builds the
  `KeyStore` lazily at `auth.py:181-192` (bootstrap `RELAY_API_KEY` path never reads
  the DB and fails closed on store outage).
- Full suite at P6 start: `1785 passed, 15 skipped, 28 failed` (all 28 = stale RC).

---

## 2. Design decisions for P6.1

### D1 — Single `platform.db` at `state_dir/platform.db` (Decision A/B, carried)

The target file is `state_dir/platform.db`:
- source checkout → `.relay/platform.db`
- installed → `%LOCALAPPDATA%\relay\platform.db` (user data dir)

`_resolve_persistence_path()` is re-pointed to `state_dir / "platform.db"`
(`config.py:83-87`), and `KeyStore`'s default becomes `str(state_dir / "platform.db")`
(`key_store.py:160`). `PERSISTENCE_ENABLED`/`PERSISTENCE_PATH` stay as gates;
`PERSISTENCE_PATH` remains the documented compat/override knob.

### D2 — `PlatformStore` owns the file; stores keep per-consumer connections

New `app/services/platform_store.py` is the single owner of the file-level concerns:

- path resolution and default `state_dir/platform.db` discovery,
- the combined `MIGRATIONS` dict and the `PRAGMA user_version` runner,
- an in-process migration lock (a module-level `threading.Lock`) so concurrent
  opens of the same file cannot race a migration,
- `0600` on the DB + `-wal`/`-shm` sidecars + `.corrupt-*.bak` backups (POSIX),
- corrupt-file backup-and-reopen.

`KeyStore` and `StateStore` are re-pointed to obtain their guarded connection via
`PlatformStore` (public class names, constructor signatures, and methods unchanged).
Each consumer keeps its own `threading.Lock` + connection, preserving today's
flusher/auth connection separation (P6 plan §2.5: WAL allows one writer + readers;
`busy_timeout` bounds contention). `StateStore()` with no path keeps `:memory:`
(test isolation; relay always passes `settings.persistence_path`).

Rationale vs. a single shared connection: zero regression to the write-behind
flusher's isolation, minimal churn to existing store internals (`_conn`/`_lock`
stay on the facades so `test_wal_mode`-style tests survive), and migration races are
eliminated by the shared migration lock rather than by connection sharing.

### D3 — Combined schema versioning mirrors the legacy history

The platform `MIGRATIONS` replay the source stores' historical steps so the
facade constants stay meaningful and upgrades are idempotent:

| Version | Tables added | Source |
| --- | --- | --- |
| 1 | `api_keys` | `key_store.py:58-75` DDL, unchanged |
| 2 | `learned_state`, `telemetry`, `telemetry_failures` (+ index) | state v1 DDL, unchanged |
| 3 | `learned_state.provider_status_expires_wall`; `telemetry.ewma_success`/`ewma_latency_ms`/`last_updated_wall`; `quality_aggregates`; `decision_stats` | state v2/v3 DDL, unchanged |
| 4 | `model_status` | new (see §3) |

`PlatformStore.SCHEMA_VERSION = 4`. `KeyStore.SCHEMA_VERSION` and
`StateStore.SCHEMA_VERSION` both reference it, so `test_key_store` / `test_state_store`
version assertions adapt with small edits.

### D4 — `model_status` canonical 3-state mapping

`availability.json` uses `available` / `overloaded` / `unavailable`
(`availability.py:16-18`). The roadmap's `model_status` is `available` /
`degraded` / `unavailable`. Migration maps `overloaded` → `degraded`. Nothing reads
`model_status` yet in P6.1 (it is a seeded snapshot; `availability.json` stays the
live source until P6.3 retires writes).

### D5 — `.env` is backed up but not imported in P6.1

`relay migrate` hard-copies `.env` into the backup set (P6 plan §2.3 step 2) but
does **not** create/import the `providers` table — that is P6.3's config swap.
Provider keys (deprecated env path) and keyring material are never written into
`platform.db`. Only the scrypt `api_keys` hashes move, 1:1.

### D6 — Legacy-unmigrated guard (safe transition rule)

The silent-loss footgun: an upgraded install with legacy files whose runtime opens a
fresh empty `platform.db` would orphan existing keys. Guard rule:

- When **no** `RELAY_DATA_DIR` / `PERSISTENCE_PATH` override is set, and a legacy DB
  source exists (`state_dir/relay_keys.db` or the legacy `relay_state.db` location)
  and the target `platform.db` does not exist, the first store open raises
  `PlatformStoreError("legacy state detected — run `relay migrate` first")`.
  - Auth (keys): the error propagates and store auth fails closed (401), matching
    today's outage contract (`auth.py:261-272`). The bootstrap key keeps working.
  - State (relay): `_init_persistence` catches it, warns, disables persistence, and
    sets `persistence_init_error` — graceful, matching today's corrupt-state path.
- `availability.json` and `.env` presence never block (both are transient or
  bootstrap).
- An explicit `PERSISTENCE_PATH`/`RELAY_DATA_DIR` override bypasses the guard: the
  operator is explicitly choosing a legacy/compat layout (retained for the
  documented downgrade path).

Fresh installs have no legacy DBs, so the guard is inert and the runtime creates
`platform.db` directly.

### D7 — Migration manifest is a JSON file

`state_dir/migration-manifest.json` records the last successful run
(`migrated_at`, `platform_schema_version`, per-source path + SHA-256 digest, per-table
row counts). Re-run detection and `--rollback <last>` use it; `last` = newest
`state_dir/backups/<ts>/`. Keeps migration bookkeeping out of the DB schema
(P6 plan §2.2's table list is the schema; the manifest is not a table).

### D8 — Rollback semantics

`relay migrate --rollback <ts|last>` copies the sources back from
`state_dir/backups/<ts>/` and removes `platform.db` (after a WAL checkpoint). The
sources were copied, never moved, so a manual restore always works. After rollback,
the P6.1 runtime re-engages the D6 guard; a full downgrade is the documented code
rollback (previous release + restored legacy files, `docs/rollback-procedure.md`).

---

## 3. P6.1 `platform.db` schema (for `docs/platform-db-schema.md`)

Tables implemented in P6.1 (DDL = source DDL verbatim):

- `api_keys` — from `key_store.py:58-75`. `key_hash`/`key_salt`/`kdf`/`scopes`/
  `expires_at`/`created_at`/`last_used_at`/`revoked_at`. `key_hash` stays
  **unindexed** (constant-time scan; the DB cannot leak which key matched).
- `learned_state`, `telemetry`, `telemetry_failures` (+ `idx_telemetry_failures_pair`),
  `quality_aggregates`, `decision_stats` — from `state_store.py:30-110`, verbatim.
- `model_status` — new, seeded at migration:

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

Columns map from `availability.json` `providers[pid].models[]`
(`setup/persistence.py:68-81`), with `overloaded` → `degraded` (D4).

Privacy contract (documented in the schema doc, unchanged): `platform.db` stores
**scrypt hashes only** for keys and **metadata only** elsewhere — never raw keys,
prompts, responses, proxy credentials, or correlation ids. `providers`,
`request_log`, `events`, `apps` are listed as future P6.3/P6.4 tables in the doc's
timeline, not implemented now.

---

## 4. `relay migrate` command

New `app/cli/migrate.py`, registered in `app/cli/__init__.py` as `relay migrate`.

```
relay migrate [--dry-run] [--yes] [--rollback <timestamp|last>] [--data-dir <dir>]
```

Algorithm (P6 plan §2.3, tightened for P6.1):

1. **Resolve** layout: `state_dir`, legacy `relay_state.db` location, `env_file`.
   `--data-dir` overrides for tests.
2. **Preflight / dry-run** (`--dry-run`): print the plan (source paths, tables,
   expected row counts), change nothing, exit 0. Missing optional sources
   (`relay_state.db` when persistence was never on) are fine.
3. **Re-run detection**: manifest exists with unchanged source digests → print
   "already migrated", exit 0. `platform.db` exists without a manifest → warn; require
   `--yes` to re-import.
4. **Backup**: copy every source (DB + `-wal`/`-shm` sidecars + `availability.json` +
   `.env`) into `state_dir/backups/<ts>/`; `0600` on POSIX. Sources are never moved
   or deleted.
5. **Create**: open `platform.db` via `PlatformStore` (runs migrations to v4).
6. **Import**: `api_keys` ← `relay_keys.db` 1:1; the five state tables ←
   `relay_state.db` 1:1; `model_status` ← `availability.json` (D4). No `.env`
   provider import (D5). No purge (Decision K's auto-purge is P6.3).
7. **Verify**: `PRAGMA integrity_check` = `ok`; per-table row counts equal source
   counts. Any mismatch → restore the backup, remove `platform.db`, abort with a
   clear error.
8. **Commit**: write `migration-manifest.json` (D7). Never prints key material.
9. **Rollback** (`--rollback <ts|last>`): copy sources back, checkpoint + remove
   `platform.db`, print next steps.
10. **Guard**: non-interactive runs require `--yes` (mirrors the P5 migrate guard).

---

## 5. Transition behavior (compat period)

- **Pre-migration legacy install**: runtime refuses to open an empty `platform.db`
  (D6); `relay migrate` is the required one-time step and works without auth.
- **Post-migration**: stores read/write `platform.db`; `relay_keys.db` /
  `relay_state.db` / `availability.json` remain on disk, inert, as the rollback
  target. `availability.json` stays the live setup-scan source until P6.3.
- **Fresh install**: no legacy DBs → `platform.db` created on first open;
  `relay migrate` later is a no-op (records a manifest only if sources exist).
- **Behavior invariants preserved** (verification checklist): bootstrap
  `RELAY_API_KEY` never reads the DB; store auth fails closed on outage; keyring
  resolution, provider loading, and state-store export/import semantics unchanged;
  `app/ui/data.py:252` "Persistence path" row reflects the new path automatically.

---

## 6. Files changed / untouched

### New
- `app/services/platform_store.py` — file owner: migrations, permissions, corrupt
  recovery, migration lock, default-path resolution, D6 guard helper.
- `app/cli/migrate.py` — `relay migrate` command (D5/D7/D8).
- `docs/platform-db-schema.md` — implemented DDL + privacy contract + timeline.

### Modified
- `app/core/config.py` — `_resolve_persistence_path()` → `state_dir/platform.db`
  (lines 83-87); comment at 421 (relay_keys.db → platform.db).
- `app/services/key_store.py` — default path (`key_store.py:160`), module docstring
  (`:4`), delegate open/migrate/permissions/corrupt to `PlatformStore`; public API
  unchanged.
- `app/services/state_store.py` — delegate open/migrate/permissions/corrupt to
  `PlatformStore`; `:memory:` default kept; `SCHEMA_VERSION` = platform version.
- `app/core/relay.py` — `_init_persistence` guard handling (D6) + verify wiring
  (line 103 path unchanged in shape).
- `app/security/auth.py` — docstring (`:11` relay_keys.db → platform.db); default
  store wiring; **behavior unchanged** (bootstrap precedence, fail-closed).
- `app/services/state_flusher.py` — docstring only (no functional change; it takes a
  `StateStore` instance, not a path).
- `app/setup/persistence.py` — docstring (`:5` relay.db → platform.db /
  `model_status`); expose a read-only import hook for migration (writes unchanged).
- `app/cli/__init__.py` — register `relay migrate` (+ help text).
- `app/services/config_store.py`, `app/services/client_tracking.py` — docstring
  standardization only (`relay.db` → `platform.db`).

### Untouched (P6.1)
Hot path / routing (`chat_service.py`, `async_chat_service.py`, `routing.py`,
`scoring.py`, `decision_engine.py`, `candidate_builder.py`, `health_*.py`); provider
runtime (`base.py`, `availability.py`, `*_client.py`, `registry.py`); API wire
(`app/api/*.py`, `app/main.py`); server/TUI (`app/core/server.py`, `app/core/terminal.py`,
`app/ui/*`); `app/services/ops_store.py`, `app/services/provider_key_store.py`,
`app/services/setup_state.py`, `app/cli/keys.py`, `app/cli/provider_keys.py`;
`PROJECT_LOG.md`.

---

## 7. Tests

### New
- `tests/test_platform_store.py` — fresh open at `SCHEMA_VERSION` with the full table
  set; migration idempotency (reopen); WAL + `busy_timeout`; `0600` + sidecars +
  `.corrupt-*.bak` (POSIX skip); corrupt backup-and-reopen; two concurrent opens of
  the same file; D6 guard (default path raises; explicit override bypasses).
- `tests/test_migrate.py` — dry-run no-op; full run imports row counts + integrity +
  backup set + manifest; idempotent re-run; missing optional sources; digest
  mismatch abort; row-count mismatch abort-and-restore; `--yes` guard; `--rollback`
  (restores sources + removes platform.db); `--data-dir`; raw key material never
  printed; `.env` provider keys never imported into `platform.db`.

### Updated
- `tests/test_packaging.py` — lines 87, 100, 455: `relay_state.db` →
  `state_dir/platform.db` (installed: `data_dir / "platform.db"`; source:
  `PROJECT_ROOT / ".relay" / "platform.db"`).
- `tests/test_key_store.py` — `test_schema_version_and_table` asserts the platform
  version (4 / `KeyStore.SCHEMA_VERSION`) instead of 1; others pass unchanged (facade
  keeps `_conn`/`_lock`, explicit tmp paths).
- `tests/test_state_store.py` — migration-runner tests (`test_schema_migration_forward`,
  `test_v2_migrates_to_v3_additively`) re-target `PlatformStore`'s runner; fresh-DB
  version assertion self-adapts to `SCHEMA_VERSION`.
- `tests/test_key_auth.py`, `tests/test_admin_keys.py`, `tests/test_key_cli.py`,
  `tests/test_security_hardening.py`, `tests/test_full_stack.py`,
  `tests/test_diagnostics.py`, `tests/test_persistence_integration.py`,
  `tests/test_persistence_metrics.py`, `tests/test_hardening.py` — expected to pass
  unchanged (explicit tmp paths / `persistence_path` monkeypatches still valid);
  verify, adjust only if a private-token assertion breaks.

### Known caveat
`tests/test_rc_validation.py` (28 stale failures) stays failing through P6.1; its
rewrite is P6.2. P6.1 must not add new failures.

---

## 8. Verification / exit gate

1. `.venv\Scripts\python.exe -m pytest tests -q` → **exactly the 28 known RC
   failures, no new failures**, and all `test_platform_store.py` + `test_migrate.py`
   tests green.
2. Migration matrix green (dry-run, run, re-run, missing sources, corruption,
   rollback, `--yes`, row-count verify).
3. Both stores read/write `platform.db`: auth store keys work; persistence flush
   round-trips through the same file.
4. Verification checklist from §5 holds (bootstrap path, fail-closed, keyring,
   provider loading, state behavior).
5. `docs/platform-db-schema.md` exists and matches the implemented DDL.
6. `PROJECT_LOG.md` untouched; no commit until explicit approval.
7. Stop after implementation and present a summary for commit approval (P6.2
   sub-phases excluded).

---

## 9. Risks / mitigations

| Risk | Mitigation |
| --- | --- |
| Legacy keys silently orphaned on upgrade | D6 guard (fail-closed keys / graceful state) + `relay migrate` is the only creator of `platform.db` from legacy data |
| Migration aborts midway | Backup-first, copy-don't-move, integrity + row-count verify, auto-restore on mismatch |
| Two consumers race the migration | PlatformStore in-process migration lock + idempotent `user_version` steps |
| Downgrade confusion | D8: rollback restores original files; downgrade = previous release (documented) |
| Test-suite regression from internals move | Facades keep `_conn`/`_lock`; explicit-path fixtures unchanged; P6.1 gate forbids new failures |
| `model_status` naming drift | D4 maps `overloaded`→`degraded` once at import; nothing reads it until P6.3 |

---

## 10. Decisions requested

| # | Decision | Recommended |
| --- | --- | --- |
| D1 | Target file `state_dir/platform.db` (carried) | `state_dir/platform.db` |
| D2 | PlatformStore owns file; stores keep per-consumer connections | Accept (§2 D2) |
| D3 | Combined schema versioning mirrors legacy history (v1–v4) | Accept |
| D4 | `model_status` maps `overloaded`→`degraded` | Accept |
| D5 | `.env` backed up but not imported into `platform.db` in P6.1 | Accept (providers table is P6.3) |
| D6 | Legacy-unmigrated guard (fail-closed keys / graceful state; overrides bypass) | Accept |
| D7 | Manifest as `state_dir/migration-manifest.json` | Accept |
| D8 | Rollback restores sources + removes `platform.db`; downgrade = previous release | Accept |
| D9 | P6.1 gate = no new failures beyond the 28 known RC stale tests; new tests green | Accept |
