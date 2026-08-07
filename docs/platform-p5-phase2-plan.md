# P5 — Phase 2 Plan: Runtime Provider-Key Integration

Status: **Planning only. No code yet.** Builds on the completed Phase 1
(`23ee1fe`, `feat: add secure key storage foundation (P5.1)`). Source:
`docs/platform-p5-plan.md` (approved), `docs/platform-p5-phase-plan.md` §Phase 2
(lines 149-208). This document is the concrete Phase 2 implementation plan; it
does not modify `PROJECT_LOG.md`.

Constraints (user-mandated): **no code in this phase — plan only**; no commit;
no `PROJECT_LOG.md`; stop after this document and wait for approval.

Baseline note: the current suite is **1666 passed, 9 skipped, 28 failed**. All 28
failures are pre-existing in `tests/test_rc_validation.py` (stale
`create_nvidia_provider`/`create_openai_provider` monkeypatches vs. the P4.1
registry refactor; verified on pristine HEAD). The "1620 passed" figure in the
older phase-plan predates P4.2/P4.3/P5.1 and is out of date. Phase 2 acceptance
is measured against the *current* baseline: **no new failures, existing 28
unchanged**.

---

## 1. Current key flow audit

Every place a provider API key enters or leaves the system today:

| # | Location | Role | Phase 2 action |
|---|---|---|---|
| 1 | `app/core/config.py:269,275,281,297,303,319,333` | `Settings.__init__` reads each `*_API_KEY` from `os.getenv` (`.env` loaded at `config.py:65`). | Unchanged — stays the env fallback source |
| 2 | `app/providers/factory.py:41` | `build_runtime_provider` passes `api_key=_settings_value(defn.key_attr, "")` into `defn.build_provider`. **Single construction point for every runtime provider** (`relay.py:169`). | **Modified** — resolve keyring-first |
| 3 | `app/services/reload.py:239-241` | On reload, `_apply_provider_side_effects` sets `provider.api_key = getattr(env, f"{prefix}_api_key")` when the env field changed, then re-runs discovery. | **Modified** — resolve keyring-first; detect keyring-driven changes |
| 4 | `app/services/config_store.py:69-70` | `set_provider_config(api_key=…)` writes the key into `.env` via `set_env(defn.key_env, …)`. Single writer of provider config. | **Modified** — route `api_key` to keyring when enabled |
| 5 | `app/setup/wizard.py:201` | `store.get_env(defn.key_env)` reads the current key for display/masking in `resolve_cloud_key`. | Unchanged (Phase 2 keeps wizard untouched; Phase 3 CLI + Phase 5 docs cover keyring-aware display) |
| 6 | `app/setup/key_validation.py:133,154` | Sets `provider.api_key` on a scratch provider for live validation. | Unchanged |
| 7 | Provider clients (e.g. `openai_compat_client`) | Read `provider.api_key` at request time to build auth headers. | Unchanged (Phase 2 guarantees the correct value is on the Provider) |

Registry data used for resolution: `ProviderDefinition.id` (keyring username),
`key_attr` (settings fallback attribute), `key_env` (env var, write path only).
`ollama` has `key_env=None`, `key_attr=None` → never touches keys (`registry.py:173,175`).
`lmstudio` has a key attr/env but `requires_api_key=False`.

Reload secrets discipline today: `_SECRET_FIELDS` (`reload.py:98-102`) names key
fields; `_redact` (`reload.py:161-169`) strips values from validation errors; the
reload report lists field names only, never values.

---

## 2. Design

### 2.1 Secret precedence contract (final, documented at Phase 5)

For a given provider id, the effective runtime key is:

1. **OS keyring** — `ProviderKeyStore.get(provider_id)` — *only when* the
   `RELAY_KEYRING` feature flag is enabled **and** an entry exists.
2. **Environment / `.env`** — `settings.<key_attr>` (the existing source).
3. **Empty string** — provider runs keyless, or `requires_api_key` gates
   discovery.

Rule 2 is reached when the flag is off, the entry is absent, or the keyring is
unavailable (`get` returns `""`). When the flag is **off**, rules 2-3 are the only
rules → behavior is byte-identical to today.

### 2.2 Feature flag / defaults

- `RELAY_KEYRING` → `Settings.relay_keyring_enabled`, default `false` (opt-in).
- `RELAY_KEYRING_BACKEND` → `Settings.relay_keyring_backend`, default `""`
  (OS-default backend). `ProviderKeyStore` reads this env var **directly, per
  call** (`provider_key_store.py:81-97`) so tests/headless servers can switch
  backends without restart; the Settings field mirrors it for introspection and
  is non-authoritative.
- Both fields are **non-secret and non-reloadable**: they are read at startup
  (`settings = Settings()`, `config.py:752`) and are **not** added to
  `_RELOADABLE_FIELDS`/`_SIMPLE_FIELDS` (`reload.py:34-127`). Toggling keyring
  mid-session requires a restart. This keeps reload comparison surface unchanged
  and prevents a mid-request source swap.

### 2.3 Shared resolver

New module-level function in `app/providers/factory.py` (reload.py already
imports from factory, so no new module / no new file):

```python
def resolve_provider_key(defn, source=None) -> str:
    """Keyring-first, env-fallback provider key resolution (P5 Phase 2)."""
    src = source if source is not None else settings
    if getattr(src, "relay_keyring_enabled", False):
        stored = provider_key_store.get(defn.id)
        if stored:
            return stored
    if not defn.key_attr:
        return ""
    return getattr(src, defn.key_attr, "") or ""
```

- `source` lets reload pass its freshly validated `Settings` (`env`) while the
  factory uses the singleton; default `None` → `settings`.
- `getattr(src, "relay_keyring_enabled", False)` keeps objects without the field
  (test fakes, `SimpleNamespace` envs, pre-Phase-2 code) on the byte-identical
  path.
- `provider_key_store.get` already returns `""` on any backend failure
  (`provider_key_store.py:42-47`), so an unavailable keyring degrades to rule 2.
- Reads only `app.services.provider_key_store.provider_key_store` (imported into
  the factory module namespace) so tests can monkeypatch it; no logging anywhere.

---

## 3. Integration points

### 3.1 `app/providers/factory.py`

`build_runtime_provider` (line 41) becomes:

```python
provider = defn.build_provider(
    api_key=resolve_provider_key(defn),
    base_url=base_url,
)
```

`defn.client().list_models(provider)` (line 52) needs no change: discovery already
gates on `provider.has_api_key() or not provider.requires_api_key` (line 50) and
the client reads `provider.api_key`. Off-flag path returns
`getattr(settings, key_attr, "") or ""`, exactly today's `_settings_value`.

### 3.2 `app/services/reload.py`

`_apply_provider_side_effects` (lines 205-261):

- Compute `keyring_enabled = bool(getattr(env, "relay_keyring_enabled", False))`
  and `new_key = resolve_provider_key(spec["defn"], env)`.
- Effective-key change detection:

```python
key_changed = (f"{prefix}_api_key" in applied_set) or (
    keyring_enabled and new_key != provider.api_key
)
```

- When `key_changed`: `provider.api_key = new_key` (replacing the direct env read
  at line 240), then the existing discovery block runs unchanged.
- When the flag is off, the second disjunct is always false → the env-diff rule
  is the only rule → byte-identical. The `provider is None` register branch
  (lines 225-235) already routes through `build_runtime_provider` → resolver.

**Truthful reporting.** `applied`/`unchanged` are computed from the env diff
before apply (`reload.py:302-312`). With the flag on, a keyring-applied key whose
env field did not change would otherwise appear in `unchanged`. `_apply_provider_side_effects`
returns the field names it applied via the keyring branch, and `reload_config`
merges them (append to `applied`, remove from `unchanged`) before building the
report. Flag off → empty merge → identical report. The report still carries
**field names only** — `_SECRET_FIELDS`/`_redact` semantics untouched.

Rollback compatibility: `_snapshot`/`_restore` already capture `provider.api_key`
(`reload.py:190,199`), which is source-agnostic; a mid-apply failure restores the
previous resolved key correctly.

### 3.3 `app/services/config_store.py`

`set_provider_config` `api_key` path (lines 69-70) branches on the flag:

```python
if api_key is not None and defn.key_env:
    if settings.relay_keyring_enabled:
        if api_key:
            provider_key_store.set(defn.id, api_key)
        else:
            provider_key_store.remove(defn.id)
    else:
        set_env(defn.key_env, api_key)
```

- Flag off → `set_env` path, byte-identical (empty string still writes `''` into
  `.env`, matching `test_set_provider_config_empty_string_clears`).
- Flag on → non-empty key goes to the OS keyring; `""` removes the keyring entry
  (`ProviderKeyStore.remove` is idempotent). **Never written to `.env`** while the
  flag is on.
- Non-key paths (`enabled`, `base_url`, `priority_models`) unchanged in both
  modes — always `.env`.
- Keyless providers (`key_env is None`, e.g. `ollama`) never touch the keyring.
- Single-writer invariant preserved: `config_store` remains the only caller of
  `provider_key_store.set/remove`; the wizard/CLI never write keyring entries
  directly.

### 3.4 `app/core/config.py`

Two new fields in `Settings.__init__` (near the Relay section, `config.py:354+`):

```python
# Keyring (P5 Phase 2): keyring-first provider-key resolution. Opt-in,
# non-reloadable; RELAY_KEYRING_BACKEND mirrors the store's dynamic read.
self.relay_keyring_enabled = (
    os.getenv("RELAY_KEYRING", "false").lower() == "true"
)
self.relay_keyring_backend = os.getenv("RELAY_KEYRING_BACKEND", "")
```

No effect on `_RELOADABLE_FIELDS`, no exhaustive settings dump exists (the TUI
`_CONFIG_ROWS` list is explicit, `app/ui/data.py:229`), and no code iterates
`vars(settings)` → adding fields is safe.

---

## 4. Security

- **No plaintext persistence.** With `RELAY_KEYRING` on, provider keys are never
  written to `.env` by `config_store`; they live only in the OS credential store
  (backend encrypts at rest). App keys remain scrypt-hashed in `relay_keys.db`
  (Phase 1). A stale pre-existing `.env` key is **not** auto-deleted (see §5).
- **Redaction preserved.** `_SECRET_FIELDS`, `_redact`, and the reload report
  name-only rule are untouched; the resolver adds no new output path.
- **No logging of raw keys.** `resolve_provider_key`, the config_store branch, and
  the reload branch perform no logging/printing/`repr`; `provider_key_store` has
  zero log statements (Phase 1, verified).
- **Failure behavior when keyring is unavailable.**
  - *Read* (`get`): any backend exception → `""` → falls through to env (rule 2).
    Runtime keeps working with the existing key; defined, degraded behavior.
  - *Write* (`set`/`remove`): the exception propagates out of
    `set_provider_config` to the caller (wizard). **No fallback to `.env`** — a
    silent downgrade to plaintext would defeat the flag's purpose. The wizard
    surfaces the error; the user can disable `RELAY_KEYRING`.

---

## 5. Migration / backward compatibility

- **No stored-state migration.** `relay_keys.db` and `relay_state.db` are
  untouched by Phase 2 (the key store is still only consumed by tests).
- **Default off.** `RELAY_KEYRING` unset → factory, reload, and config_store
  behave byte-identically; existing `.env` keys keep working via precedence
  rule 2.
- **Existing `.env` keys + flag on.** Keyring wins when a matching entry exists;
  otherwise the `.env` value is used unchanged. No user setup breaks.
- **Deliberate non-destructive choice:** enabling the flag does **not** delete an
  existing `.env` key (a stale plaintext copy is masked by precedence, not
  removed). The Phase 5 `relay keys provider migrate` command moves keys on the
  user's schedule. Documented here now, in `docs/configuration.md` at Phase 5.
- **Keyring entries written during testing** are inert while the flag is off.
- **Known Phase 2 limitation (accepted):** the wizard's "existing key detected"
  display (`wizard.py:201`) reads `.env` only, so a keyring-only key won't be
  shown/masked there. Out of scope by phase-plan mandate
  (`platform-p5-phase-plan.md:175-177`); Phase 3 CLI and Phase 5 docs close the
  gap.

---

## 6. Testing plan

All new cases live in the existing three test files (per phase-plan
`platform-p5-phase-plan.md:172-173`). Every keyring-path test pins both
`settings.relay_keyring_enabled` and a stub `provider_key_store` so the OS keyring
is never touched and the run is hermetic (monkeypatch
`factory.provider_key_store` / `config_store.provider_key_store`, or set
`RELAY_KEYRING_BACKEND` to a fake backend).

### 6.1 `tests/test_provider_factory.py`
- **Flag off = byte-identical + never consults keyring:** stub
  `provider_key_store.get` to raise; assert `build_runtime_provider` uses the
  settings value and the stub is never called.
- **Flag on, keyring entry present:** entry wins over a differing
  `settings.<key_attr>` value; discovery runs with the resolved key
  (patched `list_models` asserts the provider's key).
- **Flag on, entry absent:** `get` → `""` → env value used.
- **Flag on, keyring unavailable:** stub raises → env value used.
- **Flag on, local provider (`lmstudio`):** no entry → `""` (keyless behavior
  unchanged); with an entry, the key is applied.
- **Existing tests unchanged and green** (they don't set the flag → off path).

### 6.2 `tests/test_reload.py`
- **Flag on, env unchanged, keyring entry present:** reload applies the keyring
  key; `provider.api_key` updated; report lists the field in `applied` (moved from
  `unchanged`); field appears **by name only** — assert no value (env or keyring)
  in the report.
- **Flag on, env has a key, no keyring entry:** env fallback used.
- **Flag on, keyring entry removed between reloads:** key reverts to env value;
  discovery re-runs.
- **Rollback:** flag on, a later apply step fails → `provider.api_key` restored to
  the pre-reload value.
- **Dry-run:** unchanged (still reports env diff, mutates nothing).
- **Flag off:** existing reload tests pass unchanged (regression proof).

### 6.3 `tests/test_config_store.py`
- **Flag on:** `api_key="sk-x"` → stub `provider_key_store.set(defn.id, "sk-x")`
  called once; `.env` file contains **no** `key_env` line; `enabled`/`priority`
  still written to `.env`.
- **Flag on, `api_key=""`:** `remove(defn.id)` called; `.env` untouched.
- **Flag on, keyless provider (`ollama`):** keyring never touched.
- **Flag off:** existing tests pass byte-identical (the whole current file, esp.
  `test_set_provider_config_writes_all_fields` / `_empty_string_clears`).
- **Non-key fields** written to `.env` in both modes.

### 6.4 Regression / gate
- Full suite on the venv (`.venv\Scripts\python.exe`): **no new failures** —
  expected `1666 passed, 9 skipped, 28 failed` (the 28 pre-existing in
  `test_rc_validation.py`).
- `tests/test_auth.py`, `tests/test_provider_conformance.py`, and the existing
  factory/reload/config_store suites green → proves auth and provider behavior
  unchanged.
- Byte-identical diff proof: with the flag unset, run the factory/reload/config
  store suites and diff results against the pre-Phase-2 commit.

---

## 7. Untouched surfaces

- `app/api/*` (routes, middleware, admin, feedback) — no request-path change.
- `app/security/auth.py` — bootstrap auth stays byte-identical (Phase 4).
- Persistence / state stores — `state_store`, `ops_store`, `health_store`,
  `quality_store`, `telemetry`, `relay_state.db`.
- Provider clients — `base.py`, `nvidia_client`, `openai_client`,
  `openai_compat_client`, `lmstudio_client`, `anthropic_client`, `gemini_client`,
  `ollama_client`.
- `app/main.py`, `app/cli.py`, `app/ui/*`, `app/setup/*`, `app/core/relay.py`.
- `app/services/key_store.py` (Phase 1) — no change.
- `.env.example`, `docs/*`, README — Phase 5.
- `PROJECT_LOG.md` — never.

---

## 8. Files changed (Phase 2) and rollback

**Modified (4):**
- `app/providers/factory.py` — `resolve_provider_key` + `build_runtime_provider`.
- `app/services/reload.py` — keyring-aware key application + report merge.
- `app/services/config_store.py` — `api_key` write-path branch.
- `app/core/config.py` — `relay_keyring_enabled`, `relay_keyring_backend`.

**Modified (tests, 3):** `tests/test_provider_factory.py`, `tests/test_reload.py`,
`tests/test_config_store.py`.

**Rollback:** revert the Phase 2 commit. Because the flag defaults off, every path
returns to `.env`-driven behavior exactly; keyring entries written during testing
are inert. No data migration involved.

---

## 9. Acceptance criteria

1. `RELAY_KEYRING` unset → factory/reload/config_store behavior is byte-identical
   (diff-tested); full suite shows no new failures vs. baseline.
2. `RELAY_KEYRING=true` → keyring entry wins on read; writes go to the keyring;
   `.env` never receives a key; unavailable keyring degrades to env on read and
   fails closed on write.
3. All new tests in §6 green; existing factory/reload/config_store/auth/conformance
   suites unchanged and green.
4. No API contract, provider-client, state-store, or `PROJECT_LOG.md` changes.

Stop — no code, no commit until this plan is approved.
