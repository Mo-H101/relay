# Relay — P7 Configuration Management Plan

Date: 2026-08-06 · Planning only — no code changed.

Status: awaiting approval. Plan document stays uncommitted per workflow.

Scope: P7 per `docs/platform-implementation-roadmap.md` — `relay config
show/validate/reload/diff`, secret masking, TUI config panel, "all config
reachable without editing files" (requirement 4). Explicitly **not** in
scope: v1 release preparation, licensing/public release, `PROJECT_LOG.md`,
unrelated refactors.

---

## 1. Current configuration architecture

### 1.1 Settings and loading flow (`app/core/config.py`)

- `Settings.__init__()` reads **103 fields** directly from `os.environ`
  after a one-time `load_dotenv(env_file)` at import (module singleton
  `settings = Settings()`).
- Env-file resolution (`_resolve_env_file`): `RELAY_ENV_FILE` override →
  `<cwd>/.env` → `<project root>/.env` for source checkouts; `<user data
  dir>/.env` for installed packages. `state_dir` and `platform.db` path are
  derived from the env-file location (`_resolve_state_dir`).
- Validators: `_csv`, `_valid_int`, `_valid_float`, `_valid_url`. Invalid
  values **fail fast** — the first bad value raises `ValueError` and aborts
  `Settings()` construction (startup or reload).
- `reload_settings()` re-reads `.env` with `override=True` and re-runs
  `settings.__init__()` in place (used by the TUI on launch).
- Value parsing lives entirely in `__init__`; there is **no declarative
  spec** — field metadata is implied by code order and comments.

### 1.2 Single writer (`app/services/config_store.py`)

- `set_env` / `unset_env` / `get_env` over python-dotenv (`set_key`,
  `unset_key`, `dotenv_values`). `get_env` is file-first with a process-env
  fallback (what makes restore-on-failure rollback correct).
- `set_provider_config(defn, enabled, api_key, base_url, priority_models)`
  — keyring-aware: with `RELAY_KEYRING=true`, `api_key` writes go to the OS
  keyring (never `.env`); otherwise `.env`. POSIX chmod `0600` after write.
- Documented as the only module allowed to write configuration; wizard, CLI
  provider-key commands, and TUI save all route through it. Keys are never
  printed or logged.

### 1.3 Hot reload engine (`app/services/reload.py`)

- `reload_config(relay, *, dry_run, env, dotenv_path)`: validates a fresh
  `Settings()` (with a temporary `_env_overlay` of the target file), diffs
  against the live singleton, and applies only `_RELOADABLE_FIELDS`
  (**84 fields**) by mutating the settings singleton and Provider objects
  in place, then refreshing routing / health / scorer / decision engine /
  telemetry / quality / state-flusher.
- Rollback: on any apply failure the full mutable surface is restored from
  `_snapshot` and every component is re-refreshed; the report returns
  `error_kind="apply"`.
- Secrets (`_SECRET_FIELDS`) are reported **by field name only**; errors are
  `_redact`-ed to `"Invalid value for <ENV_VAR>"`.
- Covered by `tests/test_reload.py` (751 lines), `tests/test_hardening.py`
  (concurrent reloads), `tests/test_concurrency.py` (reload during live
  requests), and per-provider runtime tests.

### 1.4 HTTP surface (`app/api/admin.py`)

- `POST /admin/reload?dry_run=` delegates to `reload_config` (mapped to
  200 / 400 validation / 500 apply), writes a best-effort `config.reload`
  audit event, never returns secret values. `GET /admin/events` tails the
  audit log. Both protected by the shared auth dependency.

### 1.5 CLI (`app/cli/__init__.py`)

- `argparse`-based; subcommands: `setup`, `tui`, `serve`, `keys`,
  `provider keys`, `migrate`, `events`, `apps`. **There is no `config`
  subcommand.** Provider keys have a full CLI (`list` masks values; `set` /
  `remove` never echo; `migrate` moves `.env` → keyring) with `--yes`
  non-interactive guard parity (Decision G).

### 1.6 TUI Configuration panel (tab 5)

- `app/ui/data.py` `_CONFIG_ROWS` — **23 hardcoded rows**: 15 editable
  (live-reloadable), 8 restart-required read-only, 0 informational. No
  secret field appears by design; keys live on the Providers flow.
- `ServiceFacade.save_config()` flow: write via `config_store` → dry-run
  `reload_config` validate → real apply → restore previous `.env` values on
  any failure. `app/ui/screens/configuration.py` renders it grouped by
  effect (routing / failover / restart / info).

### 1.7 Masking / redaction

- `app/setup/key_validation.py::mask_key` → `********abcd`.
- `app/services/redaction.py` — `redact_text`, `redact_dict`,
  `redact_provider_error`; `SENSITIVE_KEYS` + shape regexes (`sk-`,
  `nvapi-`, `rl_`, bearer, authorization/x-relay-api-key headers).
- `reload._redact` — field-name-only error text.
- `tests/test_redaction.py` locks the contract.

### 1.8 Existing tests and docs

- Tests: `test_config_store.py`, `test_reload.py`, `test_admin_reload.py`,
  `test_reload_settings.py`, `test_ui_configuration.py`, `test_redaction.py`,
  plus provider-runtime reload coverage. Full suite: **1930 passed, 18
  skipped** (one known pre-existing timing flake, baseline-reproduced at
  `d344116`).
- Docs: `docs/configuration.md` (reference), `.env.example` (334 lines),
  `docs/tui-guide.md`, `docs/deployment.md`, `docs/security.md`.

---

## 2. Completed related work from P5/P6

- **Single-writer `.env` persistence** (`config_store`) and `0600` mode
  (P1, hardened in P5).
- **Keyring migration** (`RELAY_KEYRING`, `relay provider keys set/remove/
  migrate`) — provider keys no longer require `.env` plaintext (P5).
- **Hot reload engine** with dry-run, rollback, secret-by-name reporting,
  and `POST /admin/reload` + `config.reload` audit events (P6E).
- **Redaction layer** and `mask_key` (P5/P6).
- **Setup wizard** writes provider config through the single writer; writes
  `model_status` to `platform.db` (P6).
- **Durable surfaces that consume config**: `platform.db` (schema v6),
  request log, apps projection, security event log, persistence flusher.
- **TUI save flow** (write → validate → apply → rollback) already proven in
  `test_ui_configuration.py`; reusable as the template for the CLI write path.

---

## 3. Remaining P7 gaps

| # | Gap | Evidence |
| --- | --- | --- |
| G1 | **No `relay config` command** (`show/validate/reload/diff` don't exist). | CLI subcommand list; roadmap P7. |
| G2 | **"All config reachable without editing files" not met.** 103 settings exist; only 23 appear in the TUI form (15 editable); only provider config is writable via CLI/wizard. Scoring weights, health refresh timing, telemetry, persistence/request-log retention, proxy, keyring flags, base URLs, etc. are file-only. | Field counts; `_CONFIG_ROWS`; `_RELOADABLE_FIELDS`. |
| G3 | **No canonical settings registry.** Field metadata is split across `Settings.__init__` (code), `reload._RELOADABLE_FIELDS` (84), and `ui.data._CONFIG_ROWS` (23) — three hand-maintained lists that can drift. A spec is the precondition for a complete `show`/`validate`/`diff`/full-TUI. | Three enumerations exist independently. |
| G4 | **No standalone `validate`.** Validation only runs implicitly at `Settings()` construction (fail-fast, first error only) or via `POST /admin/reload`. No way to report *all* invalid values in a file before/without editing. | `_valid_*` raise on first bad value. |
| G5 | **No `diff`.** No way to compare the `.env` file against the running process (pending edits) or two env files. | No such command or helper. |
| G6 | **No general non-secret `set/unset` for non-provider settings** via CLI; reload of restart-required fields has no explicit user messaging. | `config_store` writes providers; TUI form excludes restart fields from editing. |
| G7 | **Audit gap:** CLI config mutation has no `config.*` audit events (only `provider_key.*` and the HTTP `config.reload` exist). | `event_log` actions; `provider_keys.py`. |
| G8 | **TUI panel is partial** (read-only restart/info groups) and hardcoded; does not derive from a spec. | `_CONFIG_ROWS`; `configuration.py`. |
| G9 | **Docs:** no CLI runbook for config management; `configuration.md`/`tui-guide.md`/`deployment.md` need the new surface. | Doc coverage check. |

---

## 4. Recommended P7 phase breakdown

Proposed split (each phase independently shippable and testable):

- **P7.1 — Registry + read-only CLI (foundation).**
  New declarative spec module (`app/core/config_spec.py`) that is the single
  source of truth for every setting: env var, attribute, type, default,
  category/group, reloadable, restart-required, secret flag. `Settings.__init__`
  is **not rewritten** — the spec is derived/validated against it (conformance
  test asserts 1:1 coverage of `vars(Settings())` and that the reloadable
  subset equals `reload._RELOADABLE_FIELDS`). Add:
  `relay config show` (masked values, `--json`), `relay config validate`
  (all-errors reporting, `--env-file`), `relay config diff` (file vs process,
  or two files; secrets by name only). Zero mutation risk.
- **P7.2 — Write CLI + reload + audit.**
  `relay config set KEY VALUE`, `relay config unset KEY`, `relay config
  reload`. Writes via `config_store` (single writer); live-applies reloadable
  fields through the *same* `reload_config` engine as the HTTP endpoint;
  restart-required fields warn "restart required" instead of live-applying;
  unknown/secret keys refused (secrets stay on the Providers flow / keyring /
  `relay provider keys`). Emits `config.set` / `config.unset` /
  `config.reload` audit events. Parity tests vs `POST /admin/reload`.
- **P7.3 — Full TUI config panel.**
  Rebuild the Configuration tab from the spec: every non-secret field
  editable, grouped by effect; live fields apply on save; restart-required
  fields write with a restart notice; masked read-only secret rows (values
  never rendered); a validate/diff summary in the status line. Exit: **all
  config reachable without editing files**, proven by a test that walks the
  spec and shows a CLI or TUI edit path for every non-secret field.

Merge option: if the reviewer prefers fewer checkpoints, P7.1+P7.2 can be one
phase (registry + full CLI) and P7.3 a second. The split above is the
conservative default because read-only tooling lands first with no write risk.

---

## 5. Why this should be the next milestone

- **M3 is achieved** (commit `2c091c3`); the remaining roadmap milestones are
  M4 (P1+P2 UX) and M5 (P3+P7+P8). M5 explicitly includes P7
  ("config management") per `docs/platform-recommended-order.md`.
- **Low risk, additive, no hot path.** P7 is "thin commands over existing
  reload" (`recommended-order.md` puts it on the additive track A with the
  lowest-risk rating). The reload engine, single writer, redaction layer, and
  TUI save flow already exist and are heavily tested; P7 layers surfaces over
  them without touching the async hot path, providers, or the database.
- **Closes an open requirement (req 4):** "all config reachable without
  editing files" is currently unmet (G2); P7 is the milestone that closes it.
- **No schema or API changes**, so it cannot destabilize the platform core
  delivered in P6. It strengthens the audit/rollback story (G7) before any
  public release work.
- Not part of v1 release prep or licensing (explicitly deferred by direction).

---

## 6. Files expected to change

**New:**
- `app/core/config_spec.py` — declarative settings spec + per-field
  validation helpers (reusing `_valid_int` / `_valid_float` / `_valid_url` /
  `_csv` semantics per type) + classification (reloadable / restart-required /
  secret / category).
- `app/cli/config.py` — `relay config` subcommands (`show`, `validate`,
  `diff` in P7.1; `set`, `unset`, `reload` in P7.2), reusing `mask_key`,
  `reload._redact`, `config_store`, `reload_config`, `event_log`.
- `tests/test_config_spec.py` — spec ↔ `Settings` parity, metadata sanity,
  reloadable-subset equivalence.
- `tests/test_config_cli.py` — `show`/`validate`/`diff` (masking, exit codes,
  all-errors), then `set`/`unset`/`reload` (single-writer routing,
  restart-required messaging, audit events, HTTP-reload parity).

**Modified (P7.1–P7.3 as scoped above):**
- `app/cli/__init__.py` — register the `config` subparser (additive).
- `app/ui/data.py` — derive `_CONFIG_ROWS`/`config_form()` from the spec
  (P7.3; behavior for existing fields unchanged).
- `app/ui/screens/configuration.py` — full panel from the spec, restart-required
  write flow, masked secret rows (P7.3).
- `tests/test_ui_configuration.py` — extended form coverage, "all reachable"
  proof test.
- `docs/configuration.md`, `docs/tui-guide.md`, `docs/deployment.md`,
  `.env.example` — CLI runbook and panel updates.

**Possibly modified (shared helper, only if it reduces duplication):**
- `app/services/reload.py` — expose a `validate_env_file(path)` helper reused
  by `relay config validate` and the CLI reload path (engine semantics
  unchanged). If this risks touching the tested allowlist, it is skipped and
  validation reuses `_env_overlay` + `Settings()` directly from the CLI module
  instead.

---

## 7. Files that must remain untouched

- `app/core/config.py` — `Settings.__init__` parsing, defaults, validation
  values stay **byte-identical**; only additive hooks are permitted (and are
  not required if the spec lives in its own module).
- `app/services/reload.py` reload allowlist / rollback semantics — unless the
  small `validate_env_file` extraction is approved and covered by tests.
- `app/api/*` public endpoints — no API contract change (`POST /admin/reload`
  unchanged).
- `app/providers/*` runtime and `app/providers/registry.py`.
- `app/services/platform_store.py` and all migrations — **no schema changes**.
- `app/services/config_store.py` write-path guarantees (single writer,
  keyring-aware, no echo).
- `PROJECT_LOG.md` (explicit workflow constraint).
- `docs/security.md`, `docs/rollback-procedure.md`, `docs/platform-db-schema.md`
  unless P7's surface demands a documented extension (not expected).
- Migration manifests and the `relay migrate` rollback contract.

---

## 8. Migration strategy

- **No database schema changes.** P7 is additive CLI/TUI surface over the
  existing `.env` + `platform.db` (v6) + keyring model.
- The spec module is additive; `Settings()` behavior and defaults are proven
  identical by the conformance test, so existing `.env` files, defaults, and
  runtime behavior are unchanged.
- `.env` file format and the single-writer rule are preserved; all new writes
  go through `config_store`.
- The reload allowlist (apply behavior) stays exactly as-is; the spec merely
  *reflects* it (parity is test-enforced), so restart-required fields never
  become live-applied accidentally.
- Users with existing `.env` (including unset/comment-only entries) see no
  change; `show`/`validate`/`diff` read the same sources the runtime reads.

---

## 9. Security strategy

- **Secrets never echoed:** `relay config show` masks secret fields
  (`mask_key`); `--json` also masks; `diff` reports secrets as changed/
  unchanged by name only; `validate` output is `reload._redact`-ed
  (field-name-only). No `--show-secrets` flag; raw secret display is not a
  supported CLI feature (consistent with `relay provider keys list`).
- **Secret writes stay out of `relay config set`:** fields marked secret are
  refused with a pointer to `relay provider keys` / `RELAY_KEYRING` /
  bootstrap `RELAY_API_KEY`. Provider keys continue to route to the keyring
  when enabled.
- **Audit:** every mutation (`set`, `unset`, `reload`) emits a best-effort
  `config.*` event with an opaque actor (`cli` or the requesting key id);
  audit failure never fails the command (matches `provider_keys._emit`).
- **Validation isolation:** `validate`/`diff`/dry-run never mutate `os.environ`
  permanently (`_env_overlay` restore semantics); only the apply path touches
  the live singleton, inside the existing rollback snapshot.
- **Non-interactive guard:** `set`/`unset`/`reload` confirm like `relay
  provider keys` (y/N on TTY, `--yes` required otherwise — Decision G parity).
- No new secret material is introduced; the redaction layer remains the last
  line of defense (`test_redaction.py` stays green).

---

## 10. Testing strategy

- **New `tests/test_config_spec.py`:** every `Settings` attribute ↔ spec
  entry (1:1, no drift); every reloadable field equals
  `reload._RELOADABLE_FIELDS`; every secret field is never editable; every
  field is classified (live / restart / informational); validator reuse.
- **New `tests/test_config_cli.py`:**
  - `show`: all fields listed, secret values masked in text and `--json`,
    defaults shown when unset, exit codes.
  - `validate`: valid file exit 0; one invalid value exit 2 with redacted
    error; **multiple** invalid values all reported; `--env-file` overlay
    correctness; no raw secret in any output.
  - `diff`: file-vs-process and file-vs-file; per-field changed/unchanged/
    missing; secrets by name only.
  - `set`/`unset`: write through `config_store`, live-apply via the real
    `reload_config`, restart-required warns without live-applying, unknown/
    secret keys refused, rollback on apply failure restores `.env`, audit
    event emitted.
  - `reload`: **parity test** — the CLI path calls the identical
    `reload_config` function/args as `POST /admin/reload` for the same input
    and produces identical applied/unchanged output.
- **Update `tests/test_ui_configuration.py`:** extended form contains every
  non-secret spec field; restart-required fields editable with restart
  notice; **"all reachable" proof test** walks the spec and asserts a CLI or
  TUI edit path exists for every non-secret field (exit criterion).
- **Regression:** existing suites must stay green — `test_reload.py`,
  `test_admin_reload.py`, `test_config_store.py`, `test_ui_configuration.py`,
  `test_redaction.py`, `test_hardening.py`, `test_concurrency.py`, provider
  runtime reload tests, and the full suite at the end (current baseline:
  1930 passed, 18 skipped, one pre-existing timing flake).
- No keyring dependency in tests (stub/spy the keyring like existing suites);
  hermetic `env_file`/`state_dir` via monkeypatch.

---

## 11. Rollback strategy

- **Read-only phase (P7.1)** cannot alter state; no rollback surface needed.
- **Write paths (P7.2/P7.3):** every mutation follows the proven
  write → dry-run validate → apply → restore-on-failure flow (TUI
  `save_config` template); `config_store` remains the single writer, so
  restoring prior `.env` values is exact.
- Live-apply failures roll back the in-process snapshot via the existing
  `reload_config` `_snapshot`/`_restore` + component re-refresh.
- **No schema/migration rollback** needed (no schema changes). The `relay
  migrate` rollback contract and platform.db are untouched.
- Audit events give an operator trail for undoing a `config.set`; docs will
  list the exact `relay config unset`/restore command for each edited key.

---

## 12. Acceptance criteria

1. `relay config show` lists **every** configurable setting with effective
   value and classification (live / restart / informational, group/category);
   secrets are masked in text and `--json`; exit 0.
2. `relay config validate` reports **all** invalid values in the target env
   file (not fail-fast) with redacted, field-name-only errors; exit 0 when
   valid, 2 when invalid; never echoes a value.
3. `relay config diff` compares file vs process settings (or two files),
   reporting changed/unchanged/missing per field with secrets by name only.
4. `relay config set KEY VALUE` / `relay config unset KEY` write through the
   single writer, live-apply reloadable fields, warn (and do not apply)
   restart-required fields, refuse unknown/secret keys, roll back on apply
   failure, and emit `config.set`/`config.unset` audit events.
5. `relay config reload` produces output identical to `POST /admin/reload`
   for the same input (parity test green); restart-required fields are
   reported, not applied.
6. The TUI Configuration tab exposes every non-secret setting from the spec,
   edits restart-required fields with a restart notice, and never renders raw
   secrets.
7. **Requirement 4 met:** a test proves every non-secret setting is reachable
   through the CLI or TUI without editing files.
8. Spec↔`Settings` and spec↔reload-allowlist conformance tests green; full
   suite at or above the baseline (1930 passed, 18 skipped) with no new
   failures; no API contract or schema changes; existing `.env` files and
   defaults unchanged.

---

## Files referenced during this audit

- `app/core/config.py`, `app/services/config_store.py`,
  `app/services/reload.py`, `app/services/redaction.py`,
  `app/setup/key_validation.py`, `app/api/admin.py`,
  `app/cli/__init__.py`, `app/cli/provider_keys.py`, `app/ui/data.py`,
  `app/ui/screens/configuration.py`, `app/setup/wizard.py`.
- `tests/test_reload.py`, `test_admin_reload.py`, `test_config_store.py`,
  `test_ui_configuration.py`, `test_redaction.py`.
- `docs/configuration.md`, `docs/platform-implementation-roadmap.md`,
  `docs/platform-recommended-order.md`, `.env.example`.
