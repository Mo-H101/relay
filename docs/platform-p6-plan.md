# Relay — P6 Consolidation Plan

Status: **draft — analysis only, no code changed**.
Depends on: P5 complete (commit `11a68ac`). Prior intent: `docs/platform-implementation-roadmap.md` (P6), `docs/platform-p5-plan.md`, `docs/platform-p5-phase5-plan.md`, `docs/platform-p1-plan.md` (§8.2), `docs/platform-p2d-plan.md` (§6).

This document consolidates the P6 scope from the roadmap and the technical debt
left by P4/P5, evaluates the database consolidation, and fixes scope so a review
can approve or redirect **before any code is written**. It is a plan only: no
code changes, no commit, no `PROJECT_LOG.md` update.

---

## 0. Objective

Make Relay's durable state a single, explainable, secure surface and close the
open items the P4/P5 work intentionally deferred:

1. **Database consolidation** — fold `relay_keys.db` and `relay_state.db` (plus
   the interim `availability.json` and `.env` provider config) into one
   `platform.db` with a safe, reversible `relay migrate`.
2. **Security maturity** — durable audit logging, exposed key rotation, key
   lifecycle cleanup, and a decision on the bootstrap `RELAY_API_KEY`.
3. **Cleanup** — remove compatibility layers, deprecated env paths, and
   P4/P5 technical debt (including the dead RC-validation gate).
4. **Developer experience** — provider onboarding, CLI consistency, docs gaps.
5. **Testing & release readiness** — a green regression gate and a
   production-readiness checklist.
6. **Scope control** — an explicit inventory of what changes, what does not,
   the migrations, and the rollback strategy.

---

## 1. Current-state audit (post-P5, commit `11a68ac`)

### 1.1 Durable surfaces in play

| Surface | Path | Owner | Contents | Schema |
| --- | --- | --- | --- | --- |
| State DB | `relay_state.db` (`app/core/config.py:83-87`, `_resolve_persistence_path`) | `StateStore` (`app/services/state_store.py`) | learned health, telemetry aggregates + failure history, quality aggregates, decision stats | v3 (`MIGRATIONS`) |
| Key DB | `state_dir/relay_keys.db` (`app/services/key_store.py:160`) | `KeyStore` (`app/services/key_store.py`) | scrypt-hashed API keys (`api_keys` table) | v1 |
| Config | `.env` (`app/services/config_store.py`) | `config_store` (single writer) | provider config + bootstrap `RELAY_API_KEY`; provider keys (deprecated path) | dotenv |
| Setup state | `state_dir/state.json` (`app/services/setup_state.py`) | `setup_state` | setup marker + configured providers | schema 1 JSON |
| Availability | `state_dir/availability.json` (`app/setup/persistence.py`) | `persistence` | latest per-provider probe snapshot | schema 1 JSON |
| Provider keys | OS keyring (`app/services/provider_key_store.py`) | `provider_key_store` | per-provider upstream keys | n/a |
| Ops window | in-memory (`app/services/ops_store.py`) | `ops_store` | request metadata + `key_admin` events | n/a |
| Client activity | in-memory (`app/services/client_tracking.py`) | `client_tracking` | interim "connected applications" | n/a |

All four SQLite-ish owners already share the same conventions: `MIGRATIONS`
dict + `PRAGMA user_version`, single guarded connection, WAL, `busy_timeout`,
corrupt-file backup + reopen, and (on POSIX) `0600` on the DB, `-wal`/`-shm`
sidecars, and `.corrupt-*.bak` backups.

### 1.2 Naming inconsistency

The target file is called **`relay.db`** in `config_store.py:6`,
`persistence.py:5`, `client_tracking.py:10`, and the P1/P2d plans, but
**`platform.db`** in `key_store.py:12`, the P5 plans, and the roadmap (rev 2).
The roadmap is the most recent canonical document → **standardize on
`platform.db`**.

### 1.3 Test state (as of `11a68ac`)

- Full suite: `1785 passed, 15 skipped, 28 failed`.
- All 28 failures are `tests/test_rc_validation.py`:
  `AttributeError: module 'app.core.relay' has no attribute
  'create_nvidia_provider'` (`test_rc_validation.py:210`). The file was written
  against the pre-P4 per-provider factory modules and was never updated for the
  P4.1 registry-driven factory. **This is the production-gateway validation
  suite, and it currently runs zero live assertions.** It must be repaired, not
  deleted.
- The 15 skips are intentional platform/feature skips (POSIX permission checks
  on Windows, packaging `skipif`s, one conformance `no check_model` skip).

### 1.4 Post-P5 security posture

- Store-backed Relay keys: scrypt hashes, constant-time verify, scope
  enforcement, bootstrap-key precedence, fail-closed store outage — complete.
- Provider keys: keyring-first resolution with `.env` fallback, migration
  command — complete.
- Redaction of `rl_`, `sk-`, `nvapi-`, bearer/header values — complete.
- Remaining gaps are listed in §3.

---

## 2. Focus 1 — Database consolidation

### 2.1 Decision A — one `platform.db` vs a separate key database

**Analysis.**

*Case for consolidating everything into `platform.db`:*
- Prior plans committed to it: roadmap P6 lists `api_keys` (from P5) inside
  `platform.db`; the P5 work deliberately aligned `KeyStore`'s schema and
  migration convention "so the `api_keys` table can be folded into the P6
  `platform.db` unchanged" (`key_store.py:10-12`).
- One file = one backup/restore story, one migration framework, one permission
  model (`0600`), one place to vacuum/monitor.
- Key `create` + audit `event` can later commit atomically in one transaction.

*Case for keeping `relay_keys.db` separate:*
- Failure isolation today: a corrupt `relay_state.db` only disables persistence
  (graceful, app continues); a corrupt `relay_keys.db` only breaks store-backed
  auth (fails closed). One file couples a security-critical read path to a
  high-write background flusher.
- Different operational lifetimes: key hashes are tiny and long-lived; telemetry
  grows and needs retention pruning.
- A single file makes "database is locked" contention possible between the
  auth `mark_used` write and the flush writer.

**Mitigations if consolidated (all already partially present):**
- WAL + `busy_timeout = 5000` and short, single-transaction writes (both stores
  already do this). Contention is bounded and non-fatal.
- The flusher already writes only on its own thread / explicit `flush()` —
  never on the request path (`state_flusher.py`).
- Corrupt recovery (backup + reopen) already exists in both stores; P6
  generalizes it into the shared `platform.db` owner.
- The bootstrap `RELAY_API_KEY` path reads no database, so a corrupt
  `platform.db` can never lock an operator out. Store-backed auth still fails
  closed (`auth.py`).
- The key tables are the same scrypt hashes; a single `0600` file preserves
  today's confidentiality.

**Recommendation: consolidate into one `platform.db`** at
`state_dir/platform.db` (`.relay/platform.db` for source checkouts;
`%LOCALAPPDATA%\relay\platform.db` installed). This is what the roadmap and P5
planned for, and the isolation loss is mitigated to acceptable risk. The
reviewer may veto to keep `relay_keys.db` separate (Decision A, §9); the plan
below is written so either choice is a localized wiring change.

### 2.2 Target `platform.db` schema

Owner: a new `app/services/platform_store.py` (single component that opens the
file, owns connections, migrations, permissions, and corrupt recovery). Existing
`StateStore`/`KeyStore` become thin facades over it or are deleted once
callers migrate (Decision: keep the public class names/APIs, re-point
internals — see §7).

Adopt the existing `MIGRATIONS`-dict + `PRAGMA user_version` convention. Table
set (the roadmap's `platform.db` set, reconciled with P1 §8.2):

| Table | Source | Notes |
| --- | --- | --- |
| `api_keys` | fold from `relay_keys.db` v1 | **unchanged DDL**, migration 1:1 |
| `learned_state`, `telemetry`, `telemetry_failures`, `quality_aggregates`, `decision_stats` | fold from `relay_state.db` v3 | DDL unchanged |
| `model_status` | `availability.json` + health snapshots + learned feedback | new; durable 3-state (`available`/`degraded`/`unavailable`) per (provider, model) |
| `providers` | `.env` provider config (non-secret fields only) | enabled, base_url, priority, model priority; **never API keys** |
| `request_log` | new | metadata only (see privacy contract) |
| `events` | new | durable audit log (see §3.4) |
| `apps` | derived | view over `api_keys` × `request_log`, not a stored table |

Privacy contract (unchanged, now explicit in `docs/platform-db-schema.md`):
`request_log` and `events` store **metadata only** — never prompts, responses,
raw keys, proxy credentials, or correlation ids (`relay.py:218`,
`security.md`).

### 2.3 Migration strategy (`relay migrate`)

New subcommand `relay migrate` (one-shot, idempotent). Algorithm:

1. **Preflight** — detect sources (`relay_keys.db`, `relay_state.db`,
   `availability.json`, `.env`). Missing optional sources (e.g. no
   `relay_state.db` because `PERSISTENCE_ENABLED` was never on) are fine.
2. **Backup** — hard-copy every source (DB + `-wal`/`-shm` + JSON + `.env`) into
   `state_dir/backups/<timestamp>/`. Migration never moves or deletes the
   sources; they remain the rollback target.
3. **Create** — create `platform.db`, run migrations to the current schema
   version, `0600` the file and sidecars on POSIX.
4. **Import** — copy rows: `api_keys` 1:1; learned/telemetry/quality/decision
   1:1 with the existing column mapping; `model_status` from `availability.json`
   snapshots; `providers` from `.env` non-secret fields (keys stay in the
   keyring / legacy `.env` fallback).
5. **Verify** — `PRAGMA integrity_check`, row counts per table compared against
   source counts. Any mismatch aborts and restores the backup.
6. **Commit** — write a migration manifest (timestamp + source digests) so a
   re-run is a no-op. Runtime then points its stores at `platform.db`.
7. **Dry-run** — `relay migrate --dry-run` prints the plan (paths, tables, row
   counts) and changes nothing. Non-interactive runs require `--yes`
   (mirroring the P5 migrate command's guard).

Flags: `--dry-run`, `--yes`, `--rollback <timestamp|last>`, `--data-dir`
(override for tests). Never prints key material.

### 2.4 Rollback strategy

- **Data rollback**: `relay migrate --rollback` restores the sources from
  `state_dir/backups/<ts>/` (and removes `platform.db` or marks it stale). The
  sources were copied, not moved, so a manual restore always works even without
  the command.
- **Code rollback**: existing `docs/rollback-procedure.md` pattern — check out
  the previous artifact. Because the legacy files are intact, a downgrade can
  re-point stores at them via `RELAY_DATA_DIR`/`PERSISTENCE_PATH` (compat
  override retained).
- **Config rollback**: during the P6.3 transition, `config_store` keeps writing
  a `.env` mirror, so reverting the DB-backed config change is a flag flip, not
  a data migration.

### 2.5 Failure isolation (post-consolidation)

- Store-backed auth **fails closed** (401) on `platform.db` outage; bootstrap
  `RELAY_API_KEY` continues to work (no DB read) — unchanged from today.
- Persistence degrades gracefully (flusher logs, app continues) — unchanged.
- Corruption of `platform.db` triggers the existing backup-and-reopen path;
  the flusher stops writing, auth keeps failing closed, and the operator has
  the `backups/` copy.
- The flusher and the auth path use separate connections on the same file (WAL
  allows one writer + readers; busy-timeout bounds contention).

---

## 3. Focus 2 — Security maturity

### 3.1 Remaining gaps (post-P5)

| # | Gap | Current state | P6 disposition |
| --- | --- | --- | --- |
| G1 | No durable audit log | `ops_store` records `key_admin` in-memory only (`ops_store.py:101-119`), lost on restart | `events` table + `EventLog` service (§3.4) |
| G2 | Rotation not exposed | `KeyStore.rotate` exists internally (`key_store.py:300-317`), no CLI/API surface | `relay keys rotate` + `POST /admin/keys/{id}/rotate` (§3.2) |
| G3 | No revoked/expired-key cleanup | rows accumulate forever; `memory_counts` reports them | `relay keys prune` + automatic purge in `relay migrate` (§3.3) |
| G4 | Bootstrap `RELAY_API_KEY` in plaintext `.env` | documented P6 item (`security.md`) | Decision D (§9): keep in `.env` for P6 (it is the recovery path and is read per request); vault-adjacent move deferred to P7 |
| G5 | No auth-failure rate limiting | each failed store auth runs an O(n) scrypt scan | Document as known limitation + bucket-count `events`; actual rate limiting is out of P6 scope (deploy-proxy note) |
| G6 | Keyring-blind first-run/wizard detection | `_has_usable_provider` reads `settings.<key_attr>` (`app/cli/__init__.py:10-28`); wizard reads `config_store.get_env` (`wizard.py:201`) — neither sees keyring-only keys | Fix to `resolve_provider_key` (§4.2, P6.2) |
| G7 | Provider keys still in `.env` until migrated | deprecated env vars still honored as fallback | keep fallback while keyring disabled; Decision E enforcement (§9) |

### 3.2 Key rotation

- `KeyStore.rotate(key_id)` already creates the replacement and revokes the
  original in one operation. Add:
  - `relay keys rotate <key_id>` — prints the new raw key exactly once.
  - `POST /admin/keys/{id}/rotate` (admin scope) — returns the new key once,
    with an `events` audit row.
- Document the rotation runbook (create → migrate clients → revoke) in
  `security.md` and `deployment.md`.
- Provider-key rotation already works via `relay provider keys set <id>`; add a
  masked-confirm prompt and an audit event.

### 3.3 Key lifecycle management

- Full lifecycle is already: `create → active → expired/revoked → delete`.
- Add a purge policy for terminal rows (`revoked`/expired): `relay keys prune`
  (default: dry-run) and an automatic purge hook during `relay migrate`.
- `list` already shows `expires_at`/`last_used_at`; add an "expiring soon" flag
  in `relay keys list` output.

### 3.4 Audit logging (`events`)

- New `EventLog` service writing to the `events` table; metadata only:
  `ts`, `actor` (opaque `key_id` or `"bootstrap"`), `action`, `target`,
  `outcome`, `detail` (no secrets).
- Emitted on: key create/revoke/delete/rotate, config writes, store open/close,
  auth failures (bucketed and rate-limited in-process), migration runs,
  `request_log` may be correlated by time window only (no correlation id).
- Read surfaces: `relay events` CLI and `GET /admin/events` (admin scope), with
  retention pruning via the existing `PERSISTENCE_RETENTION_DAYS` knob.
- Privacy tests: assert no prompts, responses, keys, or correlation ids in any
  `events` row.

---

## 4. Focus 3 — Cleanup

### 4.1 Temporary compatibility layers

| Item | Location | Disposition |
| --- | --- | --- |
| Legacy provider shims | `app/providers/nvidia.py`, `openai.py`, `lmstudio.py` | Delete after re-pointing the 5 test files (`test_nvidia_provider.py`, `test_provider_factory.py`, `test_lmstudio_provider.py`, `test_lmstudio_integration.py`, `test_lmstudio_real.py`) to `build_runtime_provider(PROVIDER_REGISTRY[...])` (P6.2) |
| `RUNTIME_READY` manual set | `app/providers/registry.py:200-206`, checked in `relay.py:162`, `reload.py:102,117`, `wizard.py:257,265` | Replace with `runtime_ready: bool` field on `ProviderDefinition` (P6.2) |
| Interim `client_tracking` | `app/services/client_tracking.py` | Replaced by `apps` view over `request_log` (P6.4); module becomes a thin facade or is deleted |
| `availability.json` | `app/setup/persistence.py` | Becomes a read-only migration input for `model_status`; writes retire (P6.3) |
| Ops tail | `app/services/ops_store.py` | Stays in-memory per its documented contract; `request_log` becomes the durable layer |
| `.env` provider-key vars | `config_store`/`Settings` | Deprecated since P5.2; enforcement per Decision E (§9) |

### 4.2 Deprecated environment-variable paths

- Provider key vars (`NVIDIA_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
  `GEMINI_API_KEY`, `LMSTUDIO_API_KEY`) — keep as keyring-disabled fallback;
  `security.md` documents removal in P6. Decision E defines the enforcement.
- **Correctness fix (P6.2):** after `relay provider keys migrate` removes the
  `.env` keys, `_has_usable_provider` (CLI first-run detection) and the wizard's
  "existing key" check are keyring-blind and would wrongly re-launch setup on a
  keyring-only install. Both must route through `resolve_provider_key`
  (`factory.py:33`).

### 4.3 Unused / parsed-but-dead settings

| Item | Location | Disposition |
| --- | --- | --- |
| `default_provider` | `config.py:361` (parsed, intentionally unused) | Decision H: remove |
| `openrouter_api_key`, `groq_api_key` | `config.py:297,319` | Decision H: wire provider defs or drop parsing |
| `relay.db` vs `platform.db` naming | `config_store.py:6`, `persistence.py:5`, `client_tracking.py:10` | Standardize docstrings on `platform.db` (P6.1) |
| `relay_state.db` path | `_resolve_persistence_path` (`config.py:83-87`) | Superseded by `state_dir/platform.db` (P6.1) |

### 4.4 Technical debt from P4/P5

- **Dead RC gate**: 28 stale tests in `test_rc_validation.py` monkeypatching
  `create_nvidia_provider` onto `app.core.relay` (never existed). Rewrite to
  registry-driven factories + `app.main` router wiring so the production
  workflow suite runs again (P6.2 — highest priority).
- CLI exit-code inconsistency (`_provider_or_fail` exits 1 vs `parser.error`
  exit 2) and duplicated scope vocabulary (`_VALID_SCOPES` in `api/keys.py:28`
  vs `_parse_scopes` in `cli/keys.py:105`) — unify (§5.2).
- `PERSISTENCE_ENABLED`/`PERSISTENCE_PATH` semantics after consolidation — see
  §7.1 (kept as gates; path becomes `platform.db`).

---

## 5. Focus 4 — Developer experience

### 5.1 Provider onboarding

Current cost to add a provider: registry entry + client class + `Settings`
attrs + `RUNTIME_READY` edit + `.env.example` + docs. Improvements (P6.2):
- `runtime_ready` flag on `ProviderDefinition` (removes the manual set).
- `relay providers list` / `relay models` / `relay status` subcommands (roadmap
  P1 listed them; never built) so provider state is inspectable without the TUI.
- `docs/provider-onboarding.md`: checklist + template (new doc).
- Registry conformance test: every defn has an instantiable `client_class`,
  consistent `key_env`↔`key_attr`/`enabled_env`↔`enabled_attr`, and a `Settings`
  attribute that exists (extend `test_provider_registry.py`).

### 5.2 CLI consistency

- Canonicalize provider-key grammar: `relay provider keys` stays canonical;
  the `relay keys provider` alias (added in P5.5) is either documented as a
  convenience or dropped (Decision J).
- Uniform exit codes: `0` success, `1` operational failure, `2` usage error.
  Audit every subcommand.
- One shared scope constant (`app/cli/` imports `_VALID_SCOPES` rather than
  re-deriving).
- New commands in P6: `relay migrate`, `relay keys rotate`, `relay keys prune`,
  `relay apps`, `relay events` (and the §5.1 status commands).
- Consistent `--json` output shape across `list`-style commands.

### 5.3 Documentation completeness

| Doc | Status | P6 action |
| --- | --- | --- |
| `docs/platform-db-schema.md` | **missing** (P1 deliverable gap; referenced by `persistence.py:5`) | create with DDL, privacy contract, migration timeline |
| `docs/provider-onboarding.md` | missing | create |
| `docs/deployment.md` | present | add `relay migrate` runbook + platform.db profile |
| `docs/security.md` | present | add audit/rotation/prune; update bootstrap-key note |
| `docs/configuration.md` | present | update key-storage precedence after config swap |
| `docs/rollback-procedure.md` | present | add platform.db/migrate rollback steps |
| `.env.example` | present | reflect deprecated/removed vars |
| `docs/blockers-before-public-release.md` | present | unchanged (environment blocker: OpenAI key quota) |

---

## 6. Focus 5 — Testing and release readiness

### 6.1 Final regression gates (P6 exit criteria)

- Full suite green: `pytest tests -q` → **0 failures** (fixes the 28 RC tests),
  or a reviewed-and-justified exception list of zero stale items.
- New `tests/test_platform_store.py`, `tests/test_migrate.py` green (migration
  up/down/rollback, integrity, idempotent re-run, failure-injection).
- Conformance suite (`test_provider_conformance.py`) stays green.
- Live smoke `tests/run_live_smoke.py`: NVIDIA green; OpenAI remains blocked by
  the documented key-quota environment issue (not a code failure).
- POSIX permission tests must run on Linux CI (they skip on Windows); the 15
  skips are intentional platform/feature skips — reviewed and kept.

### 6.2 Missing integration tests

- End-to-end lifecycle: setup → keyring migrate → `relay migrate` → restart →
  store auth → chat → audit events durable.
- `relay migrate` matrix: dry-run, no-op re-run, missing sources, corruption
  injection, rollback, row-count verification, `--yes` guard.
- Consolidated-file concurrency: flusher write vs auth `mark_used`/`verify`
  (no lock escalation).
- Audit/privacy: no prompts, responses, keys, or correlation ids in
  `request_log`/`events`.
- Rotation round-trip: old key rejected after rotate, new key accepted.
- `apps` view + `request_log` retention pruning.
- Keyring-blind first-run/wizard regression (G6).

### 6.3 Skipped tests review

Enumerate at P6 start; classify each as intentional (platform/feature) or a gap
(e.g. the conformance `no check_model` skip for providers lacking
`check_model`). Report the classification in the P6.1 mini-plan. Ensure the
Linux CI job runs the POSIX permission tests.

### 6.4 Production readiness checklist

- Update `docs/release-candidate-checklist.md` for P6 surfaces (audit log,
  rotation, apps, migrate).
- Re-run `docs/v1.0.0-readiness-report.md` audit gates.
- Update `docs/blockers-before-public-release.md` (OpenAI quota remains the
  only environment blocker).
- Document the platform.db backup cadence (see rollback procedure).

---

## 7. Scope control

### 7.1 Files expected to change (grouped by sub-phase)

**P6.1 — `platform.db` foundation + `relay migrate`**
- New: `app/services/platform_store.py`, `app/cli/migrate.py`,
  `docs/platform-db-schema.md`.
- Modify: `app/core/config.py` (platform.db path + naming),
  `app/services/state_store.py`, `app/services/key_store.py` (re-point to
  `platform_store`; keep public class APIs), `app/services/state_flusher.py`,
  `app/core/relay.py` (wire), `app/security/auth.py` (store path),
  `app/setup/persistence.py` (availability read for migration),
  `app/cli/__init__.py` (register `migrate`), `docs/deployment.md`,
  `docs/rollback-procedure.md`, `docs/security.md`.
- Tests: new `test_platform_store.py`, `test_migrate.py`; updates to
  `test_key_store.py`, `test_state_store.py`, `test_packaging.py`,
  `test_key_auth.py`, `test_admin_keys.py`.

**P6.2 — cleanup & debt**
- Delete: `app/providers/nvidia.py`, `app/providers/openai.py`,
  `app/providers/lmstudio.py`.
- Modify: `app/providers/registry.py` (`runtime_ready`),
  `app/providers/factory.py`, `app/services/reload.py`, `app/core/relay.py`,
  `app/cli/__init__.py` (`resolve_provider_key` in `_has_usable_provider`),
  `app/setup/wizard.py` (keyring-aware current key), `app/core/config.py`
  (Decision H removals).
- Tests: rewrite `test_rc_validation.py`; re-point `test_nvidia_provider.py`,
  `test_provider_factory.py`, `test_lmstudio_provider.py`,
  `test_lmstudio_integration.py`, `test_lmstudio_real.py`; extend
  `test_provider_registry.py`.

**P6.3 — config swap + audit + rotation**
- New: `app/services/event_log.py`, `app/api/events.py` (or extend
  `app/api/admin.py`), `tests/test_event_log.py`, `tests/test_request_log.py`,
  `tests/test_key_rotate.py`.
- Modify: `app/services/config_store.py` (platform.db-backed, `.env` mirror),
  `app/api/keys.py` (rotate endpoint), `app/cli/keys.py` (`rotate`, `prune`),
  `app/cli/provider_keys.py` (audit events), `app/api/middleware.py`
  (request_log capture), `app/setup/persistence.py` (retire writes),
  `docs/configuration.md`, `docs/security.md`.

**P6.4 — usage/apps**
- New: `app/api/apps.py`, `app/cli/apps.py`, `tests/test_apps.py`.
- Modify: `app/services/client_tracking.py` (facade or delete),
  `app/ui/data.py` (read projection only — **no TUI screen changes**),
  `docs/deployment.md`.

### 7.2 Files that must remain untouched

- **Hot path / routing**: `app/services/chat_service.py`,
  `app/services/async_chat_service.py`, `app/services/routing.py`,
  `app/services/scoring.py`, `app/services/decision_engine.py`,
  `app/services/candidate_builder.py`, `app/services/health_checker.py`,
  `app/services/health_refresher.py`, `app/services/health_store.py` — the
  consolidation must not alter candidate ordering, failover semantics, or any
  health-band invariant.
- **Provider runtime contract**: `app/providers/base.py`,
  `app/providers/availability.py`, `app/providers/*_client.py` — OpenAI wire
  compatibility and provider behavior unchanged. No new provider integrations
  in P6 (OpenRouter/Groq wiring is a decision, deferred).
- **API wire behavior**: `app/api/chat.py`, `app/api/openai.py`,
  `app/api/feedback.py`, `app/api/decision.py`, `app/api/metrics.py`,
  `app/api/health.py`, `app/api/diagnostics.py` — no request/response contract
  changes.
- **Server/TUI**: `app/core/server.py`, `app/core/terminal.py`, `app/main.py`
  (except router additions), `app/ui/*` screens.
- **Auth contract**: `app/security/auth.py` behavior (path allowlist,
  bootstrap-precedence, fail-closed) unchanged; only the store wiring may move.
- **Settings bootstrap**: `.env` must still be read at boot; `reload_settings`
  semantics unchanged.
- `PROJECT_LOG.md` — never modified (standing instruction).

### 7.3 Migrations required

| From | To | Notes |
| --- | --- | --- |
| `relay_keys.db` (schema v1) | `platform.db` `api_keys` | 1:1 DDL, unchanged |
| `relay_state.db` (schema v3) | `platform.db` learned/telemetry/quality/decision tables | 1:1 column mapping |
| `availability.json` | `platform.db` `model_status` | latest snapshot per provider |
| `.env` provider config (non-secret) | `platform.db` `providers` | keys never imported here |
| `relay_state.db` path | `state_dir/platform.db` | naming + location change |

### 7.4 Rollback strategy (summary)

| Layer | Rollback |
| --- | --- |
| Data | `relay migrate --rollback`; sources preserved by copy in `state_dir/backups/<ts>/` |
| Code | existing `docs/rollback-procedure.md` (checkout previous artifact); legacy files + `RELAY_DATA_DIR`/`PERSISTENCE_PATH` compat overrides keep a downgrade working |
| Config | `.env` mirror during P6.3 transition; revert is a flag flip |
| Auth | bootstrap `RELAY_API_KEY` never depends on the DB (recovery path) |

### 7.5 Explicitly out of scope

- P7 configuration management (`relay config show/validate/diff`, TUI config
  panel) — P6 only lays the storage seam.
- P8 CI/GitHub Actions wiring and client integration guides.
- Auth rate limiting infrastructure (documented gap, deploy-proxy mitigation).
- New provider integrations (OpenRouter/Groq wiring only if Decision H says so).
- TUI redesign; personality features.
- Moving the bootstrap `RELAY_API_KEY` off `.env` (Decision D).

---

## 8. Proposed sub-phase breakdown and gates

Each sub-phase gets its own mini-plan (like the P4/P5 phase plans) before
implementation, and each ends with a green gate. No sub-phase may start while
the previous gate is red.

| Sub-phase | Scope | Exit gate |
| --- | --- | --- |
| P6.1 | `platform.db` + `relay migrate` + schema doc + path rename | migrate matrix green; both stores read/write `platform.db`; full suite green |
| P6.2 | cleanup: shims, `RUNTIME_READY`, RC-test rewrite, keyring-blind fixes, dead settings | 28 RC tests green; 0 suite failures; conformance green |
| P6.3 | config swap (`.env` mirror), `events` audit, rotation, prune | audit/rotation/request_log tests green; config precedence documented |
| P6.4 | `apps` view + `relay apps`; retire `client_tracking`; checklist/readiness updates | apps + retention tests green; release-candidate checklist updated |

Regression gate for every sub-phase: full suite green (0 failures, reviewed
skips), `/v1` wire behavior + `RELAY_API_KEY` bootstrap + `.env` compat + CLI
entry points keep working (roadmap gate).

---

## 9. Decisions required from the reviewer

| # | Decision | Recommended |
| --- | --- | --- |
| A | Single `platform.db` vs separate `relay_keys.db` | **Single `platform.db`** (§2.1) |
| B | `platform.db` location | `state_dir/platform.db` (§2.1) |
| C | Config-store swap scope | DB-first with `.env` write-mirror during transition (§2.4, §7.4) |
| D | Bootstrap `RELAY_API_KEY` | Keep in `.env` for P6; vault-adjacent move deferred to P7 (§3.1 G4) |
| E | Provider-key env vars removal | Keep as keyring-disabled fallback; strip during `relay migrate` only when `RELAY_KEYRING=true`; full removal with P7 (§4.2) |
| F | 28 stale RC tests | **Rewrite to registry wiring** (never delete the production-gate suite) (§4.4) |
| G | `RUNTIME_READY` → `defn.runtime_ready` | Replace (§4.1) |
| H | Dead settings (`default_provider`, `openrouter`/`groq` keys) | Remove `default_provider`; drop `openrouter`/`groq` parsing (defer provider wiring) (§4.3) |
| I | `state.json`/`availability.json` residency | Keep `state.json` as JSON; fold availability into `model_status` (§1.1, §7.1) |
| J | `relay keys provider` alias | Keep and document as convenience (§5.2) |
| K | Expired/revoked key purge | `relay keys prune` command + automatic purge in `relay migrate` (§3.3) |

---

## 10. Gate criteria

1. Reviewer approves this plan (and the §9 decisions).
2. P6.1 mini-plan approved before any code.
3. Every sub-phase ends green; full suite `pytest tests -q` has **0 failures**.
4. `docs/platform-db-schema.md` exists and matches the implemented DDL.
5. `security-best-practices` review of the audit/rotation surface is green.
6. `PROJECT_LOG.md` untouched; no commit beyond the approved phase commits.
