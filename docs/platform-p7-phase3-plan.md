# P7.3 — Full TUI Configuration Panel and Configuration Accessibility

Status: **Approved. Implemented. Tests green. Awaiting commit approval.**
Depends on: P7.1 (`c5c1e762`), P7.2 (`fd22fdb`).

Scope: per `docs/platform-p7-plan.md` §4 P7.3 — rebuild the TUI Configuration
tab from `config_spec.py`, eliminate the hardcoded configuration row lists,
and prove every non-secret setting is reachable without editing `.env` by
hand. No API contract changes, no schema/persistence changes, no `relay.py`
reload-engine changes, `PROJECT_LOG.md` untouched, plan stays uncommitted.

---

## 1. Current state after P7.2

### 1.1 Registry and mutation layers (done in P7.1/P7.2, unchanged here)

- `app/core/config_spec.py` is the single source of truth: **103** frozen
  `SettingSpec` entries with `env`/`attr`/`type`/`default`/`description`/
  `category`/`secret`/`effect` (LIVE|RESTART|INFO)/validation bounds, plus
  `validate_value`, `parse_value`, `render_value`, and derived tuples
  (`reloadable_fields`, `secret_fields`, `tui_fields`, ...).
- `app/services/config_store.py` is the only `.env` writer: atomic
  `_apply_atomic` (temp + `os.replace`, `0600`, lock), `set_env`/`unset_env`,
  keyring-aware `set_provider_config`.
- `app/services/config_mutation.py` (new in P7.2) is the controlled mutation
  layer: `set_setting(env, raw, reload=True, dry_run=False)`,
  `unset_setting(...)`, `reload_settings_report(...)`, dry-run masked
  previews, `ConfigUsageError` (refused, exit 2, no write) / `ConfigMutationError`
  (write/apply failure, exit 1, `err.restored`), `_apply_settings_reload`
  (**singleton-only** apply — CLI-safe, never imports `app.core.relay`).
- CLI `relay config set/unset/reload` is complete and emits
  `config.set`/`config.unset`/`config.reload` audit events. **CLI already
  reaches all 94 non-secret env-backed fields.**

### 1.2 The TUI Configuration panel is still the pre-P7 hardcoded form

- `app/ui/data.py::_CONFIG_ROWS` — a hand-maintained tuple of **23 rows**
  (15 live-editable routing/failover + 8 restart read-only). No spec
  derivation; no secret rows; informational group empty.
- `app/core/config_spec.py::_TUI_META` — a **second** hand-maintained copy of
  the same 23 rows (kind/group/editable/label/hint), attached to `SPECS` by
  `_with_tui`. The golden test locks `tui_fields() == _CONFIG_ROWS == 23`.
- `app/ui/screens/configuration.py` renders `ConfigField`s grouped by
  `_GROUP_TITLES` (routing/failover/restart/info); restart and info are
  read-only; secrets never appear.
- `ServiceFacade.save_config` writes via `config_store.set_env` then applies
  with the **full** `reload_config(relay, ...)` engine (provider/runtime
  refresh + snapshot rollback), restoring `.env` originals on failure.

### 1.3 Field census (drives the panel surface)

| Class | Count | Notes |
|---|---|---|
| Total specs | 103 | `len(SPECS) == len(vars(Settings()))` |
| Secrets | 8 | 7 provider `*_api_key` + `relay_api_key` |
| Non-secret, env-backed | **94** | 73 live + 21 restart |
| Live non-secret env-backed | 73 | current TUI shows only 15 |
| Restart non-secret env-backed | 21 | current TUI shows 8 (read-only) |
| Informational env-less | 1 | `relay_name` (no env var, CLI-hidden) |
| Categories | 18 | general, task_routing, health, scoring, task_catalog, adaptive, quality, decision, persistence, task_classification, telemetry, observability, proxy, auth, providers, relay, logging, request_log |

### 1.4 Constraints carried into P7.3

- `app/core/config.py` (Settings parsing/defaults) and
  `app/services/reload.py` (allowlist + rollback semantics) stay
  **byte-identical**.
- `config_spec.py` remains the single source of truth; the TUI must consume
  the registry and `config_mutation` — **no second configuration system
  inside the TUI**.
- Textual may only be imported under `app/ui`; `app/ui/data.py` stays
  Textual-free; screens read state only through `ServiceFacade`
  (`tests/test_ui_boundary.py`).
- No API contract or persistence/schema changes.

---

## 2. Remaining P7 goals (what P7.3 closes)

Per `docs/platform-p7-plan.md` §3 gaps G2 (partial TUI/accessibility) and G8
(hardcoded panel) and §4 P7.3:

1. **Full TUI configuration coverage.** Every spec field appears in the
   Configuration tab — 73 live fields editable and live-applied, 21 restart
   fields editable with a restart notice, 8 secrets shown masked read-only,
   1 informational row read-only.
2. **Eliminate hardcoded configuration lists where possible.**
   - Remove `app/ui/data.py::_CONFIG_ROWS` / `_EDITABLE_FIELDS` /
     `_RESTART_FIELDS` / `_GROUP_TITLES`.
   - Remove `app/core/config_spec.py::_TUI_META` and `_with_tui` (and the
     dead `tui`/`tui_kind`/`tui_group`/`tui_editable`/`label`/`hint`
     dataclass fields), replacing them with **derived** classification
     helpers so nothing hand-maintains a field list.
   - The reload allowlists in `app/services/reload.py` are **not** touched
     (byte-identical constraint); the spec already mirrors them and that
     parity is test-locked.
3. **Prove configuration accessibility without manual file editing.** A test
   walks the registry and asserts every non-secret env-backed field has a CLI
   (`config_mutation.set_setting` dry-run accepted) **and** TUI
   (`config_form()` row) edit path; secrets document their provider-keys /
   keyring path.

---

## 3. TUI architecture

### 3.1 How screens consume `config_spec.py`

- `config_spec.py` owns only registry data and **derived classification
  helpers** (new, replacing `_TUI_META`):
  - `tui_kind_for(spec)` → `"bool"` if `type == "bool"` else `"csv"` if
    `type == "csv"` else `"text"`.
  - `tui_group_for(spec)` → `spec.effect` (`"live"`/`"restart"`/`"info"`).
  - `tui_editable_for(spec)` → `spec.env is not None and not spec.secret`
    (LIVE and RESTART both editable; RESTART writes with a notice).
  - `label_for(spec)` → human label derived from `env` (title-case words,
    acronyms preserved), `hint_for(spec)` → `spec.description`.
  - `tui_fields()` → **all 103 specs** (the full panel surface), replacing
    the current "23 rows" contract.
- `app/ui/data.py::config_form()` iterates `config_spec.SPECS`, builds a
  `ConfigField` (view-model, Textual-free) per spec using the helpers above
  plus the live effective value from `settings`. No per-field hand rows.
- `app/ui/screens/configuration.py` renders only `ConfigField`s from the
  facade — it still never imports `app.core`/`app.providers`.

### 3.2 How editable / read-only / restart-required fields are determined

| Spec properties | TUI row | Save behavior |
|---|---|---|
| `not secret` and `env is not None` and `effect == LIVE` | editable Input/Checkbox | written + live-applied via the full reload engine |
| `not secret` and `env is not None` and `effect == RESTART` | editable Input/Checkbox | written; **not** applied; status notes "restart required for: <env>" |
| `secret` | **masked read-only** Static row (`mask_key` value or `(unset)`); widget never holds the raw value | not editable; hint points to Providers tab / `relay provider keys` |
| `env is None` (info, `relay_name`) | read-only row (current value) | not editable |

This is derived entirely from existing spec fields (`secret`, `effect`,
`env`), so a new setting automatically appears with the correct behavior —
no list to update.

### 3.3 How secrets are displayed

- Secret rows render the value with `mask_key(value)` (e.g. `********abcd`)
  or `(unset)`; the **raw value is never placed in any widget** (rows are
  `Static` labels, not `Input`s), so it cannot leak into focused-widget
  buffers, CSS `content`, or serialized widget state.
- Secret fields are never written by this panel: `config_mutation` refuses
  them (`ConfigUsageError`), and the UI marks them read-only with a pointer
  to the Providers flow (keyring-aware) — matching the CLI contract.
- `relay_api_key` shows masked and is managed by setup; provider keys are
  shown masked and edited on tab 4 (Providers).

### 3.4 How writes go through `config_mutation.py`

The TUI must not open a second write path. New `ServiceFacade.save_config`
flow (replaces the current `set_env` loop):

1. **Validate everything, write nothing:** for each changed env, call
   `config_mutation.set_setting(env, value, reload=False, dry_run=True)`.
   `ConfigUsageError` (unknown / secret / refused) aborts the save with a
   redacted message and **zero writes**. Dry-run previews (masked) are
   collected for the status summary.
2. **Persist:** write the accepted changes through `config_store`
   (`set_env`; provider keys are not reachable here — refused at step 1),
   capturing `get_env` originals for rollback (existing `_restore_env`).
3. **Apply live:** if any changed field is live-reloadable, run the existing
   full engine `reload_config(relay, dotenv_path=env_file)` (dry-run then
   apply), exactly as today. This is deliberate: `reload_config` refreshes
   providers/routing/health/scoring/decision/telemetry/quality/state-flusher,
   whereas `config_mutation._apply_settings_reload` is the **singleton-only,
   CLI-safe** apply and would leave the running server stale. Live apply stays
   on the engine that has a `relay` instance; restart fields are written but
   never passed through apply.
4. **Report:** status line shows `applied`/`unchanged`/`restart-required`
   counts and any redacted failure; on failure, restore file originals
   (runtime is already restored by `reload_config`'s snapshot).
5. **Audit:** emit `config.set`/`config.unset` (and `config.reload`) events
   with counts-only details, matching the CLI emit shape.

`config_mutation.py` gains no new public API for P7.3 (step 1 reuses the
existing dry-run path); if a batch helper is judged cleaner during
implementation it must be additive and covered by tests.

---

## 4. Files expected to change

**Modified:**
- `app/core/config_spec.py` — remove `_TUI_META`, `_with_tui`, and the dead
  TUI dataclass fields (`tui`, `tui_kind`, `tui_group`, `tui_editable`,
  `label`, `hint`); `SPECS = _RAW_SPECS`; add derived helpers
  (`tui_kind_for`, `tui_group_for`, `tui_editable_for`, `label_for`,
  `hint_for`); redefine `tui_fields()` to return the full panel surface.
  No change to `env`/`attr`/`type`/`default`/`secret`/`effect`/bounds or any
  validation/derived-tuple behavior (`reloadable_fields`, `secret_fields`,
  etc. stay byte-identical).
- `app/ui/data.py` — delete `_CONFIG_ROWS`/`_EDITABLE_FIELDS`/
  `_RESTART_FIELDS`/`_GROUP_TITLES`; extend `ConfigField` with `secret: bool`
  (masked value already carried in `value`); `config_form()` derived from
  `SPECS`; `save_config()` routed through `config_mutation` per §3.4;
  `config_restart_required_fields()` derived.
- `app/ui/screens/configuration.py` — render the full panel grouped by effect
  (live / restart / informational) then category; editable restart rows with
  a restart notice; masked read-only secret rows; status-line summaries.
- `tests/test_config_spec.py` — replace the four `_CONFIG_ROWS`/`tui_fields`
  golden tests with the new contract (full surface, derived classification,
  no hand rows); keep all registry/reload parity tests unchanged.
- `tests/test_ui_configuration.py` — rewrite/extend: full-surface coverage,
  restart editing + notice, secret masking (raw value never in widget state),
  save-through-`config_mutation`, rollback, reachability proof.

**New:**
- `tests/test_config_accessibility.py` — the walk-the-spec reachability proof
  (or fold into `test_ui_configuration.py`; see §8).

**Unchanged (explicitly):** everything in §5.

---

## 5. Files that must remain untouched

- `app/core/config.py` — `Settings` parsing/defaults/`reload_settings`
  byte-identical.
- `app/services/reload.py` — allowlist + rollback semantics byte-identical.
- `app/services/config_store.py` — write-path guarantees (single writer,
  atomic, keyring-aware, no echo); consumed as-is.
- `app/services/config_mutation.py` — P7.2 API unchanged (only optional
  additive batch helper per §3.4 if approved).
- `app/services/event_log.py` — `config.*` actions already present.
- `app/api/*` — no public API contract changes (`POST /admin/reload` etc.).
- `app/providers/*`, `app/services/platform_store.py`, all migrations.
- `app/cli/*` (incl. `config.py`, `provider_keys.py`) — no changes required;
  CLI is already complete and is one leg of the reachability proof.
- `app/ui/app.py` — screen registration unchanged (Configuration tab stays
  tab 5); only `screens/configuration.py` and `data.py` change.
- `PROJECT_LOG.md`, `docs/security.md`, `docs/platform-db-schema.md`,
  `.env.example`. (`docs/tui-guide.md` update is optional, only if approved.)

---

## 6. Security considerations

- **Secrets never rendered raw:** secret rows are masked `Static`s; raw
  values never enter any widget or composed output; a test asserts the raw
  secret string is absent from widget values and screen content.
- **No new write surface for secrets:** `config_mutation` refuses secret
  fields, so the panel cannot write them; provider keys stay on the
  keyring-aware Providers flow.
- **Single writer preserved:** all persistence flows through
  `config_store`; validation happens before any write (dry-run);
  `ConfigUsageError` aborts with zero writes.
- **Isolation:** dry-run validation never mutates `os.environ` or the
  singleton; apply failure restores the file and the runtime snapshot.
- **Redaction:** errors surfaced in the status line are `config_mutation`'s
  redacted messages (field-name-only) or `reload`'s `_redact` output; audit
  events carry counts/booleans only, never values.
- **Textual boundary:** `config_spec.py` and `data.py` stay Textual-free;
  screens keep the facade-only import rule (`test_ui_boundary.py` stays
  green).
- No new secret material is introduced; `test_redaction.py` and the CLI
  masking tests remain green.

---

## 7. User experience considerations

- **Panel is now complete:** live settings are edited and applied on save;
  restart settings are edited, written, and marked "restart required for:
  <env>" (never silently applied); secrets are visible as masked rows with a
  pointer to the Providers tab; the informational row is read-only.
- **Grouping scales to 103 rows:** fixed effect sections (Applied live /
  Restart required / Informational), each sub-grouped by the spec
  `category` in registry order (stable, deterministic). Group headers are
  static text; the body stays in `VerticalScroll` (existing `#config-root`).
- **Existing interactions preserved:** `ctrl+s` save, `r` refresh, Revert
  discards unsaved edits, buttons stay in `#config-controls`. Save runs in a
  worker via `asyncio.to_thread` (as today) so the UI does not freeze during
  reload.
- **Save feedback:** status line shows applied/unchanged/restart-required
  counts, and for failure a redacted reason plus "previous values restored".
  No numeric values or raw settings appear in any message.
- **Dirty feedback:** Revert/Refresh retain current semantics; a pending
  change indicator is out of scope (keeps diff minimal).
- **Defaults clarity:** unset values render the effective default in the row
  value (via `render_value`) and the spec hint shows the documented default,
  so users see the running behavior without opening `.env`.

---

## 8. Testing strategy

- **Golden updates (`test_config_spec.py`):**
  - `tui_fields()` now covers the full panel surface (103 specs); the
    `_CONFIG_ROWS`-equality tests are replaced by derivation tests
    (`tui_kind_for`/`tui_group_for`/`tui_editable_for` agree with
    type/effect/secret; secrets are surfaced but never editable).
  - All registry/reload parity tests (spec↔Settings 1:1, reload allowlist
    byte-for-byte, secret set, provider triplets, validator reuse) remain
    green and unmodified.
- **`test_ui_configuration.py` (rewritten/extended, headless `run_test` +
  Pilot):**
  - Full surface: every spec env appears as a row with the correct
    editable/restart/secret classification.
  - Restart edit: save writes the file, does **not** apply, shows the restart
    notice; the running singleton value is unchanged.
  - Secret rows: masked value displayed, widget is not an `Input`, and the
    raw secret string never appears in widget values or composed screen text.
  - Save-through-`config_mutation`: invalid input → `ConfigUsageError`,
    zero writes; live success applies via `reload_config` (spy asserts
    dry-run-then-apply call order); apply failure restores file + runtime;
    audit events emitted with counts-only details.
  - Regression: existing save/rollback/restart/classification tests updated
    only where the row set changed.
- **New `tests/test_config_accessibility.py` (the exit-criterion proof):**
  - Walk `SPECS`; for every non-secret env-backed spec assert both legs:
    (a) `config_mutation.set_setting(env, <valid sample>, reload=False,
    dry_run=True)` is accepted (CLI/registry path), and
    (b) `facade.config_form()` yields an editable `ConfigField` for it (TUI
    path). For the 8 secrets, assert they are refused by `set_setting` and
    surfaced masked in `config_form()` (keyring/provider-keys path is the
    documented editor). `relay_name` (env-less INFO) is read-only by design.
- **Boundary regression:** `test_ui_boundary.py` must stay green
  (config_spec/data.py Textual-free, screens facade-only).
- **Full suite:** at or above the current baseline **2039 passed, 20
  skipped**, including `test_reload.py`, `test_admin_reload.py`,
  `test_config_store.py`, `test_config_cli.py`, `test_config_mutation.py`,
  `test_redaction.py`, and all provider runtime reload tests.

---

## 9. Rollback strategy

- **Validation failure:** nothing was written (dry-run aborts) — no rollback
  needed.
- **Write failure mid-batch:** already-written keys are restored to their
  `get_env` originals (existing `_restore_env`), so the file is consistent.
- **Apply failure:** `reload_config`'s own snapshot restores the runtime
  surface; the facade restores the file originals so file and running state
  agree — same guarantee as today, now through the `config_mutation`
  validation gate.
- **No schema/migration rollback** — no schema or API changes.
- **Operator undo trail:** each save emits `config.set`/`config.unset` audit
  events (already supported by `event_log.EVENT_ACTIONS`), giving an
  operator the exact env/value to revert via `relay config unset`.

---

## 10. Acceptance criteria

1. The Configuration tab renders **every** spec field (103) grouped by effect
   then category; no hardcoded field list remains in `app/ui` or
   `config_spec.py` (`_CONFIG_ROWS` and `_TUI_META` deleted).
2. Every non-secret env-backed field is editable: live fields apply on save
   through the full reload engine; restart fields write with a "restart
   required" notice and are never live-applied.
3. Secret fields are masked read-only; a test asserts the raw secret value
   never appears in any widget value or composed screen output.
4. Saves are validated and persisted through `config_mutation`/`config_store`
   (dry-run first, zero writes on refusal) and applied via `reload_config`;
   any failure restores file + runtime and the status line shows a redacted
   reason.
5. **Requirement 4 met:** the accessibility test proves every non-secret
   setting has a CLI (`config set/unset`) and TUI edit path without manual
   file editing; secrets have a documented provider-keys/keyring path.
6. `tui_fields()` derivation tests and the walk-the-spec proof are green;
   all spec↔Settings and spec↔reload parity tests unchanged and green.
7. Full suite at or above the baseline (2039 passed, 20 skipped) with no new
   failures; `app/core/config.py` and `app/services/reload.py` byte-identical;
   no API contract or schema changes; `PROJECT_LOG.md` untouched.

---

## Files referenced during this audit

- `app/core/config_spec.py`, `app/core/config.py`,
  `app/services/config_store.py`, `app/services/config_mutation.py`,
  `app/services/reload.py`, `app/services/event_log.py`,
  `app/ui/data.py`, `app/ui/screens/configuration.py`, `app/ui/app.py`.
- `tests/test_config_spec.py`, `tests/test_ui_configuration.py`,
  `tests/test_ui_boundary.py`, `tests/test_config_cli.py`,
  `tests/test_config_mutation.py`.
- `docs/platform-p7-plan.md`, `docs/platform-p7-phase2-plan.md`.
