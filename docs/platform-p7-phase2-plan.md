# P7.2 — Controlled Configuration Mutation

Status: **Draft — awaiting approval. No code written.**
Depends on: P7.1 (committed `c5c1e76201a7dbb542725799c01e7322c8af3e4d`).

## 1. Current state after P7.1

- `app/core/config_spec.py` is the single source of truth: 103 `SettingSpec`s
  (env/attr/type/default/secret/effect/validation bounds/TUI+CLI metadata),
  `SPEC_BY_ENV`/`SPEC_BY_ATTR` lookups, and derived tuples
  (`reloadable_fields`, `reload_secret_fields`, `simple_reloadable_fields`,
  `secret_fields`, `tui_fields`).
- `app/core/config_spec.py` exposes `validate_value(spec, raw)`,
  `parse_value(spec, raw)`, `render_value(spec, value)` that reuse the exact
  `Settings` validators and bounds.
- `app/services/config_store.py` is the sanctioned **single writer** for
  `.env`: `set_env`/`unset_env` (python-dotenv `set_key(quote_mode="always")`,
  `unset_key`; POSIX chmod `0600` after write) and
  `set_provider_config` (keyring-aware `api_key` path).
- `app/services/reload.py` consumes the registry (`_RELOADABLE_FIELDS` etc.)
  and hot-applies through `reload_config(relay, dry_run=..., dotenv_path=...)`
  with snapshot/rollback; secrets reported by field name only.
- `app/core/config.py` exposes `reload_settings()`: `load_dotenv(env_file,
  override=True)` + in-place `settings.__init__()`. Cheap, **no network, no
  Relay import** — this is the CLI-safe apply path.
- `app/core/relay.py` builds `relay = Relay()` at import time and performs
  network I/O (provider model discovery). The CLI must **not** import it.
- TUI `ServiceFacade.save_config` already implements write → dry-run reload →
  real reload → restore-on-failure (the pattern P7.2 generalizes to the CLI).
- Read-only CLI exists (`relay config show/validate/diff`), fully masked.
- Audit log (`app/services/event_log.py`) has a bounded `EVENT_ACTIONS`
  vocabulary that already includes `config.reload`.

## 2. P7.2 objectives

Add safe, validated configuration writes through the CLI:

- `relay config set <ENV> <VALUE>` — write one setting.
- `relay config unset <ENV>` — remove one setting (restore default).
- `relay config reload` — re-read `.env` into the in-process singleton and
  report applied/unchanged (settings-level; full provider-side-effect reload
  stays with the TUI and `POST /admin/reload`).

Guiding rules:

- Every write validates **before** persistence using the registry.
- Every write goes through `config_store.py` (never raw dotenv/`Path` writes
  in the CLI).
- Secrets are never echoed; input via positional/`-` stdin/getpass prompt
  (reuse the `provider_keys` pattern); provider keys route through the
  keyring when `RELAY_KEYRING` is on.
- `reload_settings()` (not `reload_config(relay)`) is the CLI apply path so
  `relay config` never triggers network I/O or a Relay build.
- Writes are confirmed (`--yes` for non-interactive runs) and audited.

## 3. Proposed architecture

### 3.1 Write API (config_store.py)

`set_env`/`unset_env` remain the single-key public API. Add one atomic
multi-key primitive underneath:

- `_load() -> dict[str, str]` — parse the active file via `dotenv_values`.
- `_atomic_write(entries: dict[str, str], removals: set[str])` — build the
  new file content in memory, write to a sibling temp file (`<name>.relay-tmp`),
  `fsync`, `os.replace` over the target, then chmod `0600` on POSIX.
  No value is ever echoed or logged.
- A module-level `threading.Lock` + best-effort `fcntl`/`msvcrt` OS lock on
  a `.lock` sibling guards two concurrent CLI processes. On Windows the
  `msvcrt.locking` path is used when available; lock acquisition is
  best-effort (a contended lock retries briefly, then proceeds — the
  read-modify-write inside the lock makes the file consistent).

`set_env(key, value)` and `unset_env(key)` become thin wrappers over
`_atomic_write` (single key), preserving their existing signatures so the
wizard, TUI, `provider_keys`, and migrate keep working unchanged. No new
public multi-key API unless the implementation needs it internally for
`set` + restore. This preserves byte-identical behavior for existing
callers (same quote mode, same file layout).

### 3.2 Mutation service (new: `app/services/config_mutation.py`)

A small service that owns the orchestration and is unit-testable without a
CLI run. `set_env`/`unset_env`/`reload_settings` come from existing modules;
this module adds:

- `set_setting(env: str, raw: str, *, reload: bool, dry_run: bool) -> dict`
- `unset_setting(env: str, *, reload: bool, dry_run: bool) -> dict`
- `reload_settings_report() -> dict` (applied/unchanged/error)

Flow for `set`:

1. Resolve `spec = SPEC_BY_ENV[env]`; unknown env → error (exit 2), never
   writes.
2. `validate_value(spec, raw)`; on `ValueError` → redacted error via
   `reload._redact` (field name only), exit 2, never writes.
3. For a **provider key env var** (spec is `provider` and ends `_api_key`),
   map to the provider definition and call
   `config_store.set_provider_config(defn, api_key=value)` so the keyring
   boundary is honored. For **every other field** call
   `config_store.set_env(env, value)`.
4. If `reload` and the field is live-reloadable: call `reload_settings()`
   (validation of the whole file happens inside `Settings.__init__`). On
   `ValueError` restore the original value(s) via `config_store` and report
   a failed reload with the file restored.
5. If the field is restart-required: report `effect=restart`, no in-process
   apply, note "restart required".
6. Return a report: `{saved, env, effect, reloaded, applied, unchanged,
   restored}` — no values, ever.

Flow for `unset` mirrors `set` with `remove` semantics (restore = re-set the
original value when reload fails).

Flow for `reload`: read the file, run `reload_settings()`, build the
applied/unchanged lists by comparing the previous singleton attribute values
against the new ones (same diff logic `reload.py` uses, restricted to
`reloadable_fields()`), report field names only. Never imports
`app.core.relay` and never performs provider model discovery.

### 3.3 CLI (`app/cli/config.py`)

Extend the existing `config` parser with three subcommands:

```
relay config set <ENV> <VALUE>
relay config unset <ENV>
relay config reload
```

Flags shared with the existing write commands:

- `--yes` — required for non-interactive runs (reuse `_confirm_write`
  parity; `set` with a secret prompts via getpass when interactive).
- `--dry-run` — `set`/`unset`: validate + show what would change, write
  nothing. `reload`: report without mutating.
- `--no-reload` — persist only, skip the in-process apply (default is to
  apply for live fields). `reload` semantics are identical either way in a
  fresh process; the flag is for scripted flows.
- `--json` — machine-readable report (values still never included).

Secret input resolution reuses the `provider_keys` pattern: positional
value, `-` for stdin, hidden `getpass` prompt on a TTY. A set value for a
secret field is confirmed masked (`mask_key`), never raw.

Restart-required and informational handling: `set`/`unset` on a
restart-required field writes the file, reports `effect=restart` and does
not apply in-process. `set`/`unset` on an informational (env-less `relay_name`)
field is refused (exit 2, "cannot be set"). `set`/`unset` on a non-CLI
visible field is refused.

### 3.4 Audit events

Add `config.set`, `config.unset` to `EVENT_ACTIONS` in `event_log.py`
(pure vocabulary constant — no schema change; `config.reload` already
exists). Emit best-effort `config.set` / `config.unset` rows with
`actor="cli"`, `target=env`, `outcome="ok"|"failed"`, `detail` = counts only
(`{"reloaded": bool, "restored": bool}`). Never include values or names of
secret fields' values. CLI `_emit` helper (reuse the `provider_keys` pattern).

## 4. Files expected to change

| File | Change |
|---|---|
| `app/services/config_store.py` | Add atomic `_atomic_write` + lock; wrap `set_env`/`unset_env`. |
| `app/services/config_mutation.py` | **New**: orchestration service (`set_setting`/`unset_setting`/`reload_settings_report`). |
| `app/cli/config.py` | Add `set`/`unset`/`reload` subcommands + dispatch. |
| `app/services/event_log.py` | Add `config.set`, `config.unset` to `EVENT_ACTIONS`. |
| `tests/test_config_mutation.py` | **New**: service-level tests. |
| `tests/test_config_cli.py` | Add `set`/`unset`/`reload` CLI tests (masking, exit codes, rollback). |
| `tests/test_config_store.py` | Add atomic-write/lock/concurrency tests. |
| `tests/test_config_spec.py` | Possibly add a "every live field is settable" coverage test. |

## 5. Files that must remain untouched

- `PROJECT_LOG.md` — no edits, ever.
- `app/core/config.py` — `Settings.__init__` stays byte-identical (P7.1
  invariant). `reload_settings()` is *used*, not modified.
- `app/core/config_spec.py` — no spec/metadata changes in P7.2.
- `app/services/reload.py` — the reload engine is consumed, not modified.
- `app/api/admin.py`, `app/api/*` — no API contract changes in P7.2.
- `app/core/relay.py` — never imported by the CLI paths.
- `app/ui/*`, `app/providers/*`, persistence/schema code — untouched.
- `docs/platform-p7-phase2-plan.md` (this file) — remains uncommitted.

## 6. Security risks and mitigations

| Risk | Mitigation |
|---|---|
| Raw secret echoed by `set` | Reuse `provider_keys` input handling (getpass/`-`); confirm only `mask_key`; report dicts never contain values; audit detail never contains values. |
| Provider key written to `.env` when keyring is on | Provider-key env vars route through `set_provider_config` so `RELAY_KEYRING` semantics are preserved (keyring-first, no plaintext fallback). |
| Invalid value persisted before validation | `validate_value` before any write; whole-file re-validation on reload; `Settings.__init__` is the final gate. |
| Concurrent CLI writes corrupting `.env` | In-memory read-modify-write + temp-file + `os.replace` (atomic on the same filesystem) + best-effort OS lock + thread lock. |
| `set`/`unset` on unknown / non-CLI / informational keys | Registry lookup rejects unknown (exit 2); non-CLI-visible and `relay_name` refused. |
| `.env` permissions widened | `_atomic_write` chmods `0600` on POSIX after replace (matching current `set_env`). |
| Reload failure leaves file + process inconsistent | Snapshot original value, restore via `config_store` on failure (same as TUI `save_config`); report `restored=true`. |
| Audit log leak | New events carry counts only; `redact_dict` runs at insert anyway. |
| CLI importing `app.core.relay` → network side effects | `reload_settings()` only; relay import is prohibited in the mutation path. |

## 7. Migration impact

- No `platform.db` schema change; no event-table change (`EVENT_ACTIONS` is
  an in-code vocabulary set).
- `.env` layout: `_atomic_write` reproduces python-dotenv's
  `quote_mode="always"` formatting so existing files round-trip identically;
  comments and unrelated keys are preserved (in-memory merge, not a rewrite
  of the whole file).
- Existing callers of `set_env`/`unset_env`/`set_provider_config` (wizard,
  TUI `save_config`, `provider_keys`, `migrate`) keep working unchanged;
  their behavior is covered by the existing test suite.
- A `.relay-tmp` file may briefly appear next to `.env` during a write; it is
  replaced atomically and never left behind on the success path (tempfile
  cleanup on failure).

## 8. Test plan

Service tests (`tests/test_config_mutation.py`), hermetic — no real Relay,
no network, temp `.env` via `RELAY_ENV_FILE`/monkeypatch:

- `set` persists + validates (int/bool/csv/url/secret), rejects invalid
  values (exit path, redacted error, file untouched).
- `set` on unknown env / informational / non-CLI-visible → refused.
- `set` provider key routes through `set_provider_config` when
  `relay_keyring_enabled` is true (keyring backend stub).
- `unset` removes the key and restores the default on reload.
- Reload-failure rollback: write invalid combination → `reload_settings`
  raises → original value restored and `restored=true`.
- `reload_settings_report` reports applied/unchanged field names only and
  never triggers `app.core.relay` import (monkeypatch a sentinel).

CLI tests (`tests/test_config_cli.py`, via `main(argv)` + `capsys`):

- `config set` valid → exit 0, "saved", no raw value in output.
- `config set` secret → value never in stdout/stderr; masked confirm.
- `config set` invalid → exit 2, redacted field-name error.
- `config set` unknown → exit 2.
- `config set` non-interactive without `--yes` → exit 1 refused.
- `config unset` → key removed; idempotent when absent.
- `config reload` → exit 0, applied/unchanged lists; `--dry-run` mutates
  nothing.
- Secret input via stdin `-`.
- Audit row emitted on success and failure (`isolated_event_log` fixture).

Store tests (`tests/test_config_store.py`):

- Atomic write preserves unrelated keys/comments; quote-mode parity with the
  old `set_key`; chmod `0600` on POSIX (skip on Windows).
- Concurrent writers (threads) produce a consistent file (no lost updates
  across two keys).

Regression: existing suite must stay green (the P7.1 golden suite already
locks the registry and reload allowlist).

## 9. Rollback plan

- Code rollback: P7.2 is additive. If it misbehaves, revert the P7.2 commit;
  P7.1 behavior is unaffected (config_store keeps its public API).
- Runtime rollback: `relay config unset` re-applies the default; `set`
  restored the previous value automatically on any reload failure.
- File safety: the previous `.env` is never rewritten destructively —
  `_atomic_write` merges in memory; the pre-change value of a touched key is
  captured in the `originals` snapshot (as `save_config` does) and restored
  on failure. No `.env` backup file is created (the merge is the safety net),
  matching the current single-writer contract.
- No DB migration exists, so there is no schema rollback.

## 10. Acceptance criteria

1. `relay config set` validates before writing, persists via `config_store`,
   reports effect, never echoes secret values, and exits 0/1/2 correctly.
2. `relay config unset` removes a key, restores the default, and is
   idempotent.
3. `relay config reload` re-reads `.env` in-process without importing
   `app.core.relay` or doing network I/O, and reports field names only.
4. A failed reload restores the previous `.env` value and reports
   `restored=true`.
5. Provider-key writes honor `RELAY_KEYRING` (keyring, no plaintext .env).
6. `config.set` / `config.unset` audit events exist and carry no values.
7. `.env` survives concurrent writes intact; atomic replace + `0600` on POSIX.
8. `Settings.__init__`, `config_spec`, `reload.py`, `admin.py`, and all UI
   code are byte-identical (untouched); `PROJECT_LOG.md` untouched.
9. Full suite green: new tests + existing 1982 passed / 18 skipped baseline.
10. Plan document remains uncommitted; commit happens only after approval.
