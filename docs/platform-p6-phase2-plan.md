# P6.2 - Phase Plan: Security Maturity

## 0. Objective

Harden the security surfaces built on the P6.1 `platform.db`
consolidation: durable audit logging, exposed key rotation, a terminal-row
purge policy, keyring-aware setup detection, and a CLI/API review — with a
full regression gate. This plan pulls the security-maturity items of the
master plan (§3.2/3.3/3.4 and parts of §4.2) forward into P6.2. No code
changes land with this plan; implementation is a separate, gated pass.

## 1. Current-state recap (facts the plan is built on)

All line references are post-P6.1 (`0f49419`).

### 1.1 Key lifecycle

- `KeyStore` (`app/services/key_store.py`) already implements
  `create`/`get_by_id`/`list`/`revoke`/`delete`/`rotate`/`classify`/
  `verify`/`mark_used`/`memory_counts`. `rotate` (lines 282-299) creates
  the replacement and revokes the original, but **no CLI or API surface
  calls it**.
- Expiration is evaluated lazily at `verify`/`classify` time
  (`key_store.py:335-336, 367-368`). Expired and revoked rows accumulate
  forever; `memory_counts` reports them but nothing removes them. There is
  no "expiring soon" flag anywhere.
- `delete` (hard remove) exists at `key_store.py:247-265` and is exposed
  only via `DELETE /admin/keys/{id}?permanent=true`.
- CLI surface (`app/cli/keys.py`): `list`/`add`/`remove`/`test` (plus the
  `provider` alias). No `rotate`, no `prune`.
- API surface (`app/api/keys.py`): `POST /admin/keys` (create, raw key
  once), `GET /admin/keys`, `GET /admin/keys/{id}`, `DELETE
  /admin/keys/{id}?permanent=`. All gated by the global dependency + the
  `admin` scope when store auth is on.

### 1.2 Audit today

- **No durable audit log.** `ops_store` (`app/services/ops_store.py`)
  records `key_admin` events **in memory only** (lines 101-119) and loses
  them on restart. This is master-plan gap G1.
- Metrics (`app/services/metrics.py`): `record_auth` (auth success/failure
  by reason/method, `auth_by_key`), `record_key_action` (action/outcome).
  In-memory, reset on restart, not an audit trail.
- `log_service` emits JSON request/attempt metadata (no prompts, no
  responses, no keys); correlation ids are ephemeral and logged.
- `client_tracking` is a bounded in-memory apps surface; not an audit log.

### 1.3 Security gaps (verified against source)

| # | Gap | Current state | Disposition |
| --- | --- | --- | --- |
| G1 | No durable audit log | `ops_store.record_key_action` is in-memory (`ops_store.py:101-119`) | `events` table + `EventLog` service (§3.2) |
| G2 | Rotation not exposed | `KeyStore.rotate` exists (`key_store.py:282-299`), no CLI/API caller | `relay keys rotate` + `POST /admin/keys/{id}/rotate` (§2.1) |
| G3 | No terminal-row cleanup | revoked/expired rows accumulate forever | `relay keys prune` + automatic purge in `relay migrate` (§2.2) |
| G4 | Bootstrap `RELAY_API_KEY` plaintext in `.env` | documented; read per request; recovery path | **out of scope** (master Decision D; vault move is P7) |
| G5 | No auth-failure rate limiting | each failed store auth runs an O(n) scrypt scan | document as known limitation + bucketed `events`; proxy note (§3.1) |
| G6 | Keyring-blind setup detection | `_has_usable_provider` reads `settings.<key_attr>` (`app/cli/__init__.py:10-28`); wizard reads `config_store.get_env(defn.key_env)` (`app/setup/wizard.py:201`) — neither sees keyring-only keys | route both through `resolve_provider_key` (§4.1) |
| G7 | Provider keys still honored from `.env` | deprecated env vars are the keyring-disabled fallback | keep for P6.2 (Decision E); removal is P7 |
| G8 | `events` not yet in schema | `platform_store.SCHEMA_VERSION = 4`; `MIGRATIONS` v1-v4 only | additive v5 migration (§5) |

### 1.4 Redaction and permissions (baseline)

- `app/services/redaction.py` masks `sk-…`, `nvapi-…`, `rl_`+43, `bearer`,
  and `authorization`/`x-relay-api-key` header values, by key name and by
  value shape; `redact_dict` deep-scrubs dicts/lists/strings.
- `platform.db` + `-wal`/`-shm` + `.corrupt-*.bak` are `0600` on POSIX
  (`platform_store._secure_permissions`/`_backup_corrupt`); `relay
  migrate` backups are `0600`; `.env` is `0600` after writes
  (`config_store.set_env`). `setup_state`/`availability.json` writes use
  default permissions (no secrets, but inconsistent with the rest).
- `mask_key` (`app/setup/key_validation.py:54-60`) renders `********abcd`.

## 2. Design decisions for P6.2

### D1 - `events` table (schema v5) + `EventLog` service

Additive migration `MIGRATIONS[5]`:

```sql
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    actor    TEXT NOT NULL,          -- "bootstrap" | opaque key_id | "cli" | "system"
    action   TEXT NOT NULL,          -- bounded vocabulary (see D2)
    target   TEXT NOT NULL,          -- opaque id / provider id / path label
    outcome  TEXT NOT NULL,          -- "ok" | "failed" | "denied"
    detail   TEXT NOT NULL           -- JSON; redacted, no secrets
)
```

- New `app/services/event_log.py` (`EventLog`) writes through a
  `PlatformStore` connection to the shared `platform.db`. Metadata only:
  no prompts, responses, raw keys, hash material, proxy credentials, or
  correlation ids. `detail` is passed through `redact_dict` before insert.
- **Write semantics:** best-effort on hot paths (auth) — never raises,
  never blocks the request; failures increment
  `relay_events_failed_total`. Admin-action endpoints (create/rotate/
  revoke/delete/ prune) write synchronously and surface failure as a 500
  so an operator cannot believe an un-recorded action happened. An
  in-process failure counter distinguishes "audit degraded" from
  "authenticated" so monitoring can alert.
- Actor identity: store-backed requests carry the opaque `key_id` from
  `request.scope["relay_key_id"]`; bootstrap-key requests record
  `"bootstrap"`; CLI writes record `"cli"`; migration/purge record
  `"system"`. Never the raw key.
- `EventLog` and `KeyStore`/`StateStore` each keep their own guarded
  connection to the same WAL file (the established multi-connection
  model, already covered by `busy_timeout`/`_migration_lock`).

### D2 - Event vocabulary (bounded, for `detail`/`action` stability)

`action` values: `key.create`, `key.rotate`, `key.revoke`, `key.delete`,
`key.prune`, `provider_key.set`, `provider_key.remove`,
`provider_key.migrate`, `config.reload`, `auth.success`, `auth.failure`,
`store.open`, `store.close`, `migrate.run`. `outcome` ∈
{`ok`, `failed`, `denied`}. `detail` carries bounded metadata only (e.g.
`{"scope_count": 2, "label": "ci"}` — labels are operator text, not
secrets; raw keys are never written). This mirrors the metrics label
discipline so the set stays stable and testable.

### D3 - Rotation exposed (G2)

- `relay keys rotate <key_id>` — resolves full/short id (same rule as
  `keys remove`), prints the new raw key **exactly once**, then revokes
  the original; non-interactive requires `--yes`; `--json` includes the
  new key. Reuses `KeyStore.rotate`.
- `POST /admin/keys/{id}/rotate` (admin scope) — returns the new raw key
  once with a `key.rotate` event; 404 for unknown ids.
- Provider-key rotation already works (`relay provider keys set`); add the
  masked-confirm prompt + `provider_key.set` event (§4.2).
- Update the rotation runbook in `docs/security.md` (create → migrate
  clients → revoke; short overlap is safe because both old and new keys
  are valid during the swap).

### D4 - Prune (G3, master Decision K)

- `relay keys prune [--older-than-days N] [--yes] [--json]` — default
  **dry-run** listing what would be removed; removes terminal rows only:
  `revoked_at IS NOT NULL` OR (`expires_at IS NOT NULL` AND `expires_at <=
  now`). A grace window keeps rows that became terminal recently:
  `--older-than-days` defaults to 30 (no env knob in P6.2; constant
  `_PRUNE_GRACE_DAYS = 30`). Rows still active are never touched.
- Automatic purge hook inside `relay migrate`: after import/verify and
  before the manifest commit, run the same prune with the grace window and
  record a `key.prune` event (no-op when no rows qualify).
- Additive `GET /admin/keys` field `expires_soon` (D6) so operators can
  see at-risk keys; prune is deliberately CLI-only in P6.2 (no
  destructive API surface without a path for operator confirmation).
- `KeyStore.prune(cutoff_ts)` returns `(removed, scanned)` for the
  verification message; it never deletes a row that is still valid.

### D5 - Keyring-aware setup detection (G6)

- `_has_usable_provider` (`app/cli/__init__.py`) and the wizard's
  `_configure_cloud` (`app/setup/wizard.py`) route through
  `resolve_provider_key` (`app/providers/factory.py:33-58`) instead of
  reading `settings.<key_attr>` / `config_store.get_env` directly, so a
  keyring-only install (post `relay provider keys migrate`) is detected as
  configured and setup is **not** re-launched.
- `resolve_provider_key` already implements the exact precedence
  (keyring-first when `RELAY_KEYRING=true`, then `.env` fallback); the two
  call sites become consumers of that single truth.

### D6 - Expiring-soon flag + additive API fields

- `KeyStore.list` metadata gains `expires_soon: bool` (true when
  `expires_at` is within the window, default `_EXPIRY_WINDOW_DAYS = 7`;
  false for no-expiry). Additive: existing `--json`/API consumers are
  unaffected.
- `relay keys list` prints an `exp` marker; `GET /admin/keys` entries
  include `expires_soon`. No existing field changes meaning or order.

### D7 - Audit read surfaces + retention

- `relay events [--action ...] [--outcome ...] [--limit N] [--json]` —
  tail the `events` table, newest first, default limit 50.
- `GET /admin/events?action=&outcome=&limit=` (admin scope) — bounded,
  newest first; total returned; no secrets (rows are redacted at write).
- **Retention:** prune `events` rows older than `PERSISTENCE_RETENTION_DAYS`
  (existing knob, `config.py:764-768`; `0` = disabled) on the same cadence
  as `state_flusher.prune_retention` (flusher tick, `state_flusher.py:84-85`)
  and inside `relay migrate`. Events retention is decoupled from key-row
  retention on purpose (keys need the grace window; events need a max age).

### D8 - Auth events (best-effort, bucketed)

- `require_api_key` (`app/security/auth.py`) emits `auth.success` /
  `auth.failure` rows via `EventLog` alongside the existing
  `relay_metrics.record_auth` calls. The `detail` reason uses the same
  bounded set already in metrics (`missing`, `invalid`, `expired`,
  `revoked`, `forbidden`, `store_unavailable`, `public`, method label), so
  no new unbounded label space appears.
- **Rate limiting is out of scope** (G5): the durable record is a bucketed
  count signal (action + outcome + reason), not per-request volume; the
  deploy-proxy note in `docs/security.md` stands.

## 3. Audit system

- **Durable security events**: `events` table (D1) is the single durable
  audit trail. Emitted on key create/rotate/revoke/delete/prune,
  provider-key set/remove/migrate, config reload, auth success/failure,
  store open/close, and `relay migrate` runs.
- **Admin actions**: every `/admin/keys` mutation and the new rotate/
  prune endpoints write a synchronous event with the acting key id
  (`request.scope["relay_key_id"]`) or `"bootstrap"`.
- **Authentication events**: D8 best-effort writes on every decision.
- **Retention strategy**: D7 — `PERSISTENCE_RETENTION_DAYS` gates event
  age; no automatic unbounded growth; `relay events`/`/admin/events` are
  bounded reads.
- **Privacy tests** (§6): assert no prompt/response/key/correlation-id
  bytes in any `events` row (mirrors the `redaction.py` contract tests).

## 4. CLI / API improvements

### 4.1 `relay keys` surface (D3/D4/D6)

| Command | Change | Backward compatibility |
| --- | --- | --- |
| `list` | add `exp` marker + `expires_soon` in `--json` | additive fields only |
| `add` | unchanged (`--expires-days` already present) | none |
| `remove` | unchanged (revoke) + `key.revoke` event | none |
| `test` | unchanged | none |
| `rotate <key_id>` | **new** (D3) | new subcommand |
| `prune` | **new** (D4) | new subcommand |

### 4.2 Provider-key CLI (`app/cli/provider_keys.py`)

- `set`/`remove` gain a masked-confirm prompt on interactive terminals
  (`Stored key ...` now confirms the masked value before writing) and emit
  `provider_key.set`/`provider_key.remove` events. Non-interactive paths
  require `--yes` for these writes, matching the migrate guard.
- `migrate` emits one `provider_key.migrate` event per provider moved.
- No flag removal; `--force`/`--dry-run`/`--provider`/`--yes` unchanged.

### 4.3 Admin endpoints (`app/api/keys.py`, `app/api/admin.py`)

- New `POST /admin/keys/{id}/rotate` (D3) and `GET /admin/events` (D7),
  both under `/admin` (admin scope enforced by the existing dependency +
  `_SCOPES_BY_PREFIX`). No changes to request/response bodies of existing
  endpoints except the additive `expires_soon` field on `GET /admin/keys`.

### 4.4 Backward-compatibility guarantees

- No existing CLI flag, exit code, or `--json` shape is removed or
  re-ordered; new fields/commands are strictly additive.
- API wire behavior of `chat/openai/feedback/decision/metrics/health/
  diagnostics` is untouched; `GET /admin/keys` only gains a field.
- `relay keys provider` alias and `relay provider keys` canonical home are
  both preserved (master Decision J).

## 5. Scope

### Files to change

| File | Change |
| --- | --- |
| `app/services/platform_store.py` | `MIGRATIONS[5]` (`events`), `SCHEMA_VERSION = 5` |
| `app/services/event_log.py` | **new** `EventLog` (D1) |
| `app/services/key_store.py` | `prune()`, `expires_soon` in metadata |
| `app/cli/keys.py` | `rotate`, `prune`, `exp` marker |
| `app/cli/events.py` | **new** `relay events` (D7) |
| `app/cli/__init__.py` | register `events`; keyring-aware `_has_usable_provider` (G6) |
| `app/cli/provider_keys.py` | masked-confirm + audit events (4.2) |
| `app/api/keys.py` | `POST /admin/keys/{id}/rotate`; `expires_soon` |
| `app/api/admin.py` | `GET /admin/events` (or fold into `app/api/events.py` — new) |
| `app/security/auth.py` | best-effort auth events (D8); **behavior unchanged** |
| `app/setup/wizard.py` | keyring-aware `_configure_cloud` (G6) |
| `app/services/state_flusher.py` | prune `events` on the retention tick (D7) |
| `app/cli/migrate.py` | automatic key purge + `key.prune`/`migrate.run` events (D4) |
| `app/core/relay.py` | store open/close events (best-effort); no init-path change |
| `app/services/setup_state.py` | `0600` on POSIX writes (permission review, §4.4 of master) |
| `docs/security.md`, `docs/deployment.md`, `docs/configuration.md` | audit/rotation/prune runbooks, events retention, precedence updates |
| `.env.example` | document `PERSISTENCE_RETENTION_DAYS` audit role (no new required vars) |
| Tests | see §6 |

### Files untouched

- **Hot path / routing**: `chat_service.py`, `async_chat_service.py`,
  `routing.py`, `scoring.py`, `decision_engine.py`, `candidate_builder.py`,
  `health_checker.py`, `health_refresher.py`, `health_store.py`.
- **Provider runtime contract**: `app/providers/*` (no shim deletion in
  P6.2; that is the master plan's cleanup sub-phase, deferred).
- **API wire behavior**: `app/api/chat.py`, `openai.py`, `feedback.py`,
  `decision.py`, `metrics.py`, `health.py`, `diagnostics.py`.
- **Server/TUI**: `app/core/server.py`, `app/core/terminal.py`,
  `app/main.py`, `app/ui/*` screens (no TUI changes in P6.2).
- **Auth contract**: bootstrap precedence, public allowlist, fail-closed
  semantics in `app/security/auth.py` — behavior unchanged; only additive
  best-effort event emission.
- **Settings bootstrap**: `.env` read at boot and `reload_settings`
  semantics unchanged.
- `PROJECT_LOG.md` — never modified (standing instruction).

### Migration impact

- `SCHEMA_VERSION` 4 → 5, additive `CREATE TABLE IF NOT EXISTS events`
  only. An existing v4 `platform.db` migrates on next open under the
  in-process lock (`platform_store.migrate`). `relay migrate`-created
  databases migrate identically. No backfill, no column changes, no data
  rewrites — existing tables and their contents are byte-identical.
- `relay migrate` remains the canonical upgrade path; a post-P6.2 re-run
  is still a digest-matched no-op that additionally prunes terminal keys
  (D4) and records a `migrate.run` event.

### Rollback strategy

| Layer | Rollback |
| --- | --- |
| Data | `relay migrate --rollback <ts\|last>` restores legacy sources and removes `platform.db` (P6.1 path, unchanged). Code downgrade of a v5 DB is the standard refuse-on-newer-version path (`platform_store.migrate` raises for `user_version > SCHEMA_VERSION`); because sources were never deleted, `--rollback` (or manual backup restore) is the supported way back. |
| Code | existing `docs/rollback-procedure.md`; `RELAY_DATA_DIR`/`PERSISTENCE_PATH` compat overrides keep a downgrade working. |
| Config | no config-store swap in P6.2; `PERSISTENCE_RETENTION_DAYS` is the only new behavior and it is off (`0`) by default. |
| Auth | bootstrap `RELAY_API_KEY` never depends on the DB (recovery path). |

## 6. Testing

### New test files

- `tests/test_event_log.py` — write/read rows; `redact_dict` applied
  before insert (inject raw-key-shaped detail, assert masked); best-effort
  hot-path semantics (store failure raises nothing, bumps
  `relay_events_failed_total`); synchronous admin-path failure surfaces;
  retention prune; bounded reads; privacy assertions (no
  prompt/response/key/correlation bytes).
- `tests/test_key_rotate.py` — `KeyStore.rotate` round-trip: old key
  rejected after rotate (`classify` → `revoked`), new key accepted;
  `relay keys rotate` prints new key once, revokes old, requires `--yes`
  non-interactively; `POST /admin/keys/{id}/rotate` returns the new key
  once, 404 for unknown, admin-scope enforcement, `key.rotate` event.
- `tests/test_key_prune.py` — dry-run default; grace window keeps recent
  terminal rows; active rows never touched; `--older-than-days`;
  `KeyStore.prune` counts; automatic purge inside `relay migrate`; `--json`.
- `tests/test_keyring_setup.py` — G6 regression: keyring-only provider
  (no `.env` key) makes `_has_usable_provider` True and the wizard's
  existing-key check see the keyring value via `resolve_provider_key`.

### Updated test files

- `tests/test_platform_store.py` — `SCHEMA_VERSION == 5`; full table set
  includes `events`; migration-from-scratch history covers v5; v4→v5
  additive upgrade on an existing migrated file.
- `tests/test_migrate.py` — post-migrate purge behavior; `migrate.run`
  event; v4-created database opens cleanly under v5.
- `tests/test_key_store.py` — `expires_soon` window; `prune`; metadata
  additions.
- `tests/test_key_cli.py`, `tests/test_admin_keys.py`, `tests/test_key_auth.py`
  — new subcommands/endpoints, additive fields, events on mutations,
  auth events.
- `tests/test_redaction.py`, `tests/test_security_hardening.py` —
  extend privacy assertions to `events`.
- `tests/test_setup_wizard.py`, `tests/test_provider_factory.py`,
  `tests/test_provider_migrate.py` — keyring-aware detection and
  provider-key audit events.

### Failure tests

- Store outage → auth fails closed (401) AND a best-effort `auth.failure`
  attempt does not raise; `relay_events_failed_total` increments.
- `EventLog` write failure on an admin action → the action endpoint
  returns 500 (audit-not-degraded-silently).
- Prune with a locked/read-only store → clear error, no partial deletes.
- Concurrent flusher retention prune vs auth `mark_used` on the shared
  file → no lock escalation (extend `test_concurrency.py` pattern).

### Migration compatibility

- v4 `platform.db` (from P6.1) → v5 upgrade green; re-open idempotent.
- `relay migrate` on a legacy layout still lands at v5 with all rows.
- Rollback: `relay migrate --rollback last` after a v5 run restores
  sources and removes `platform.db`; post-rollback guard re-engages.

### Full regression gate

- `pytest tests -q` → **0 new failures beyond the 28 pre-existing
  `test_rc_validation.py` failures** (the stale-RC rewrite is the master
  plan's cleanup sub-phase, not P6.2 security maturity).
- Conformance suite (`test_provider_conformance.py`) stays green.
- `security-best-practices` review of the audit/rotation/prune surface is
  green (master gate criteria #5).

## 7. Risks / mitigations

- **Audit write on the auth hot path** could add latency/contention. Write
  is best-effort, non-blocking (own connection, `busy_timeout`, WAL
  readers); failures degrade to a counter, never 401/500.
- **Event table growth** — bounded by `PERSISTENCE_RETENTION_DAYS` prune
  (D7); default off mirrors today's behavior (no new default retention).
- **Downgrade of a v5 DB** refuses on older binaries — mitigated by the
  never-deleted legacy sources + `relay migrate --rollback` (§5 rollback).
- **Keyring-blind detection regression** (G6) — the two call sites are the
  only consumers of the fix; `test_keyring_setup.py` locks both.
- **Raw-key printing surface** grows by one (`keys rotate`). Contract is
  the same as `keys add`/`/admin/keys` POST: printed exactly once, never
  logged, never persisted; redaction tests cover the audit path.

## 8. Verification / exit gate

1. All §6 new/updated tests green; full suite has **no new failures**.
2. `SCHEMA_VERSION == 5`; `events` present in `platform_store.MIGRATIONS`.
3. Rotation round-trip and prune dry-run verified manually via the CLI.
4. G6: a keyring-only install does not re-launch setup.
5. Audit privacy assertions pass (no keys/prompts/responses/correlation ids
   in `events`).
6. `docs/security.md`, `docs/deployment.md`, `docs/configuration.md`
   updated; `PROJECT_LOG.md` untouched.

## 9. Decisions requested

| # | Decision | Recommended |
| --- | --- | --- |
| A | Events retention knob | reuse `PERSISTENCE_RETENTION_DAYS` (`0` = disabled, current default) |
| B | Prune grace window | `--older-than-days` default 30; constant `_PRUNE_GRACE_DAYS = 30` |
| C | Audit write semantics | best-effort hot path (auth) + fail-visible admin path (§D1) |
| D | Rotate semantics | `KeyStore.rotate` as-is: create replacement + revoke original; raw key returned once |
| E | `expires_soon` window | `_EXPIRY_WINDOW_DAYS = 7` |
| F | Admin events endpoint | `GET /admin/events` inside `app/api/admin.py` (no new router module) |
| G | Provider-key masked confirm | require `--yes` non-interactively for `set`/`remove` writes (guard parity with migrate) |
