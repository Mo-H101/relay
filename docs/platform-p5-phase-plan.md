# P5 — Phase Plan: API-Key Security & Secret Management

Status: **Phase-planning only. No code yet.** Approved P5 design
(`docs/platform-p5-plan.md`) broken into safe incremental phases. Implementation starts
only after this phase plan is approved.

Source: `docs/platform-p5-plan.md` (approved); `docs/platform-implementation-roadmap.md`
§P5 (lines 93-107). Decisions carried over: `RELAY_API_KEY` bootstrap retained
byte-identical; `.env` stays a supported bootstrap/compat path; keyring-stored provider
key wins over `.env` when present; `api_keys` table folded into `platform.db` in P6.

Constraints (user-mandated): **no code in this phase — plan only**; no `PROJECT_LOG.md`;
no code; stop after the phase plan and wait for approval.

Repo conventions applied to every phase: one commit per phase, on approval; each phase
ends with the **full baseline suite green (1620 passed, 10 skipped, 0 failed) plus that
phase's new tests**; no API contract changes to existing endpoints.

## Phase ordering rationale

Each phase is independently shippable and backward-compatible:

- **0** locks the storage decision before any schema work.
- **1** builds storage in isolation — nothing consumes it yet, so it cannot break runtime.
- **2** flips the provider-key source (keyring-first, env fallback) with zero behavior
  change when `RELAY_KEYRING` is off.
- **3** adds the CLI surface on top of the Phase-1 store.
- **4** adds the store-backed auth + admin API — the only phase that touches the request
  path.
- **5** hardens and audits everything; docs land last so they describe final behavior.

Phases 1-3 never touch `auth.py` or any request path; Phase 4 is the single point where
request-time behavior changes, and it is protected by the Phase-1/3 test suites.

---

## Phase 0 — Storage decision (plan-only)

**Goal.** Confirm the app-keys storage decision: a **separate `relay_keys.db`** in P5
rather than extending the existing `relay_state.db` or introducing `platform.db` now.
Document why, and the migration path into P6.

**Decision.** `relay_keys.db` at `state_dir / "relay_keys.db"` (`state_dir` =
`.relay/` in a source checkout, per-user data dir when installed, `config.py:69-80`),
schema v1 owned by a new `KeyStore`, using the same migration pattern as `StateStore`
(`MIGRATIONS` dict + `PRAGMA user_version`, `state_store.py:30-110,618-639`).

**Why this is preferable.**

1. **Zero coupling to the learned-state store.** `relay_state.db` holds learned health /
   telemetry/quality and is explicitly *not* a secret store
   (`state_store.py:1-8`). Mixing key hashes into it would expand its trust surface and
   its flush/lifecycle semantics for no benefit.
2. **P6 can adopt it wholesale.** The `api_keys` table and its migration pattern are
   designed to be folded into `platform.db` unchanged by P6's migrations framework — the
   P6 work is a table move, not a redesign.
3. **Independent failure domain.** A corrupt key store must fail closed (401) without
   dragging learned-state persistence down with it; a separate DB keeps failure domains
   disjoint.
4. **Independent permissions and lifecycle.** Keys get their own file permissions,
   backup/recovery, and (P6) retention semantics.
5. **Safe rollback.** Deleting a standalone DB file is a complete rollback; it cannot
   corrupt or require re-migration of existing state.

**Migration path.**

- P5: `KeyStore` creates/opens `relay_keys.db`, `PRAGMA user_version = 1` (one table,
  `api_keys`). Existing databases untouched.
- P6: `platform.db` migrations framework gains a `user_version`/table slot; the
  `api_keys` DDL and `KeyStore` CRUD move behind the P6 store; `relay_keys.db` is
  migrated/copied once and retired. No user-visible API change; `KeyStore` public surface
  stays stable so callers do not change at the P6 boundary.

**Files changed.** `docs/platform-p5-plan.md` and this document (decision recorded). No
application code.

**Must remain untouched.** Every application module and test file.

**Migration impact.** None.

**Tests required.** None (decision + documentation only). A short schema-contract review
walkthrough at the Phase-1 gate.

**Rollback strategy.** None needed (docs only).

**Acceptance criteria.** The decision above is recorded in both plan docs; Phase 1 starts
with the confirmed `relay_keys.db` + schema-v1 approach.

---

## Phase 1 — Key storage foundation

**Goal.** Stand up the two storage backends in isolation: the scrypt-backed `KeyStore`
(app keys) and the OS-keyring-backed `ProviderKeyStore` (upstream provider keys). Add the
`keyring` dependency. Nothing consumes these yet, so the runtime cannot break.

**Files changed.**

- New: `app/services/key_store.py` — `KeyStore` class:
  - SQLite at `state_dir / "relay_keys.db"`, WAL + busy timeout + single guarded
    connection (mirror `StateStore`), `PRAGMA user_version = 1`.
  - `api_keys` table (columns per `platform-p5-plan.md` §3.1): `id` (uuid4 hex PK),
    `key_hash` (BLOB, unindexed), `key_salt` (BLOB, 16 B), `kdf` (per-row
    `"scrypt|16384|8|1"`), `label`, `scopes` (JSON), `expires_at`, `created_at`,
    `last_used_at`, `revoked_at`.
  - Methods: `create(label, scopes, expires_at=None) -> (id, raw_key)` (raw key
    returned once, never stored), `get_by_id`, `list()`, `revoke(id)`,
    `mark_used(id)`, `rotate(id)` (create + revoke), `verify(token) -> row | None`
    (scrypt + `hmac.compare_digest`, constant-time over active rows), `memory_counts`,
    `close`. Corruption recovery mirrors `_backup_corrupt` (`state_store.py:641-661`).
  - Hashing: `hashlib.scrypt`, `N=2**14, r=8, p=1`, `dklen=32`, fresh salt per key;
    params stored per row so they can be upgraded later.
- New: `app/services/provider_key_store.py` — thin `keyring` wrapper: service name
  `"relay"`, username = provider id (`nvidia`, `openai`, `anthropic`, `gemini`,
  `lmstudio`). `get(provider_id) -> str`, `set(provider_id, value)`, `remove(id)`.
  `RELAY_KEYRING_BACKEND` override respected for tests/headless.
- New: `tests/test_key_store.py`, `tests/test_provider_key_store.py`.
- Modified: `pyproject.toml` (+ `keyring`), `requirements.txt`, `requirements-dev.txt`.

**Must remain untouched.** `app/security/auth.py`, `app/services/reload.py`,
`app/services/config_store.py`, `app/providers/factory.py`, `app/core/config.py`,
`app/cli.py`, `app/main.py`, `app/api/*`, `app/setup/*`, and every existing test file.

**Migration impact.** Creates `relay_keys.db` schema v1 on first use. No change to
`relay_state.db` or any other file. First-creation chmod to 0600 (user-only) on
POSIX.

**Tests required.**

- `KeyStore`: hash/verify round-trip; two keys with different salts verify
  independently; per-row KDF params persisted and honored; wrong token rejected;
  bit-flip tamper of `key_hash` rejected; create returns raw once and it is not in the
  DB; revoke rejects; expiry rejected; `mark_used` updates `last_used_at`; `memory_counts`
  is metadata-only; corrupt-file → backup + recreate; constant-time verification by
  construction (both digests compared via `hmac.compare_digest`).
- `ProviderKeyStore`: fake keyring backend fixture round-trip (`get`/`set`/`remove`),
  absent entry → `""`/None, backend override env honored.

**Rollback strategy.** Revert the Phase-1 commit. Delete `relay_keys.db` (state dir) —
nothing else references it, so this is a complete, side-effect-free rollback. Keyring
entries written during manual testing are inert until Phase 2.

**Acceptance criteria.** Both stores pass their unit suites; no module outside the two
new services imports them; `pyproject`/`requirements*` install cleanly; full baseline
suite still 1620 passed / 10 skipped / 0 failed.

---

## Phase 2 — Runtime provider integration

**Goal.** Make provider keys resolvable keyring-first, env-fallback-second at the two
points keys enter the runtime (`factory.build_runtime_provider`,
`app/services/reload.py`), and route `config_store`'s `api_key` write path to the
keyring when enabled. Define and enforce the secret-precedence contract. With
`RELAY_KEYRING` off (default), behavior is byte-identical to today.

**Files changed.**

- Modified: `app/providers/factory.py` — `build_runtime_provider` resolves
  `ProviderKeyStore.get(defn.id)` before `settings.<key_attr>` (env fallback)
  (`factory.py:41`).
- Modified: `app/services/reload.py` — provider key application on reload re-reads the
  provider key store before falling back to env (`reload.py:239-241`); secrets stay
  reported by field name only (`_SECRET_FIELDS`, `_redact` unchanged in semantics).
- Modified: `app/services/config_store.py` — `set_provider_config(api_key=…)` writes to
  the provider key store when `RELAY_KEYRING` is on, else the existing `.env` path
  (`config_store.py:51-77`); non-key paths (`enabled`, `base_url`, `priority`) untouched.
  Single-writer invariant preserved: config_store remains the only writer.
- Modified: `app/core/config.py` — new non-secret, non-reloadable `Settings` fields
  `relay_keyring_enabled` (`RELAY_KEYRING`, default `false`) and `relay_keyring_backend`
  (`RELAY_KEYRING_BACKEND`), documented in the config comment blocks.
- Modified (tests): `tests/test_provider_factory.py`, `tests/test_reload.py`,
  `tests/test_config_store.py`.

**Must remain untouched.** `app/security/auth.py`, `app/main.py`, `app/api/*`,
`app/setup/*` (wizard keeps calling `config_store` unchanged), `app/cli.py`, the
`.env`-only behavior when `RELAY_KEYRING` is unset, and existing auth/API tests.

**Secret precedence order (contract, documented in `docs/configuration.md` at Phase 5).**

1. Keyring-stored provider key for that provider id (when `RELAY_KEYRING` enabled and an
   entry exists).
2. `.env` / environment value (`settings.<key_attr>`).
3. Empty string (provider runs keyless, or `requires_api_key` gates discovery).

`RELAY_KEYRING` defaults **off** in P5 (opt-in) so no existing user's setup changes;
Phase 5 documents the migration command (`relay keys provider migrate`) that moves keys
from `.env` into the keyring on the user's schedule.

**Migration impact.** None to stored state. Existing `.env` keys keep working unchanged
(precedence rule 2). Enabling `RELAY_KEYRING` only changes where newly written keys go.

**Tests required.**

- Factory: keyring entry wins over env value; no keyring entry → env value; both absent →
  keyless behavior unchanged.
- Reload: provider key change via keyring applied on reload; env fallback when unset;
  reload report still contains field names only, never values; dry-run unchanged.
- Config store: `api_key` written to keyring when enabled; `.env` path byte-identical
  when disabled; `enabled`/`base_url`/`priority` writes unchanged in both modes.

**Rollback strategy.** Revert the Phase-2 commit. Because `RELAY_KEYRING` defaults off,
reverting fully restores `.env`-driven behavior; any keyring entries written during
testing are inert. No data migration involved.

**Acceptance criteria.** With `RELAY_KEYRING` unset, factory/reload/config_store behavior
is byte-identical to before (diff-tested); with it on, keyring wins and writes go to the
keyring; all new and existing tests green; full suite green.

---

## Phase 3 — CLI workflow

**Goal.** Add the `relay keys` subcommand tree on top of the Phase-1 `KeyStore`:
`list`, `add`, `remove`, `test`, plus provider-key management. Enforce safe-output rules.

**Files changed.**

- New: `app/cli/keys.py` — argparse subparsers registered under `keys`:
  - `relay keys list` — id, label, scopes, expires_at, created_at, last_used_at,
    revoked_at (never hash, never raw).
  - `relay keys add --label <name> [--scopes chat,v1] [--expires-days N]` — creates a
    key; prints the label and the **raw key exactly once**; never persists it.
  - `relay keys remove <id>` — revokes (soft delete); prints the masked id + "revoked".
  - `relay keys test <key>` — verifies against the store; prints
    ok / invalid / expired / revoked. Key never echoed on failure.
  - `relay keys provider get|set|remove <provider-id>` — keyring-backed upstream keys
    (set echoes nothing; get shows `********last4` via `mask_key`).
- Modified: `app/cli.py` — register the `keys` subparser in the existing argparse tree
  (`app/cli.py:125-156`); `setup`/`tui`/`serve` untouched.
- New: `tests/test_key_cli.py`.

**Safe output rules.**

- The raw key is printed exactly once, at `add` time.
- `list`/`remove`/`test` never print raw keys or hashes; `test` failure reasons are the
  fixed set ok / invalid / expired / revoked.
- Keyring `get` output uses `mask_key` (`app/setup/key_validation.py:54-60`).
- All output passes through the Phase-5 redaction layer where it formats user input
  (defense in depth; see Phase 5).

**Must remain untouched.** `app/api/*`, `app/security/auth.py`, `app/services/reload.py`,
`app/services/config_store.py`, `app/providers/factory.py`, and the existing
`setup`/`tui`/`serve` command behavior and help text.

**Migration impact.** None.

**Tests required.** CLI invocation via `app.cli.main(argv=[...])` (argparse pattern):
`add` prints raw once and the raw key is not recoverable from `list`; `list` fields
redacted; `remove` revokes and subsequent `test` reports revoked; `test` on valid /
invalid / expired keys; `provider set/get/remove` round-trip with masked `get`; unknown
subcommand exit behavior; `--scopes`/`--expires-days` persisted correctly.

**Rollback strategy.** Revert the Phase-3 commit; the `keys` subparser disappears and CLI
behavior returns to Phase-2 exactly. Keys created during testing remain in
`relay_keys.db` and are inert (or revoked) — the DB is still only consumed by tests at
this point.

**Acceptance criteria.** All five subcommands work end-to-end against the Phase-1 store;
raw keys appear exactly once in the entire CLI surface; full suite green.

---

## Phase 4 — Admin API + store-backed auth

**Goal.** The single request-path change: `require_api_key` gains the store-backed
tier-2 lookup and scope enforcement, and the admin key-management API lands
(`GET/POST /admin/keys`, `DELETE /admin/keys/{id}`). Bootstrap `RELAY_API_KEY` path stays
byte-identical. This is the only phase that alters request-time behavior.

**Files changed.**

- Modified: `app/security/auth.py` —
  - Tier 1: `RELAY_API_KEY` set → current constant-time path unchanged (`auth.py:95-131`),
    key identity `"bootstrap"`, full access.
  - Tier 2: token → iterate active rows, scrypt-verify constant-time, attach
    `request.state.key_id` / `key_label` / `key_scopes`. Store read failure **fails
    closed** (401, reason `store_unavailable`). Revoked/expired → 401 with distinct
    metric reasons (`revoked` / `expired`).
  - Scope enforcement map: `/admin/*` → `admin` scope; `/chat`, `/v1/*`, `/feedback` →
    `chat`/`v1`; empty scopes = full access; `PUBLIC_PATHS` unchanged.
- New: `app/api/keys.py` — `GET /admin/keys` (list, never hash/raw), `POST /admin/keys`
  (create; returns raw key exactly once), `DELETE /admin/keys/{id}` (revoke). Guarded by
  the global `require_api_key` dependency plus the `admin` scope check.
- Modified: `app/main.py` — register the keys router (`main.py:63-71`).
- Modified: `app/services/metrics.py` — `record_auth` gains a `key_id` label
  (`metrics.py:582-597`); bounded cardinality (one per key + `bootstrap`).
- Modified: `app/api/middleware.py` — pass `request.state.key_id` (opaque, non-secret)
  into ops events for per-key correlation; never header values.
- New: `tests/test_key_auth.py`, `tests/test_admin_keys.py`.

**Must remain untouched.** `/v1`, `/chat`, `/health`, `/`, `/admin/reload` response
shapes and status codes; existing `tests/test_auth.py` (bootstrap assertions pass
unchanged); `PUBLIC_PATHS`; docs/redoc/openapi gating; provider/reload/config_store
modules.

**Migration impact.** None to stored state (consumes Phase-1 DB).

**Tests required.**

- Store-backed auth: bearer + `X-Relay-API-Key` against a created key; wrong token →
  401; revoked → 401 `revoked`; expired → 401 `expired`; store-unavailable → 401
  fail-closed; bootstrap key still accepted with full access; `request.state.key_*`
  populated on success.
- Scopes: `admin` scope required for `/admin/keys`; `chat`/`v1` required for
  `/chat` and `/v1/*`; empty-scope key has full access; bootstrap unaffected.
- Admin API: `GET` list shape (no hashes/raw), `POST` returns raw exactly once and it
  verifies, `DELETE` revokes (subsequent auth → 401), 401 without credentials, 403 for
  non-admin scopes.
- Metrics: `key_id` label present on auth success; no key material anywhere in metric
  labels.
- Regression: entire `tests/test_auth.py` passes unchanged; `/admin/reload` behavior
  unchanged; full suite green.

**Rollback strategy.** Revert the Phase-4 commit. `require_api_key` returns to the
pure-bootstrap dependency (`auth.py` pre-Phase-4 state); admin keys router unmounts;
created keys remain in `relay_keys.db` inert. Because Phase 1-3 left `auth.py` untouched,
this revert is a clean single-commit rollback of the only request-path change.

**Acceptance criteria.** `relay keys add --label "opencode"` → returned key authenticates
end-to-end against `/v1` (roadmap P5 exit criterion) and is listed/revoked via
`/admin/keys`; bootstrap path and all existing auth tests unchanged; full suite green.

---

## Phase 5 — Security hardening + docs

**Goal.** Close the security surface: redaction for the new key format, logging/
permissions audit, privacy tests, and final documentation of precedence, keyring
caveats, and the `.env` migration command.

**Files changed.**

- Modified: `app/services/redaction.py` — add the `rl_…` value shape to the pattern set
  (`redaction.py:42-48`) alongside `sk-`/`nvapi-`; keep key-name masking unchanged.
- Modified: `tests/test_redaction.py` — new shape tests (text + dict, quoted/unquoted).
- Modified: `app/services/key_store.py` — ensure `relay_keys.db` created with user-only
  permissions (0600 on POSIX); confirm `_backup_corrupt` copies do not inherit lax perms.
- Audit (may touch, only if findings): `app/services/log_service.py`,
  `app/services/ops_store.py`, `app/api/middleware.py`, `app/setup/reporting.py` — verify
  no key material is ever logged/exported; `_safe_provider_body`/`safe_error_body` already
  strip keys from provider error bodies (re-verify with `rl_` keys).
- Docs: `docs/configuration.md` (precedence order, `RELAY_KEYRING`,
  `RELAY_KEYRING_BACKEND`, headless caveats), `docs/deployment.md` (production profile +
  `relay keys provider migrate`), `.env.example` (new variables), README (client flow:
  `relay keys add --label "opencode"`).
- New: `tests/test_security_hardening.py` — privacy assertions.

**Must remain untouched.** API wire contracts, existing endpoint shapes, bootstrap auth
semantics, `tests/test_auth.py` assertions, provider/reload/config_store behavior from
Phases 2-3.

**Migration impact.** None (docs + hardening only).

**Tests required.**

- Redaction: `rl_…` tokens never survive `redact_text`/`redact_dict`; authorization
  header values with `rl_` tokens masked; innocuous text untouched (existing tests stay).
- Permissions: freshly created `relay_keys.db` is user-only (POSIX check; skipped on
  Windows).
- Privacy grep: no `rl_`/`sk-`/`nvapi-` key material in rendered diagnostics exports,
  ops events, metric label sets, or log fixtures; `key_id` (opaque uuid) is the only
  identity recorded.
- Docs review: precedence table matches the Phase-2 contract.
- Full regression: baseline 1620 + all new suites green; conformance suite
  (`test_provider_conformance.py`, 0.63s bound) green; `security-best-practices` review
  gate (roadmap line 146) passes before Phase-4 code is considered final.

**Rollback strategy.** Revert the Phase-5 commit; redaction patterns and docs return to
Phase-4 state. No behavioral or stored-state impact.

**Acceptance criteria.** `rl_` keys are masked in every export/render path; permission
and privacy tests pass; precedence and keyring caveats documented; `security-best-practices`
gate green; full suite green; P5 complete and ready for the P6 `platform.db` handoff.

---

## Phase summary

| Phase | Deliverable | New files | New tests | Request-path impact |
|---|---|---|---|---|
| 0 | Storage decision (docs) | — | — | none |
| 1 | `KeyStore` + `ProviderKeyStore` + `keyring` dep | `app/services/key_store.py`, `app/services/provider_key_store.py` | `test_key_store.py`, `test_provider_key_store.py` | none |
| 2 | Provider-key runtime resolution + config_store key path | — | factory/reload/config_store tests | none |
| 3 | `relay keys` CLI | `app/cli/keys.py` | `test_key_cli.py` | none |
| 4 | Store-backed auth + scopes + admin API | `app/api/keys.py` | `test_key_auth.py`, `test_admin_keys.py` | yes (auth dependency) |
| 5 | Hardening audit + docs | `test_security_hardening.py` | redaction/permissions/privacy tests | none |

**Cross-cutting gates (every phase):** full baseline suite (1620 passed, 10 skipped,
0 failed) plus the phase's new tests green; one commit per phase on approval; no
`PROJECT_LOG.md` changes; no API contract changes; `security-best-practices` review gate
run before Phase 4 is finalized.
