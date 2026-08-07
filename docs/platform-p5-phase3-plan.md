# P5 — Phase 3 Plan: CLI Key Management Workflow

Status: **Phase-planning only. No code yet.** Approved design
(`docs/platform-p5-plan.md`), approved phase plan (`docs/platform-p5-phase-plan.md`
§Phase 3, lines 210-262), and completed foundations: Phase 1 KeyStore +
ProviderKeyStore (`23ee1fe`) and Phase 2 keyring-first resolution (`329d2cb`).
This document is the concrete Phase 3 implementation plan. No code in this
phase; no `PROJECT_LOG.md` changes; stop after this document and wait for
approval.

## CLI architecture audit (current state)

- `app/cli.py` is a single ~160-line argparse module. Entry point
  `relay = "app.cli:main"` (`pyproject.toml:23-24`); the console script resolves
  `main` from the module.
- Subcommands today: `setup`, `tui`, `serve` only (`app/cli.py:126-140`),
  dispatched by a flat `if args.command == ...` chain (`app/cli.py:144-156`).
  `--version` via argparse `action="version"`.
- Helpers: `_has_usable_provider()` (registry-driven; local providers always
  count, cloud only with a non-empty key), `_config_configured()` (setup-state
  marker + usable provider), `_cmd_tui()` (guarded by
  `terminal.tui_ready()`/`print_tui_guidance`, `app/core/terminal.py`),
  `_cmd_setup()`, `_cmd_serve()`.
- No provider/key CLI surface exists today; the setup wizard
  (`app/setup/*`) is the only key-entry path, writing through the single writer
  `config_store` and masking display via `mask_key`
  (`app/setup/key_validation.py:54-60`).
- Existing CLI tests invoke `app.cli.main([...])` and monkeypatch `_cmd_setup`/
  `_cmd_serve` (`tests/test_packaging.py:194-299`); `tests/test_ui_boundary.py:63`
  asserts `import app.cli` stays Textual-free; `tests/test_ui_terminal.py`,
  `tests/test_setup_wizard.py` also import `app.cli`.
- Packaging: `[tool.setuptools.packages.find] include = ["app*"]`
  (`pyproject.toml:29-30`) auto-discovers subpackages — a `app/cli/` package is
  picked up with no config change.

## Command design

### Module layout (decision)

`app/cli.py` (module) → `app/cli/` (package) so the phase plan's `keys.py`
module can exist. `app/cli/__init__.py` keeps `main`, `_cmd_setup`, `_cmd_tui`,
`_cmd_serve`, `_has_usable_provider`, `_config_configured` (entry point
`app.cli:main` and `import app.cli as cli` both keep working; existing tests
unchanged). New modules:

- `app/cli/keys.py` — `relay keys` subcommands (app-key management on
  `KeyStore`).
- `app/cli/provider_keys.py` — `relay provider keys` subcommands (provider-key
  management via `config_store` + `ProviderKeyStore`).

### Syntax

```
relay keys list [--json]
relay keys add --label <name> [--scopes chat,v1] [--expires-days N] [--json]
relay keys remove <key-id> [--yes]
relay keys test <key>|-

relay provider keys list [--json]
relay provider keys set <provider-id> <key>|-
relay provider keys remove <provider-id>
```

- `relay keys add`: label required; `--scopes` comma-separated (stored as JSON,
  default `[]`); `--expires-days N` (positive int) → `expires_at = now + N
  days`, default no expiry. Prints the **raw key exactly once** plus metadata.
- `relay keys remove <key-id>`: soft-delete (revoke). Requires confirmation
  (below).
- `relay keys test`: verifies a key against the store; prints one of
  `ok <label>` / `invalid` / `expired` / `revoked`. The key is never echoed on
  failure.
- `relay provider keys set <provider-id> <key>|-`: stores a provider key through
  `config_store` (single-writer invariant preserved — see Integration). `<key>`
  positional or `-` for stdin; never echoed.
- `relay provider keys remove <provider-id>`: clears the provider key (keyring
  entry removed / `.env` cleared via `config_store`), idempotent.

### Interactive vs non-interactive

| Command | Interactive (TTY) | Non-interactive |
|---|---|---|
| `keys add` | same output; label required in both | label required; prints raw once |
| `keys remove` | confirm `Revoke key <short-id>? [y/N]` | requires `--yes` (refuses otherwise) |
| `keys test` | `<key>` positional or getpass prompt | `<key>` positional or `-` stdin |
| `provider keys set` | getpass prompt if `<key>` omitted | `<key>` positional or `-` stdin |
| `provider keys list/remove` | same | same |

- TTY detection via `sys.stdin.isatty()` (consistent with
  `app/core/terminal.py`). No command ever blocks waiting for input in a
  non-TTY: missing required values → argparse error (exit 2) or refusal
  (exit 1).
- `getpass.getpass()` is used for hidden key entry on TTY only (no echo, not in
  shell history).

### Stdin handling

- `-` reads the key from stdin: read the first non-empty line, strip
  surrounding whitespace; empty input → usage error (exit 2). This keeps keys
  out of process args and shell history.

### Confirmation requirements

- `keys remove`: interactive y/N prompt on TTY; non-interactive requires
  `--yes`; anything else → refusal (exit 1, no revocation). Prevents accidental
  revocation in scripts.
- No other command requires confirmation.

### Output format

- Human-readable plain text by default; `--json` for machine use (both `keys
  list` and `provider keys list`).
- `keys list`: one line per key — `id  label  scopes  expires_at  created_at
  last_used_at  revoked_at`; id shown shortened (first 8 hex chars) plus full id
  under `--json`; times ISO 8601 or `-`.
- `keys add`: `Key ID: <full id>`, label, scopes, expiry, then a `---`-fenced
  `API Key: rl_...` block and the notice "Shown once — store it now."
- `keys remove`: `Revoked <short-id>`.
- `keys test`: `ok <label>` / `invalid` / `expired` / `revoked`.
- `provider keys list`: one line per cloud provider with a key attr — `id
  requires_key  has_key  key(masked)`; masked via `mask_key`; `-` when absent.
- `provider keys set`: `Stored key for <provider-id>` (never the value).
  `provider keys remove`: `Removed key for <provider-id>`.
- All output goes through plain `print` (no key material), consistent with the
  existing CLI.

## Security requirements

- **Raw key displayed exactly once:** only `keys add` prints the raw key (and
  its sole print site is the result of `KeyStore.create`). `list`, `remove`,
  `test`, and every provider-key command never print raw material.
- **Masking:** provider keys are always masked with `mask_key`
  (`********last4`; short keys fully masked). App-key ids are opaque uuids (not
  secret) and are shown as-is.
- **No secrets in shell history / process list where possible:** keys are
  *entered* only via positional arg (documented risk), `-` stdin, or a hidden
  getpass prompt; `keys add` *generates* its key so no key ever appears in the
  command line. CLI help text documents the `-`/prompt pattern.
- **No secrets in logs or errors:** the CLI adds no logging; exceptions from
  `KeyStore`/`ProviderKeyStore`/`config_store` are caught and re-raised as
  short messages without values. `keys test` never prints the tested key, on
  success or failure. `--json` output contains no hash or raw material.
- **Safe copy/paste workflow:** the `keys add` output fences the raw key for
  easy selection and states it is shown once; nothing is written to disk by the
  CLI.

## Integration

- **KeyStore** (`app/services/key_store.py`): `keys add` → `create(label,
  scopes, expires_at)`; `keys list` → `list()`; `keys remove` → `revoke(id)`;
  `keys test` → new additive method `classify(token) -> {"status",
  "meta"|None}` with statuses `ok` / `invalid` / `expired` / `revoked` (see
  "KeyStore additive method" below). The CLI constructs `KeyStore()` at the
  default `state_dir / "relay_keys.db"`; tests inject a temp-path store via a
  module-level `_store()` hook in `keys.py`.
- **KeyStore additive method (only store change):** `verify()` returns `None`
  for both "no match" and "revoked/expired", so `keys test` needs one new
  read-only method, `classify(token)`, that scans all rows (not just active)
  with the same constant-time scrypt + `hmac.compare_digest` loop, computes no
  side effects, and returns the status of the matched row (revoked → `revoked`;
  expired → `expired`; active → `ok`; no match → `invalid`). `verify()` and all
  existing methods are unchanged. This is additive and behavior-preserving;
  `key_store.py` is not in the phase plan's Phase-3 "must remain untouched"
  list.
- **ProviderKeyStore** (`app/services/provider_key_store.py`): used by
  `provider_keys.py` through `config_store` for writes, and read for `list`
  display. The CLI never writes the keyring directly.
- **config_store (single writer):** `provider keys set <id> <key>` →
  `config_store.set_provider_config(defn, api_key=key)` — routes to the keyring
  when `RELAY_KEYRING` is on, `.env` otherwise (byte-identical Phase 2
  behavior). `provider keys remove` → `set_provider_config(defn, api_key="")`
  (keyring remove / `.env` clear). This preserves the invariant that
  `config_store` is the only writer of provider configuration.
- **Keyring-enabled mode:** with `RELAY_KEYRING` on, `set`/`remove` touch the
  OS keyring and runtime resolution is keyring-first (Phase 2). `provider keys
  list` shows the effective key per Phase 2 precedence (keyring entry wins,
  else env value).
- **Env fallback behavior:** with `RELAY_KEYRING` off, `set`/`remove` go to
  `.env` exactly as the wizard does today; runtime continues to read env. No
  change to Phase 2 resolution.
- **Keyless providers:** `ollama` has `key_env=None`/`key_attr=None`; `provider
  keys` commands reject it with a clear message (no key concept).

## Testing plan

New `tests/test_key_cli.py`, invoking `app.cli.main(argv=[...])` (same pattern
as `test_packaging.py`). KeyStore instances injected at a `tmp_path`; keyring
paths use a fake `RELAY_KEYRING_BACKEND` or monkeypatched `provider_key_store`;
`.env` writes use monkeypatched `config_store.env_file`.

1. **CLI parsing:** each subcommand dispatches; unknown subcommand / missing
   required args → exit 2; `keys remove` without `--yes` in non-TTY → exit 1,
   nothing revoked; invalid `<provider-id>` → exit 1 with message.
2. **add/remove/list:** `add` prints raw key exactly once and it verifies;
   `list` shows metadata only (never hash/raw); `remove --yes` revokes
   (subsequent `list` shows `revoked_at`, `test` → `revoked`); `remove` unknown
   id → exit 1.
3. **Masking:** `provider keys list` masks values (`********last4`); short keys
   fully masked; `--json` output contains no raw material.
4. **Invalid keys / test outcomes:** wrong token → `invalid`; expired
   (`--expires-days` in the past) → `expired`; revoked → `revoked`; valid → `ok
   <label>`.
5. **Provider key lifecycle:** `set` → `list` shows present (masked); `remove`
   → absent; `remove` idempotent; stdin `-` path works; keyless provider
   rejected.
6. **Keyring disabled behavior:** `RELAY_KEYRING` off → `set` writes `.env`
   (monkeypatched env_file, byte-identical to `set_provider_config`), `list`
   reads env, `remove` clears.
7. **Regression:** existing `tests/test_packaging.py`, `test_ui_boundary.py`,
   `test_ui_terminal.py`, `test_setup_wizard.py` pass unchanged (module→package
   conversion preserves `import app.cli` and `main`); full suite green
   (1683 passed / 9 skipped / 28 pre-existing `test_rc_validation.py` failures,
   no new failures).

## Scope

### Files expected to change

- New: `app/cli/keys.py`, `app/cli/provider_keys.py`, `tests/test_key_cli.py`.
- Converted: `app/cli.py` → `app/cli/__init__.py` (package; entry point and
  public names unchanged).
- Modified: `app/services/key_store.py` (additive `classify` method only).

### Untouched

- `app/api/*`, `app/security/auth.py`, `app/main.py`, `app/services/reload.py`,
  `app/services/config_store.py` (behavior; CLI only calls it),
  `app/services/provider_key_store.py`, `app/providers/*` (factory, registry,
  clients), all persistence/state stores, `app/ui/*`, `app/setup/*` (wizard,
  key_validation), `app/core/config.py`, `app/core/terminal.py`,
  `app/cli/__init__.py` public behavior, `.env`, `.env.example`, `docs/*`,
  `PROJECT_LOG.md`.

### Migration impact

- None. `relay_keys.db` schema v1 is already created by Phase 1 on first use;
  Phase 3 only reads/writes it through `KeyStore`. Existing `.env` keys keep
  working (Phase 2 precedence). Keyring entries written during manual testing
  are inert while `RELAY_KEYRING` is off.

### Risks

- **Module→package conversion:** `app/cli.py` → `app/cli/`. Mitigated: all
  public names and the entry point string stay in `__init__.py`; the existing
  packaging/UI-boundary tests guard imports. Rollback: delete the package, restore
  `app/cli.py`.
- **`keys test` status fidelity:** `classify` must not alter `verify()`/hashing
  semantics; covered by new classify tests plus the unchanged `test_key_store.py`
  suite.
- **Secret exposure via process args:** positional `<key>` leaks to shell
  history/`ps`; mitigated with `-` stdin and getpass; documented in help text.
- **Interactive prompts in CI:** all commands are non-interactive-safe (never
  block on TTY-less stdin).
- **Store/state drift:** CLI-created keys are inert until Phase 4 auth; remove
  rolls back cleanly.

### Rollback strategy

- Revert the Phase-3 commit: subparsers disappear, `app/cli.py` module is
  restored, the additive `classify` method is removed. Keys created during
  testing remain in `relay_keys.db` (inert until Phase 4); keyring entries are
  inert while `RELAY_KEYRING` is off; `.env` values written by `provider keys`
  are cleared on `remove`. No data migration involved.

## Acceptance criteria

1. All five `keys` + three `provider keys` commands work end-to-end against the
   Phase-1 store / keyring.
2. Raw keys appear exactly once across the entire CLI surface (the `keys add`
   output); everything else is masked or absent.
3. Provider-key writes preserve the single-writer invariant and the Phase 2
   flag behavior (keyring on → keyring; off → `.env` byte-identical).
4. `keys test` reports ok/invalid/expired/revoked without changing store
   semantics; full suite shows no new failures.

Stop — no code, no commit until this plan is approved.
