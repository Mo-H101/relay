# Platform P5 Phase 5 — Security hardening + final documentation

**Status:** plan (no code, no commit)
**Phase goal:** Close the remaining security surface before the P6 `platform.db` handoff: redaction for the `rl_` key format, permission/plaintext-key fixes, the env→keyring migration command, and final security documentation.

Phase 4 shipped as `05f9f12` (store-backed auth + scopes + `/admin/keys`). This phase is the final P5 step per `docs/platform-p5-phase-plan.md` §5 (lines 326-374).

---

## 0. Current implementation summary

What exists today, confirmed by audit:

| Surface | File | State |
|---|---|---|
| Relay API-key storage | `app/services/key_store.py` | scrypt-hashed `relay_keys.db` (schema v1), `verify`/`classify`/`revoke`/`delete`/`rotate`, WAL, `_backup_corrupt` |
| Provider-key storage | `app/services/provider_key_store.py` | OS keyring, service `"relay"`, per-provider username; `RELAY_KEYRING_BACKEND` override |
| Provider-key resolution | `app/providers/factory.py` `resolve_provider_key` | keyring-first when `relay_keyring_enabled`, else `.env` fallback |
| Single config writer | `app/services/config_store.py` | keyring when enabled, else `.env` `set_key` |
| Relay-key auth | `app/security/auth.py` | bootstrap tier + store tier + scopes (Phase 4) |
| Admin key API | `app/api/keys.py` | create/list/inspect/revoke/permanent-delete |
| CLI (Relay keys) | `app/cli/keys.py` | `relay keys list|add|remove|test` |
| CLI (provider keys) | `app/cli/provider_keys.py` | `relay provider keys list|set|remove` |
| Redaction | `app/services/redaction.py` | masks `sk-`/`nvapi-`/bearer/auth-header shapes; **not** `rl_` |
| Provider error scrubbing | `app/providers/availability.py` `safe_error_body` | strips provider's own key from error bodies |
| Config reload | `app/services/reload.py` | allowlist incl. `relay_api_key`, `relay_auth_store`, provider keys |

Test baseline after Phase 4: **1755 passed, 9 skipped, 28 failed** (all 28 pre-existing `test_rc_validation.py` stale-monkeypatch failures; no new failures).

---

## 1. Security hardening audit

### 1.1 Remaining plaintext key paths

- **Provider keys in `.env`** (`NVIDIA_API_KEY`, `OPENAI_API_KEY`, …). This is the last plaintext bulk surface. `relay provider keys set` only writes them to `.env` when `RELAY_KEYRING` is off; keys placed before Phase 2, or while keyring was disabled, remain in `.env` and are served by the `resolve_provider_key` fallback. → Removed by the Phase 5 migration command (§2), then the `.env` fallback becomes a documented compatibility layer (§4).
- **`RELAY_API_KEY` (bootstrap)** stays plaintext `.env` **by design**: tier-1 bootstrap semantics and the Phase 4 "bootstrap always wins, no store read" contract depend on it being readable at every request. Not migrated. Documented in §3; moving it into a vault is a P6 item (§4).
- **Runtime object in memory**: `provider.api_key` and `settings.relay_api_key` are plaintext strings in-process. Unavoidable (the provider HTTP client needs it); no logging/export path may carry them (verified below).

### 1.2 Env migration strategy

- `relay keys provider migrate` (canonical home: `relay provider keys migrate`, §2.1) moves each cloud provider's key from `.env` into the OS keyring, then removes it from `.env`.
- Runtime already resolves keyring-first (Phase 2), so **no request-path change is needed**; the command only changes where the key lives.
- **Ordering requirement:** `RELAY_KEYRING=true` must be in effect at runtime after migration, otherwise `resolve_provider_key` ignores the keyring and the provider loses its key (the `.env` fallback is gone). The migrate command warns when `relay_keyring_enabled` is false (§2.7); it can run before or after the flip because keyring writes are independent of the flag.
- Post-migration `.env` diff: provider key vars removed; `RELAY_KEYRING=true` (and optionally `RELAY_KEYRING_BACKEND`) added/confirmed. `RELAY_API_KEY` untouched.

### 1.3 Secret exposure risks (findings)

| # | Finding | Fix (this phase) |
|---|---|---|
| F1 | `relay_keys.db` permission enforcement covers only the main file. SQLite WAL sidecars (`-wal`, `-shm`) and `.corrupt-*.bak` copies are not chmod'd; backups are made *before* the `0600` chmod in the corrupt path, so they can inherit the original (e.g. `0644`) mode. | In `key_store._open`: chmod the main file, then chmod existing `-wal`/`-shm` sidecars; in `_backup_corrupt`, chmod the `.bak` after `copy2` (POSIX only; skipped on Windows, matching current behavior). |
| F2 | Auth's `_key_store()` builds `KeyStore()` at the default `state_dir` without creating the directory. On a fresh machine with `RELAY_AUTH_STORE=true`, the first auth request fails closed `store_unavailable` until the dir exists (the CLI `_store()` already does the mkdir). | `auth._key_store()` does `state_dir.mkdir(parents=True, exist_ok=True)` before constructing, mirroring the CLI. Fails closed still applies on genuine DB errors. |
| F3 | `redaction.py` masks `sk-…`/`nvapi-…`/bearer/auth-header shapes but **not** a bare `rl_…` Relay key. A raw Relay key echoed in a provider error body or rendered text would survive `redact_text`/`redact_dict`. `safe_error_body` also only strips the provider's *own* key, not an `rl_` token. | Add `\brl_[A-Za-z0-9]{43}\b` to the `redact_text` pattern set (alongside `sk-`/`nvapi-`); keep key-name masking and the `_AUTH_HEADER` handling unchanged. |
| F4 | `.env` is written by `set_env`/`set_key` without permission enforcement. On POSIX a provider key in `.env` can sit at `0644` (umask). | In `config_store.set_env`, chmod `env_file` to `0600` after the write (POSIX; Windows is user-profile protected by convention). Add a `test_config_store` assertion. |
| F5 | Keyring availability on headless servers: `keyring.get_password` can raise (no backend / no desktop session). `ProviderKeyStore.get` swallows to `""`; `set` raises. Migrate must surface this clearly and abort safely (never partial-env). | Handled in §2.7; documented in `docs/deployment.md`. |
| F6 | `KeyStore.rotate` exists but is not exposed by CLI or `/admin/keys`. Revocation requires create-then-revoke or a manual rotate. | Document as a known limitation; add `POST /admin/keys/{key_id}/rotate` as a P6 candidate (§4). |
| F7 | `relay_keys.db` stores only scrypt hashes (verified: `key_hash`/`key_salt`/`kdf`); raw keys are returned once at create and never persisted. | No fix; re-asserted in §5 secret-scanning tests. |

### 1.4 Logging / redaction review

- `ops_store` events carry metadata only (`route`, `status`, `latency`, opaque `key_id`); prompts/responses/keys never stored (verified in Phase 4).
- Metrics label sets are bounded; `auth_by_key{key_id}` uses the opaque uuid hex, not key material.
- Provider error bodies pass through `safe_error_body` (strips the provider's own key, control chars, truncates). Re-verify with an `rl_` token in the body after the F3 redaction fix.
- Diagnostics exports and setup reporting pass through `redact_dict`/`redact_text` (verified callers in `app/services/diagnostics.py`, `app/setup/reporting.py`). After F3, `rl_` tokens are masked on every export/render path.
- CLI: `keys add` prints the raw Relay key exactly once (intended, Phase 3 contract); `provider keys set/list/remove` never echo; migrate (§2) never prints secrets.

### 1.5 Permissions review

- `relay_keys.db` → `0600` POSIX (exists) + **sidecar/backup fix (F1)**.
- `.env` → `0600` on write (**F4**).
- OS keyring → delegated to the platform backend (Windows Credential Manager / macOS Keychain / libsecret); `RELAY_KEYRING_BACKEND` lets headless servers pick an encrypted backend.
- State dir → created with default perms; document that it should be user-owned.

### 1.6 Key lifecycle review

- Lifecycle: create → active → expired (evaluated at verify time) / revoked (soft) → permanent delete (`delete`). No background sweeper; expired/revoked rows are inert in `verify` and reported accurately by `classify`. Documented behavior, no change.
- `last_used_at` recorded on successful store-backed auth (Phase 4).
- Rotation: `KeyStore.rotate` exists but is unexposed (F6) → documented limitation, P6 API candidate.
- Admin API covers the full operator surface (create/list/inspect/revoke/delete); bootstrap key always has full access (Phase 4 contract).

---

## 2. Migration workflow — `relay keys provider migrate`

### 2.1 Command shape

Canonical form under the existing provider-key CLI (the single writer's home):

```
relay provider keys migrate [--dry-run] [--force] [--provider ID] [--yes]
```

The phase-plan contract names the command `relay keys provider migrate`. To satisfy both the documented name and CLI consistency, `relay keys provider migrate` is registered as an **alias** on the `keys` parser that dispatches to the same handler, so both spellings work. The migration logic lives in `app/cli/provider_keys.py` (next to `set`/`list`/`remove`).

Flags:
- `--dry-run`: print the plan, mutate nothing, exit 0.
- `--force`: overwrite a conflicting keyring entry (see §2.5).
- `--provider ID`: restrict to one provider id.
- `--yes`: confirm non-interactively (required when stdin is not a TTY, matching `keys remove`).

### 2.2 Algorithm

For each selected cloud provider (`defn.kind == "cloud"`, `key_attr` set) with a non-empty `.env` key read via `config_store.get_env(defn.key_env)`:

1. **Classify** (see §2.5): skip / already-migrated / conflict / migrate.
2. **Write phase:** `provider_key_store.set(defn.id, value)` for every key marked *migrate*.
3. **Cleanup phase:** only after **all** writes succeed, `config_store.unset_env(defn.key_env)` for every migrated provider.

Splitting writes and env-removal into two phases is what makes rollback safe (§2.4).

### 2.3 Dry-run

`--dry-run` resolves the same classification and prints one line per provider:
`nvidia   migrate   env→keyring (already stored: no | conflict: no)` — using `mask_key` for any value display. Never prints a raw key, on any path. Exit 0.

### 2.4 Rollback behavior

- **Write failure (keyring unavailable / backend error):** the command aborts before the cleanup phase. No `.env` key is removed; any keys already written to the keyring are harmless (they are the keyring-first source of truth and identical to the env values). Providers keep working via the `.env` fallback.
- **Mid-run failure:** per-provider classification means each provider is independently resolvable afterward; a partial run is not a broken state (keyring entries for migrated providers + env fallback for the rest).
- **Reverting one migrated provider:** re-run `relay provider keys set <id> <value>` to restore the `.env` value, or re-run migrate (idempotent, §2.5) after the keyring entry is removed.
- **Full undo:** delete keyring entries with `relay provider keys remove <id>`, then `relay provider keys set <id>` to write `.env` again. No schema/data migration is involved, so there is nothing to downgrade.

### 2.5 Duplicate / conflict handling

- **Env key empty** → skip (nothing to migrate).
- **Keyring entry equals env value** → report `already migrated`, skip (idempotent re-run is a no-op).
- **Keyring entry differs from env value** → **conflict**: report both values via `mask_key` (`********abcd`), **skip** unless `--force`, which overwrites the keyring entry with the env value (env wins). Without `--force`, the command exits non-zero when any provider conflicts.
- **Keyring entry, no env key** → skip (nothing to migrate; already keyring-resident).
- **Keyless provider / local kind** → skip silently (no key concept).
- Output is a per-provider summary table (status, masked tail only) ending with totals.

### 2.6 Never-print guarantees

- The env value is only ever handed from `config_store.get_env` directly to `provider_key_store.set`; it is never printed, logged, or rendered (not even masked tails of the full value — display uses `mask_key`'s 4-char tail).
- `--dry-run` and the summary table use `mask_key` only.
- The command adds no logging; errors surface as short messages without values (CLI `_fail` convention).

### 2.7 Error handling / keyring failures

- `provider_key_store.set` raising (no backend, backend error) → abort with `_fail` before cleanup; `.env` untouched (§2.4).
- `config_store.get_env` read failure → treat provider as skipped and report; never partial.
- `RELAY_KEYRING` false → print a stderr warning: *"RELAY_KEYRING is not true; set it in .env before relying on migrated keys"*; proceed (writes still land in the keyring).
- Unknown `--provider` → usage error (exit 2) via the provider-registry lookup.

---

## 3. Documentation

| Doc | Change |
|---|---|
| `docs/security.md` (**new**) | Key model & threat summary; precedence table (bootstrap > store; keyring > env) matching the Phase 2 contract; permissions; keyring backends & headless caveats; redaction contract; lifecycle & rotation guidance; incident notes. |
| `docs/configuration.md` | Precedence section (Phase 2 contract, line 179 of the phase plan); new settings `RELAY_AUTH_STORE`, `RELAY_KEYRING`, `RELAY_KEYRING_BACKEND`; **deprecation notes** on provider-key env vars (`NVIDIA_API_KEY`, …): still honored as fallback, no longer written by the tools, to be removed in P6. |
| `docs/deployment.md` | Production profile: `RELAY_API_KEY` bootstrap guidance, `RELAY_AUTH_STORE`, keyring on headless servers (`RELAY_KEYRING_BACKEND`), and the **migration runbook** (`relay provider keys migrate` steps + rollback). |
| `.env.example` | Add `RELAY_KEYRING`, `RELAY_KEYRING_BACKEND`, `RELAY_AUTH_STORE`; deprecation comments on provider-key vars. |
| `README.md` | Client flow: `relay keys add --label "opencode"` → use returned key against the API (P5 exit criterion). |

---

## 4. Cleanup

### Legacy paths to remove
- **None removed in Phase 5.** Every existing path is either load-bearing (compatibility) or removal is gated on P6.

### Deprecated settings (document, keep working)
- Provider-key env vars (`NVIDIA_API_KEY`, …): deprecated after migration, **kept** as the `resolve_provider_key` fallback and for installs that never enable keyring.

### Compatibility layers to keep
- `.env` fallback in `resolve_provider_key` (needed until keyring is on).
- `config_store.set_provider_config` `.env` branch when `relay_keyring_enabled` is off (backwards compat; writers never switch back automatically).
- `config_store.get_env` (source of truth for the migrate command and rollback).
- Bootstrap `RELAY_API_KEY` tier-1 path (Phase 4 contract).
- `ProviderKeyStore.get` swallowing keyring errors to `""` (degradation, not crash).

### What waits until P6 (`platform.db`)
- Fold `relay_keys.db` into `platform.db` (already designed for it: migration convention + schema v1).
- Remove `.env` provider-key writing entirely; hard-deprecate the env vars.
- Move `RELAY_API_KEY` bootstrap into a vault-adjacent store.
- `POST /admin/keys/{key_id}/rotate` (F6) and optional revoked-key sweep.

---

## 5. Testing

### New suites
| File | Covers |
|---|---|
| `tests/test_provider_migrate.py` | Migration: env→keyring, idempotent re-run, dry-run (no mutation, no secrets printed), write-failure aborts before env removal, conflict skip / `--force` overwrite, `--provider` filter, non-interactive `--yes` requirement, never-print assertion (raw key absent from stdout/stderr), rollback (re-set restores). Injected keyring backend via `RELAY_KEYRING_BACKEND` (existing `test_provider_key_store` pattern) + temp `env_file` (existing `test_config_store`/`test_key_cli` pattern). |
| `tests/test_security_hardening.py` | Privacy assertions: `rl_` keys never survive `redact_text`/`redact_dict` (text + dict, quoted/unquoted); `rl_` bearer header masked; `rl_` token in a provider error body masked via `safe_error_body`+redact; `key_id` (uuid) is the only identity in ops events/metrics; `.env` written as `0600` (POSIX, skipped on Windows); `relay_keys.db` main + `-wal`/`-shm` + `.corrupt-*.bak` modes `0600`; secret grep over rendered diagnostics/ops/metrics/log fixtures finds no `rl_`/`sk-`/`nvapi-` material. |

### Regression (must stay green, unchanged assertions)
- `tests/test_auth.py`, `tests/test_key_auth.py` — auth regression (bootstrap byte-identical; store tier; identical 401s).
- `tests/test_admin_keys.py`, `tests/test_admin_reload.py` — admin API regression.
- `tests/test_key_cli.py`, `tests/test_config_store.py`, `tests/test_provider_factory.py`, `tests/test_reload.py` — CLI/config/factory regression.
- `tests/test_redaction.py` — extended with `rl_` shapes (existing assertions unchanged).
- `tests/test_key_store.py` — permission sidecar tests (POSIX).
- `tests/test_metrics.py`, `tests/test_ops_store.py`, `tests/test_diagnostics.py`, `tests/test_setup_reporting.py`, `tests/test_ui_*.py` — rendering/privacy paths.

### Full suite gate
- Full suite: **1755 baseline (9 skipped) + new suites green**, with only the known 28 pre-existing `test_rc_validation.py` failures present — no new failures.
- Provider conformance suite (`tests/test_provider_conformance.py`, 0.63s bound) green.
- Secret scan over the staged diff: no `rl_`/`sk-`/`nvapi-`/`Bearer <real>` values (only fake test constants).
- `security-best-practices` review gate (roadmap line 146) passes before Phase 5 is considered final.

---

## 6. Scope

### 6.1 Files expected to change
- `app/services/redaction.py` — `rl_` value shape (F3).
- `app/services/key_store.py` — sidecar + backup permissions (F1).
- `app/services/config_store.py` — `.env` `0600` on write (F4).
- `app/security/auth.py` — `state_dir` mkdir in `_key_store()` (F2).
- `app/cli/provider_keys.py` — `migrate` subcommand + parser + handler.
- `app/cli/__init__.py` — `relay keys provider migrate` alias dispatch (if approved; §2.1).
- `docs/security.md` (new), `docs/configuration.md`, `docs/deployment.md`, `.env.example`, `README.md`.
- `tests/test_redaction.py`, `tests/test_key_store.py`, `tests/test_config_store.py` (extended) + **new** `tests/test_provider_migrate.py`, `tests/test_security_hardening.py`.

### 6.2 Untouched files
- API wire contracts and all endpoint shapes (`app/api/*` route bodies, responses).
- `tests/test_auth.py` assertions; bootstrap tier-1 semantics.
- Provider runtime, reload, and `config_store` behavior from Phases 2-3 (no request-path change).
- `key_store` schema v1, `ProviderKeyStore`, `factory.resolve_provider_key` precedence, `app/api/keys.py`.
- `PROJECT_LOG.md` (never modified in P5).

### 6.3 Migration impact
- **None on the request path.** The migration changes where provider keys live; runtime already resolves keyring-first. The only operational requirement is `RELAY_KEYRING=true` after migration (warned by the command).
- `relay_keys.db` and auth behavior unchanged; `.env` provider keys removed only when the operator runs the command (or `--force` re-runs).
- Docs-only + hardening otherwise.

### 6.4 Rollback strategy
- Revert the Phase 5 commit: redaction patterns, permission fixes, docs, and CLI additions return to Phase 4 state. No stored-state or behavioral impact on the running system.
- Migration reversibility is user-level (§2.4): re-set or re-run to move keys either direction; there is no schema change to downgrade.
- A conflict is never auto-resolved without `--force`; env-removal is gated on all writes succeeding, so a failed run leaves every provider usable.

---

## Acceptance criteria

1. `rl_` raw keys are masked in every export/render/error path (`redact_text`, `redact_dict`, `safe_error_body`); `tests/test_security_hardening.py` and extended `tests/test_redaction.py` pass.
2. `relay_keys.db` main file, WAL sidecars, and `.corrupt-*.bak` backups are user-only on POSIX; `.env` is user-only after writes.
3. `relay provider keys migrate` (and the `relay keys provider migrate` alias) moves keys env→keyring, never prints a secret, is dry-run safe, aborts before env-removal on write failure, and handles conflicts/already-migrated idempotently.
4. Auth/admin/CLI/config/factory regression suites pass unchanged; full suite has zero new failures over the 1755/9-skip baseline (28 known `test_rc_validation` failures unchanged).
5. Precedence, keyring caveats, deprecation notes, and the deployment migration runbook are documented; `.env.example` carries the new variables.
6. `security-best-practices` review gate green; P5 complete and ready for the P6 `platform.db` handoff.
