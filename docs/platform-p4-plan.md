# P4 Plan — Provider Integrations (Async-First): Sub-phase P4.1

Status: **draft — planning only. No code written.**

Scope: P4.1 — **de-string-name the runtime provider system.** Move runtime
provider *selection* (creation, lookup, client resolution, reload) from
display-name strings to stable provider IDs. P4.2 (documented in §8) then
migrates the remaining display-name-keyed surfaces (stores, API payloads,
persisted state) so display names become UI-only metadata.

Regression gate for P4.1: full suite stays green (baseline **1217 passed, 7
skipped, 1 pre-existing failure** in `test_ui_app.py`, unrelated to providers),
wire behavior of `/chat` and `/v1` unchanged, `relay serve` / TUI entry points
unchanged.

---

## 1. Current architecture

Verified by inspection at planning time (2026-08-05).

### 1.1 Provider identity today

There are **three strings per provider**, and only one is stable:

| String | Source (`app/providers/registry.py`) | Example | Role today |
|---|---|---|---|
| `id` | `ProviderDefinition.id` | `nvidia` | Stable slug; setup/UI/config use it |
| `provider_name` | `ProviderDefinition.provider_name` | `NVIDIA` | **Runtime identity**: `Provider.name`, `ClientRegistry` keys |
| `display_name` | `ProviderDefinition.display_name` | `NVIDIA NIM` | Setup menu label only |

`PROVIDER_REGISTRY` is already **id-keyed** (`test_provider_registry.py`
guards the six ids). `PROVIDER_MENU` is id-ordered.

### 1.2 Two construction paths

**Setup/UI path — already registry-driven (id-keyed):**
- `app/setup/wizard.py:244` `_configure_provider`: `defn.client()` +
  `defn.build_provider()`.
- `app/services/config_store.py:51` `set_provider_config(defn, ...)`: writes
  `.env` from `defn.enabled_env` / `key_env` / `base_url_env` / `priority_env`.
- `app/ui/data.py:131` already maps `provider_name → id`.
- Wizard writes configured **provider ids** to setup state; `RUNTIME_READY =
  {"nvidia", "openai", "lmstudio"}` (`app/setup/wizard.py:32`).

**Runtime path — hardcoded and name-keyed (the P4.1 target):**
- `app/core/relay.py:161` `_load_providers`: three hardcoded factories
  (`create_nvidia_provider`, `create_openai_provider`, `create_lmstudio_provider`)
  gated by `settings.*_enabled`.
- `app/providers/nvidia.py`, `openai.py`, `lmstudio.py`: each hand-builds a
  `Provider(name="NVIDIA" | "OpenAI" | "LM Studio", ...)`, hardcodes
  base_url/priority, and runs model discovery.
- `app/services/provider_manager.py`: `_providers` dict **keyed by
  `provider.name`**; `get(name)`.
- `app/services/client_registry.py`: maps **`provider_name` → client
  instance** (all six providers already registered).
- `app/services/chat_service.py:50`, `async_chat_service.py:72`,
  `health_checker.py:75`: `self.registry.get(provider.name)`.
- `app/services/reload.py`: `_PROVIDER_SPECS` hardcodes
  `(name, prefix, factory, client)` for the three runtime providers;
  `_snapshot`/`_apply_provider_side_effects` look up
  `provider_manager.get(spec["name"])`; `_RELOADABLE_FIELDS` built from the
  hardcoded `_PROVIDER_PREFIXES`.
- `app/core/relay.py:204` `choose_provider`: `provider.name ==
  decision.selected.provider`.

### 1.3 `Provider` shape (`app/providers/base.py`)

`Provider(name, base_url, health_endpoint, api_key, enabled, priority,
requires_api_key, proxy, models, priority_models)`. **No `id` field.**
`build_provider()` (registry.py:55) sets `name=provider_name`, base_url, key,
`enabled=True`, `priority=runtime_priority`, `requires_api_key`,
`health_endpoint` — but not an id.

### 1.4 Name-keyed consumers (inventory)

Stores and outputs keyed by `provider.name` (unchanged in P4.1, migrated in
P4.2):
- `candidate_builder.py` (reports/learned/telemetry/quality lookups,
  `Rankable.provider = provider.name`).
- `health_store`, `telemetry`, `quality`, `decision_stats` keys.
- Attempts dicts `{"provider": provider.name}` in both chat services →
  telemetry/feedback recording.
- API output: `app/api/providers.py:13` (`name`), `app/api/decision.py:27`
  (`name`), `relay.health()` (`name`), `app/services/diagnostics.py:93`.
- Client metrics labels (`app/providers/*_client.py` pass `provider.name`).

### 1.5 Concrete drift found during inspection

- `openai.py` factory uses `priority=5`; registry `runtime_priority=9` for
  `openai`. Runtime and setup/UI would rank OpenAI differently (P4.2 wiring
  would change behavior). Must be reconciled before the generic factory lands.
- `nvidia.py`/`openai.py` factories do **not** set `requires_api_key` /
  `health_endpoint` (defaults are correct by luck); lmstudio factory sets
  `requires_api_key=False` and reads `settings.lmstudio_priority`.
- `Settings` parses `NVIDIA_BASE_URL`/`OPENAI_BASE_URL`? No — `base_url_env`
  on those defs is currently unused.
- `DEFAULT_PROVIDER` defaults to `"NVIDIA"` (a name), unused today.

---

## 2. Problems with current string-based wiring

1. **Display-ish names are runtime identity.** `Provider.name` ("NVIDIA",
   "LM Studio") keys `ProviderManager`, `ClientRegistry`, and — transitively —
   every learned store, persisted key, and API field. Renaming a display label
   (e.g. "Google Gemini") silently orphans learned state and changes API
   payloads.
2. **Adding a provider touches many hardcoded lists.** P4.2 (OpenRouter, Groq,
   custom OpenAI-compatible) would require editing `Relay._load_providers`,
   `reload._PROVIDER_SPECS`, `reload._PROVIDER_PREFIXES`,
   `_RELOADABLE_FIELDS`, `_SECRET_FIELDS`, plus a factory module — directly
   contradicting "adding a provider is a registry entry" (`test_provider_registry`
   docstring).
3. **Two sources of truth for provider metadata.** Runtime reads hardcoded
   factories/specs; setup/UI/config read the registry. The OpenAI priority
   drift (§1.5) is the proof: the same provider can rank differently depending
   on which path built it.
4. **Reload duplicates registry data.** `_PROVIDER_SPECS` re-declares
   name/prefix/factory/client that already live on `ProviderDefinition`, so
   the reload allowlist can silently fall out of sync with the registry.
5. **Config is already id-prefixed, runtime isn't.** `.env` uses
   `NVIDIA_*`/`OPENAI_*` prefixes matching registry ids, but runtime keying
   uses names — the seam is exactly at the registry/factory boundary.
6. **No uniqueness guarantee.** `ProviderManager.register` keys by `name`;
   nothing enforces unique ids or rejects a name collision.

---

## 3. Proposed architecture

### 3.1 `Provider` gains a stable id with graceful fallback

- Add `id: str = ""` to `Provider` (default keeps every hand-built provider in
  the existing tests constructing `Provider(name="LM Studio", ...)` valid).
- Add `Provider.identity()` returning `self.id or self.name`. This is the
  **compatibility bridge**: registry-built providers resolve to the stable id;
  legacy hand-built providers resolve to their name exactly as today, so the
  whole existing suite stays green without edits.
- `ProviderDefinition.build_provider()` sets `id=defn.id` (plus today's
  `name=defn.provider_name`). Display metadata stays on the definition and on
  the Provider via `name` until P4.2.

### 3.2 Registry definitions drive runtime creation

- New `app/providers/factory.py::build_runtime_provider(defn)`: builds a
  `Provider` from one `ProviderDefinition` + `settings`:
  - `api_key = getattr(settings, defn.key_attr, "")` when `key_attr` set;
  - `base_url = defn.base_url_default` (settings override only where `Settings`
    actually parses the env today: lmstudio);
  - `priority = defn.runtime_priority` (reconciled — see §8 decision D2);
  - model discovery + `apply_model_priority` via `defn.client()`, key-gated
    for cloud (`requires_api_key`) and unconditional for local (mirrors the
    three factories today).
- `nvidia.py` / `openai.py` / `lmstudio.py` keep `create_provider()` as **thin
  wrappers** around `build_runtime_provider(PROVIDER_REGISTRY["..."])` so all
  existing imports/tests keep working while the registry becomes the real
  implementation.
- `Relay._load_providers` iterates `PROVIDER_REGISTRY` values filtered to
  `RUNTIME_READY`, gated by `getattr(settings, defn.enabled_attr)`, and calls
  `build_runtime_provider`.

### 3.3 Id-keyed manager and client resolution

- `ProviderManager`: key `_providers` by `provider.identity()` (id for
  runtime providers, name fallback for legacy). `register`/`get` accept the
  identity. Name-only callers keep working through the fallback.
- `ClientRegistry`: build `_by_id = {defn.id: defn.client_class()}` for every
  registry entry, plus a legacy `_by_name = {defn.provider_name: client}`
  alias map. `get(key)` resolves **id first, then legacy name** so both
  `get("lmstudio")` and today's `get("LM Studio")` work.
- `chat_service.py` / `async_chat_service.py` / `health_checker.py` resolve
  clients with `self.registry.get(provider.identity())`.

### 3.4 Reload resolves through registry definitions

- Derive `_PROVIDER_SPECS` from `PROVIDER_REGISTRY` filtered to `RUNTIME_READY`
  (preserving the exact reloadable set for P4.1): spec = `(id, prefix,
  factory=build_runtime_provider, client=defn.client())`, where `prefix`
  derives from `defn.enabled_env`/`key_env`/`priority_env` (strip the known
  suffixes).
- `_snapshot` / `_apply_provider_side_effects` look up
  `provider_manager.get(spec["id"])`.
- `_RELOADABLE_FIELDS` and `_SECRET_FIELDS` derive from registry defs (still
  scoped to `RUNTIME_READY` in P4.1 so the reload report is byte-identical).
- `Relay.choose_provider` keeps its name comparison in P4.1 (decision layer is
  name-keyed); §8 flags the P4.2 switch to identity.

### 3.5 What stays the same in P4.1

- `Provider.name`, store keys, attempts `provider` field, API payloads
  (`/providers`, `/provider`, `/health`, `/diagnostics`), persisted state, and
  metrics labels all keep today's values → byte-identical wire behavior and no
  learned-state loss.
- Setup wizard, config store, UI data layer (already id-driven) untouched.

---

## 4. Migration strategy

Additive, staged, suite-green at every step. No sync-behavior rewrite.

### P4.1a — `Provider.id` + registry-driven factory
1. Add `id: str = ""` + `Provider.identity()` to `app/providers/base.py`.
2. `build_provider()` sets `id=defn.id`.
3. Add `app/providers/factory.py::build_runtime_provider`; make the three
   factory modules delegate to it. Reconcile `openai` priority (decision D2).
4. `Relay._load_providers` iterates the registry (RUNTIME_READY-scoped).
5. New tests: identity fallback, factory parity vs legacy factories, registry
   build sets id, manager keying by identity.

### P4.1b — Id-keyed manager + client registry
6. `ProviderManager` keyed by `identity()`; `ClientRegistry` id-keyed with
   legacy name alias.
7. `chat_service` / `async_chat_service` / `health_checker` use
   `provider.identity()`.
8. Update `tests/test_reload.py` fake manager to resolve by id; add id/name
   resolution tests.

### P4.1c — Reload via registry + gate
9. Rebuild `_PROVIDER_SPECS`/`_RELOADABLE_FIELDS`/`_SECRET_FIELDS` from the
   registry (RUNTIME_READY-scoped); reload lookups by `spec["id"]`.
10. Full suite + `/admin/reload` behavior tests; secret scan; compile check.

### P4.1d — Docs
11. This plan → approved status; architecture/PROJECT_LOG note at phase
    boundary (per roadmap cross-cutting note), **not before approval**.

Backward compatibility summary: `.env` config is prefix-based and unchanged;
`ClientRegistry`/`ProviderManager` accept legacy names through the identity
fallback and alias map; API wire behavior and persisted state are untouched in
P4.1.

---

## 5. Files expected to change

### App (production)
| File | Change |
|---|---|
| `app/providers/base.py` | Add `id`, `identity()`; docs. |
| `app/providers/registry.py` | `build_provider` sets `id`; add `base_url_attr` (for P4.2); reconcile `openai` `runtime_priority` (D2); docstring "from P4" → P4.1. |
| `app/providers/factory.py` | **New** — `build_runtime_provider(defn)`. |
| `app/providers/nvidia.py` / `openai.py` / `lmstudio.py` | Thin wrappers delegating to factory. |
| `app/services/provider_manager.py` | Key by `identity()`. |
| `app/services/client_registry.py` | Id-keyed + legacy name alias; `get` resolves id→name. |
| `app/services/chat_service.py` | `registry.get(provider.identity())`. |
| `app/services/async_chat_service.py` | `registry.get(provider.identity())`. |
| `app/services/health_checker.py` | `registry.get(provider.identity())`. |
| `app/core/relay.py` | `_load_providers` iterates registry (RUNTIME_READY); factory import. |
| `app/services/reload.py` | Specs/reloadable/secrets derive from registry; lookups by id. |

Not touched: config.py, stores, API routers, decision/candidate layers, setup,
UI, PROJECT_LOG (until approval).

### Tests
| File | Change |
|---|---|
| `tests/test_reload.py` | Fake manager resolves by id; expected spec-derived sets. |
| `tests/test_provider_registry.py` | Assert `build_provider()` sets `id`; new factory-parity checks. |
| `tests/test_nvidia_provider.py` | Assert `id == "nvidia"` on `create_provider()`/`build_provider()`. |
| `tests/test_lmstudio_provider.py` | `ClientRegistry().get("lmstudio")` works (legacy `"LM Studio"` kept). |
| `tests/test_lmstudio_integration.py`, `tests/test_lmstudio_real.py` | Wrapper imports unchanged; assert identity. |
| New: `tests/test_provider_factory.py` | Registry-driven creation, key-gated discovery, priority reconciliation, id/name resolution. |

The ~18 suites that monkeypatch `ClientRegistry.get` (signature unchanged) are
**unaffected**.

---

## 6. Test strategy

1. **Full suite gate** at every sub-phase (baseline 1217 passed / 7 skipped /
   1 pre-existing TUI failure — must stay 1, not grow).
2. **Parity:** for identical settings/fakes, `build_runtime_provider` and the
   legacy `create_provider` factories produce identical `name`, `base_url`,
   `api_key`, `priority`, `models`, `priority_models`, `requires_api_key`
   (new test file).
3. **Identity resolution:** `ClientRegistry.get(id)`,
   `ClientRegistry.get(legacy_name)`, `ProviderManager.get` via
   id and via name fallback for hand-built providers.
4. **Reload parity:** `/admin/reload` applied/unchanged reports and
   enable/key/priority side effects are identical pre/post change
   (`test_admin_reload.py`, `test_reload.py`).
5. **API wire parity:** `/chat`, `/v1/chat/completions`, `/providers`,
   `/health` responses byte-identical (existing integration/UI suites).
6. **Boundary/compile:** `python -m compileall app tests`; secret scan clean;
   Textual boundary test green.

---

## 7. Risks

| # | Risk | Likelihood | Mitigation |
|---|---|---|---|
| 1 | `Provider.identity()` fallback masks a provider that forgot its id, silently keeping name-keying forever | Medium | `build_provider`/factory always set `id`; P4.1c adds a registry-integrity test asserting every runtime provider resolves by id; D1 makes `id` required in P4.2. |
| 2 | Priority reconciliation (openai 9↔5) changes ranking for anyone who noticed factory-5 vs registry-9 | Low | D2: align registry to current factory values; parity test pins it. |
| 3 | Reload specs rebuilt from registry change the reloadable field set and break `test_admin_reload` expectations | Medium | Keep RUNTIME_READY scoping in P4.1; run reload/admin suites at P4.1c before merging. |
| 4 | Legacy `get("LM Studio")` callers drift while alias exists | Low | Alias emits no warning now (quiet compat); D1 removes it in P4.2 with tests updated. |
| 5 | Generic factory subtly diverges from per-provider factories (lmstudio priority/base_url from settings) | Medium | lmstudio keeps settings-driven behavior; parity tests cover all three. |
| 6 | Store/API/persistence keys still name-based until P4.2 (display names not yet fully "UI-only") | Accepted | Explicitly scoped out; P4.2 is required before P4's "display names UI-only" exit is claimed. |
| 7 | `DEFAULT_PROVIDER` defaults to a name ("NVIDIA") | Low | Flagged; switch default to id in P4.2 when the field becomes used. |

---

## 8. Scope boundary & decision register

- **P4.1 (this plan):** runtime selection/wiring → stable ids
  (creation, manager, client registry, chat/health resolution, reload,
  load path). Backward-compatible with names via identity fallback + alias.
- **P4.2 (next sub-phase, not here):** migrate store keys (health/telemetry/
  quality/decision), attempts/`result["provider"]`, API payloads
  (`/providers`, `/provider`, `/health`, `/diagnostics`), persisted
  `relay_state.db` keys, metrics labels, `DEFAULT_PROVIDER`, and `Rankable.
  provider`/`choose_provider` to id; make `id` required and `name` display-only
  (or introduce `display_name`); optional one-time migration of persisted
  keys; complete the "display names are UI-only metadata" exit criterion.

Decisions (confirmed at plan approval):
- **D1.** `Provider.id` optional-with-fallback in P4.1; required in P4.2.
- **D2.** Registry `openai.runtime_priority` reconciled 9 → 5 to match the
  live factory; parity test pins all three current priorities (nvidia 10,
  openai 5, lmstudio settings-driven default 1).
- **D3.** Reload stays `RUNTIME_READY = {nvidia, openai, lmstudio}`-scoped in
  P4.1; the registry is the metadata source but the runtime set is unchanged.
- **D4.** `nvidia.py`/`openai.py`/`lmstudio.py` remain as thin wrappers (import
  compatibility); they are removed when P4.2 lands and nothing imports them.

---

## 9. Gate criteria (P4.1)

1. Full `pytest tests -q`: **1217 passed, 7 skipped, 1 pre-existing failure**
   (the TUI DuplicateKey case) — no new failures.
2. Factory parity tests green (legacy vs registry-driven identical output).
3. `/admin/reload` behavior and report parity green.
4. `/chat` and `/v1` wire behavior byte-identical (integration suites green).
5. `python -m compileall app tests` clean; secret scan clean; Textual
   boundary test green.
6. New providers in P4.2 require **no** edits to `Relay._load_providers`,
   `ClientRegistry`, or `reload` — only a registry entry.
