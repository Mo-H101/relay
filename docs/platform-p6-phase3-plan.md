# P6.3 — Cleanup and technical debt

Status: **approved with scope adjustments (2026-08-06) — implementation in
progress, commit awaiting approval.**
Source: user directive (2026-08-06) after P6.2 security maturity (commit `0474ce0`).
Scope relationship: absorbs the **deferred cleanup items** from the master plan
`docs/platform-p6-plan.md` §4 (Focus 3) that were scheduled "P6.2" but did not
land (RC-test rewrite, provider-shim handling, dead settings), **plus**
compatibility/naming/doc debt identified by a post-P6.2 audit. The master
plan's **config swap** (provider config into `platform.db`, still labelled P6.3
there) is explicitly **out of scope** here and deferred (§5).

**Approved scope adjustments** (user directive, overriding the first draft):
1. Provider shims (`app/providers/nvidia.py`, `openai.py`, `lmstudio.py`) are
   **not deleted**. Audit imports/usages, add deprecation comments, remove any
   internal dependencies, and add tests proving the runtime registry paths do
   not depend on them. Deletion is deferred to a later cleanup release.
2. `RUNTIME_READY` is **not renamed** (no concrete benefit; minimize churn).
   No compatibility impact is introduced because nothing changes.
3. `test_rc_validation.py` is **repaired** through the registry/manager pattern;
   no coverage is deleted.
4. Continue with: env retirement audit, shared helper extraction **only where
   duplication is real**, CLI naming consistency, docs terminology cleanup.
5. Constraints: no API contract changes, no persistence/schema changes, no
   `PROJECT_LOG.md` changes, no provider runtime behavior changes.

No code changes, no commit, no `PROJECT_LOG.md` edits beyond what is listed here.

---

## 0. Post-P6.2 baseline (measured 2026-08-06)

- Full suite: **1887 passed, 18 skipped, 28 failed** — the 28 failures are all
  `tests/test_rc_validation.py` (a known pre-existing caveat, unchanged since
  P4.1).
- Targeted suites: `test_packaging.py` 25 passed; CLI set
  (`test_key_cli.py`, `test_provider_factory.py`, `test_provider_migrate.py`)
  77 passed; provider set (`test_nvidia_provider.py`,
  `test_lmstudio_provider.py`, `test_lmstudio_integration.py`) 68 passed.
- Commit base: `0474ce0` (P6.2).

---

## 1. Test debt — the 28 `test_rc_validation.py` failures

### 1.1 Root cause (verified)

All 28 tests fail **inside the shared `prod_components` fixture at
`test_rc_validation.py:210`**, before any assertion runs:

```
monkeypatch.setattr(relay_module, "create_nvidia_provider", nvidia_factory)
E   AttributeError: module 'app.core.relay' has no attribute 'create_nvidia_provider'
```

The names `create_nvidia_provider` / `create_openai_provider` were imported
aliases in `app/core/relay.py` that the **P4.1 registry refactor
(commit `d2c5f8c`)** removed when `Relay._load_providers()` switched to
`build_runtime_provider(PROVIDER_REGISTRY[...])`. The suite has run **zero live
assertions** since then.

### 1.2 What the suite actually targets

- Real `app.main` FastAPI app via `TestClient` and `openai.AsyncOpenAI` +
  `httpx.ASGITransport`.
- Real `app.core.relay.Relay` (same class every passing test uses).
- Scripted loopback upstreams (`MockOpenAIProvider` from
  `tests/conformance_helpers.py`) standing in for NVIDIA/OpenAI.
- The nine `app.api.*` modules it wires the `relay` singleton into
  (`admin, chat, decision, diagnostics, feedback, health, metrics, openai,
  providers`) — all still exist and hold the singleton today.

It does **not** target a removed entrypoint (`app.core.server`, `app.cli`, or
`python -m app` all unused by it). Only the **provider-injection mechanism in
the fixture** is stale.

### 1.3 Equivalent passing coverage (the safety net)

Every behavior the suite claims to validate is covered green elsewhere:

| rc class | Equivalent passing tests |
| --- | --- |
| ProductionGatewayWorkflow | `test_openai_sdk_compat.py`, `test_openai_api.py`, `test_openai_conformance.py` |
| NativeChatWorkflow | `test_api_integration.py` (`TestChatSuccess`/`TestChatFailure`), `test_openai_api.py::TestRegressionChatEndpoint` |
| ReliabilityMatrix | `test_retry_hardening.py`, `test_chat_service.py`, `test_async_chat_service.py`, `test_cross_provider.py` |
| RoutingIntelligence | `test_health_store.py`, `test_adaptive_routing.py`, `test_full_stack.py`, `test_quality_feedback.py`, `test_diagnostics.py`, `test_persistence_integration.py` |

Measured green: `test_openai_sdk_compat + test_retry_hardening +
test_cross_provider + test_quality_feedback` = 86 passed;
`test_openai_api + test_full_stack + test_api_integration + test_diagnostics +
test_health_store + test_persistence_integration` = 112 passed.

### 1.4 Decision D1 — repair, not remove or replace

**Repair the fixture** (~20 lines in one fixture). Rationale:

- The suite is the only **production-profile end-to-end gate**: real `app.main`
  + real wire clients + loopback mocks. Equivalent coverage exists but is
  fragmented across many files at lower fidelity.
- The repo's own plan (`docs/platform-p6-plan.md` §4.4) mandates repair
  ("must be repaired, not deleted").
- A fixture rewrite is mechanical and already proven by
  `test_openai_sdk_compat.py`: patch `Relay._load_providers` to a no-op (the
  registry build is driven by real settings, which the profile overrides with
  loopback mocks), construct `Relay()`, then register the two scripted
  providers via `relay_obj.provider_manager.register(provider)`.

**Work items**

- W1: Rewrite `prod_components` in `tests/test_rc_validation.py` to the
  registry/manager pattern; delete the two `setattr(... create_*_provider ...)`
  calls; patch `Relay._load_providers` to a no-op; register the NVIDIA/OpenAI
  mock providers directly.
- W2: Keep all 28 test bodies and `rc_client`/`_sdk_client` helpers unchanged.
- W3: Fix the one stale fixture reference `tmp_path / "relay_state.db"` in the
  persistence fixture (`test_rc_validation.py:171`) to
  `str(tmp_path / "platform.db")`.

**Exit condition:** all 28 rc tests pass; full suite at 0 failures.

---

## 2. Legacy cleanup — provider shims (deprecate, do NOT delete)

### 2.1 Findings (verified by grep)

`app/providers/nvidia.py`, `app/providers/openai.py`, `app/providers/lmstudio.py`
are structurally identical **one-line facades**:

```python
def create_provider() -> Provider:
    return build_runtime_provider(PROVIDER_REGISTRY["<id>"])
```

- **Zero `app/` imports** — nothing in the runtime references them.
- `registry.py` and `factory.py` are fully self-contained on the `*_client.py`
  modules; the shims appear nowhere in `app/`.
- Consumers are **5 test files only**: `test_nvidia_provider.py:18`,
  `test_lmstudio_provider.py:6`, `test_lmstudio_integration.py:25`,
  `test_lmstudio_real.py:21`, `test_provider_factory.py:18-20`.
- `test_provider_factory.py:52-60` already asserts the wrapper/registry
  equivalence, so the shims add nothing functionally.
- No internal dependencies exist in the shims beyond `build_runtime_provider`
  and `PROVIDER_REGISTRY` (both runtime-owned) — nothing to remove.

### 2.2 Decision D2 — deprecate + prove independence; deletion deferred

Per approved adjustment #1:
- Add a `# DEPRECATED` module header to each shim stating the registry is the
  single source of truth, that no runtime code imports them, and that they are
  slated for removal in a later cleanup release.
- **No deletion.** `tests/bench_nvidia_models.py` and
  `tests/test_lmstudio_real.py` keep their existing imports untouched
  (standing constraint: never touch bench/live tests).
- Add a test in `tests/test_provider_factory.py` proving the **runtime registry
  paths do not depend on the shims**: the registry's `client_class` resolves to
  the `*_client` modules (never the shims), the registry+factory source never
  references the shim imports, and a runtime `Provider` builds directly from a
  `ProviderDefinition` without any shim.

**Work items**

- W4: Deprecation headers on the three shims.
- W5: New independence test in `tests/test_provider_factory.py`.
- W6: No factory/registry code changes (docstrings already accurate).

---

## 3. Compatibility cleanup

### 3.1 Environment variables

Findings (all verified by grep in `app/`):

| Variable | Status | Evidence |
| --- | --- | --- |
| `GOOGLE_API_KEY` | **Dead** — no code reads it; live var is `GEMINI_API_KEY` | only `docs/v1.0.0-readiness-report.md:67` (historical record) |
| `DEFAULT_PROVIDER` | Parsed-but-unused (documented + UI-tested as informational) | `config.py:358`, `ui/data.py:257`, `configuration.md:120` |
| `OPENROUTER_API_KEY`, `GROQ_API_KEY` | Parsed-but-unused (reserved providers) | `config.py:297,319` |
| `OLLAMA_BASE_URL` | **Functional** — do not touch | `config.py:340`, `registry.py:178` |
| `RELAY_AUTH_STORE`, `RELAY_DATA_DIR`, `RELAY_KEYRING`, `RELAY_KEYRING_BACKEND`, `PERSISTENCE_ENABLED`, `PERSISTENCE_PATH` | **Functional** — do not touch | §0 audit |

**Decision D3 — dead settings (master-plan Decision H):**
- **`GOOGLE_API_KEY`**: already absent from code; the readiness report records
  its removal. No code change; documented in the compat audit only.
- **`DEFAULT_PROVIDER`**: remove the parsing block in `config.py`, the
  informational row in `app/ui/data.py` (`_CONFIG_ROWS`), the
  `ServiceFacade.default_provider()` read of `settings.default_provider`
  (replace with the runtime top-priority provider so the dashboard tile keeps
  showing truthful info), and the stale doc references
  (`docs/configuration.md:120`, `docs/tui-guide.md:89`, `.env.example:56`).
  Update `test_ui_configuration.py` accordingly (drop the informational
  assertions; the save-reject coverage moves to a real unknown/secret env).
- **OpenRouter/Groq:** keep parsing (providers are reserved, roadmap P4);
  `docs/configuration.md:79` already documents them as reserved-and-parsed.
  No code change; **no-op**.

### 3.2 Temporary migration guards

The **D6 legacy-source guard** (`app/services/platform_store.py:187-226` +
mirror in `app/cli/migrate.py:105-119`) is **load-bearing, not temporary**:

- It fails closed (`PlatformStoreError("legacy state detected - run `relay
  migrate` first")`) so a fresh `platform.db` is never created over unmigrated
  legacy stores.
- `relay migrate` copies sources and **never deletes them** (rollback
  contract), so the guard must stay for the whole supported-lifetime of
  pre-P6.1 layouts.
- `RELAY_DATA_DIR`/`PERSISTENCE_PATH` bypass (:214-215) is the documented
  downgrade/compat knob (master plan §7.1).

**Decision D4 — keep.** No code change. Add one doc line to
`docs/configuration.md` stating the guard is permanent for the lifetime of the
legacy import path. Keep `relay_state.db`/`relay_keys.db` references **only**
where they describe legacy migration sources.

### 3.3 Duplicate code paths (only where duplication is real)

Findings (verified):

| Duplicate | Location | Disposition |
| --- | --- | --- |
| `_safe_provider_body` == `safe_error_body` (identical logic: key-strip + control-char filter + 200-char truncate) | `openai_compat_client.py:21-48` vs `providers/availability.py:48-77` | **D5**: consolidate to one function in `app/services/redaction.py` (the single redaction layer named by `docs/security.md:95-96`). Both public helpers become thin delegators so the ~55 existing call sites stay untouched. |
| `_text_content` byte-identical | `anthropic_client.py:35-51` and `gemini_client.py:38-52` | **D6**: hoist to a shared helper in `app/providers/openai_compat_client.py` (already the cross-client helper hub), imported by both clients; public call sites identical. |
| `_terminal_before` mirrors `KeyStore.prune` predicate, and diverges at the expiry boundary (`<` vs SQL `<=`) | `cli/keys.py:432-446` vs `services/key_store.py:276-300` | **D7**: add `KeyStore.list_terminal(cutoff)` matching the prune SQL exactly; CLI dry-run uses it. Fixes the boundary drift (`test_prune_cutoff_at_boundary` proves `<=`). |
| `RUNTIME_READY` manual id-set | `registry.py:200-206` | **D9**: **no change** per approved adjustment #2 (no concrete benefit; minimize churn). |
| Scope vocabulary `_VALID_SCOPES` | `api/keys.py:28` | **D8**: **dropped** (not in the approved continue-list; minimal-churn preference; no real duplication — defined once). |
| CLI exit-code split | `provider_keys.py:125-134` `_provider_or_fail` exits 1; usage errors exit 2 | **D10**: audit only — unknown provider id is an **operational** failure (1) and stays; every `parser.error` path is genuinely usage (2). No broad churn. |

Per-store guarded connections (`KeyStore`/`StateStore`/`EventLog` each own a
WAL connection over `platform.db`) are the **documented D2 decision** — not
duplication; unchanged.

### 3.4 Stale documentation

Stale-doc sweep (find/replace + copy review). Source of truth: the naming
canon in §4. All edits are doc-only.

| File | Fix |
| --- | --- |
| `docs/configuration.md:95,107,231,276` | `relay_keys.db`/`relay_state.db` → `state_dir/platform.db` (as the live store) |
| `docs/deployment.md:51,105,137,268` | same; keep 268's migration-history framing |
| `docs/security.md:13,19,25,58` | live-store name → `platform.db` |
| `README.md:154,176-177` | `PERSISTENCE_PATH=./relay_state.db` → `state_dir/platform.db` |
| `docs/blockers-before-public-release.md:82` | path example → `platform.db` |
| `docs/ux-validation-guide.md:666-791` | persistence validation steps assert `relay_state.db` at project root (pre-P6.1 behavior) → assert `state_dir/platform.db` |
| `.env.example:80-81` | `RELAY_AUTH_STORE` comment `relay_keys.db` → `platform.db` |
| `.env.example` | drop `DEFAULT_PROVIDER` (D3) |
| `docs/configuration.md:37` | drop "the **future** platform database" ("future" is stale) |
| `docs/configuration.md` | note the D6 legacy guard is permanent for the legacy-import lifetime (D4) |
| `docs/platform-db-schema.md:112,115` | `--data-dir` → `--state-dir` (alias kept) |
| `.gitignore:8` | keep `relay_state.db` entry (still protective for source-checkout legacy files) — no change |

Historical phase-plan docs (`platform-p1/p2d/p3/p5-*.md`) and the P6 master
plan are **left as-is** (they describe the state at their time; the master plan
is the design record).

### 3.5 CLI alias and grammar

- `relay keys provider migrate` alias exists (`cli/keys.py:226-232`); canonical
  is `relay provider keys migrate`. **Decision J: keep** the alias as a
  documented convenience (deployment.md already documents both) — do not drop;
  do not widen it.
- **D11** (naming consistency): `relay migrate --data-dir` → canonical
  `--state-dir` with `--data-dir` kept as a hidden alias and the internal
  `dest="data_dir"` unchanged, so `args.data_dir`, `_resolve_layout`, and every
  existing `--data-dir` invocation keep working. Zero script breakage.

---

## 4. Naming consistency

### 4.1 Canonical set (already the code truth)

| Concept | Canonical |
| --- | --- |
| Consolidated SQLite database | `platform.db` (at `state_dir/platform.db`) |
| State directory (module attr / path) | `state_dir`; default dir name `.relay` (source checkout) |
| Env override for state dir | `RELAY_STATE_DIR` |
| Env override for installed user-data dir | `RELAY_DATA_DIR` |
| Persistence path override | `PERSISTENCE_PATH` (defaults to `state_dir/platform.db`) |
| Legacy DB filenames (read-only, migrate + D6 guard only) | `relay_keys.db`, `relay_state.db` |
| Retired name | `relay.db` (never shipped; 16 doc-only mentions, all historical) |

### 4.2 Inconsistencies to fix

- **Docs lag** (covered by §3.4 sweep): README/configuration/deployment/
  blockers/validation-guide still present `relay_state.db` as the default.
- **`relay.db`**: purge remaining references from the P6 master/phase1 plan
  **only where they claim current naming**; leave the historical P1-P3
  planning docs alone.
- **CLI flag vs config attr**: `relay migrate --data-dir` → `--state-dir`
  (alias retained, `dest` unchanged) — see D11.
- **Terminology in one place**: settle on "state directory" in user-facing
  text; "data directory" remains only as the platformdirs internal concept
  (`config.py` docstrings). Update `cli/migrate.py` help and
  `docs/security.md:66` wording.
- Generated `relay.egg-info/PKG-INFO` is stale but regenerated on build —
  no source change.

---

## 5. Scope control

### 5.1 Files to change

**Implementation (`app/`)**
| File | Change |
| --- | --- |
| `app/providers/nvidia.py`, `openai.py`, `lmstudio.py` | **deprecation headers only** (D2); no deletion |
| `app/services/redaction.py` | host the consolidated provider-error helper (D5) |
| `app/providers/availability.py` | `safe_error_body` delegates to redaction helper (D5) |
| `app/providers/openai_compat_client.py` | `_safe_provider_body` → shared helper (D5); host shared `_text_content` (D6) |
| `app/providers/anthropic_client.py`, `gemini_client.py` | import `_text_content` from openai_compat_client, drop local copies (D6) |
| `app/services/key_store.py` | add `list_terminal(cutoff)` (D7) |
| `app/cli/keys.py` | dry-run uses `store.list_terminal`; drop `_terminal_before` (D7) |
| `app/core/config.py` | remove `DEFAULT_PROVIDER` parsing (D3) |
| `app/ui/data.py` | drop the `DEFAULT_PROVIDER` row; `default_provider()` reads runtime top provider (D3) |
| `app/ui/screens/dashboard.py` | tile label "Default provider" → "Preferred provider" (D3 accuracy) |
| `app/cli/migrate.py` | `--state-dir` canonical + `--data-dir` alias; help wording (D11) |
| `app/setup/persistence.py` | comment `--data-dir` → `--state-dir` (D11) |

**Tests (`tests/`)**
| File | Change |
| --- | --- |
| `test_rc_validation.py` | fixture rewrite (W1-W3); persistence path → `platform.db` |
| `test_provider_factory.py` | new runtime-independence test (W5) |
| `test_key_prune.py` | locked-store test patches `list_terminal` (D7) |
| `test_ui_configuration.py` | drop `DEFAULT_PROVIDER` assertions; save-reject coverage on a real unknown/secret env (D3) |

**Docs / config**
| File | Change |
| --- | --- |
| `docs/configuration.md`, `docs/deployment.md`, `docs/security.md`, `README.md`, `docs/blockers-before-public-release.md`, `docs/ux-validation-guide.md`, `.env.example`, `docs/tui-guide.md`, `docs/platform-db-schema.md` | naming + env-var sweep (§3.4) |

### 5.2 Files untouched

- **Hot path / routing**: `chat_service.py`, `async_chat_service.py`,
  `routing.py`, `scoring.py`, `decision_engine.py`, `candidate_builder.py`,
  `health_checker.py`, `health_refresher.py`, `health_store.py`.
- **Provider wire contract**: all `*_client.py` behavior,
  `openai_compat_client` shared helpers (only `_safe_provider_body` body and
  the new `_text_content` are touched; wire surfaces unchanged), `base.py`,
  `exceptions.py`, `factory.build_runtime_provider` semantics,
  `providers/registry.py` (no field/set changes).
- **API wire behavior**: `app/api/chat.py`, `openai.py`, `feedback.py`,
  `decision.py`, `metrics.py`, `health.py`, `diagnostics.py`; existing
  `app/api/keys.py` responses and `app/api/admin.py` events.
- **Auth contract**: `app/security/auth.py` (no behavior change).
- **Persistence/schema**: `platform_store.py` (schema stays v5; the D6 guard
  and migration path stay), `state_store.py`, `state_flusher.py`, `event_log.py`.
- **CLI surfaces that shipped in P6.2**: `relay events`, `relay keys
  rotate/prune` (behavior unchanged; only the dry-run source is deduped),
  `relay migrate` behavior (only flag alias + help wording), provider-key
  commands.
- **TUI screens** (aside from the single dashboard tile label),
  `app/core/server.py`, `app/main.py`.
- `PROJECT_LOG.md` — never modified (standing instruction).

### 5.3 Out of scope (deferred)

- **Config swap** (provider config into `platform.db`; the master plan's own
  "P6.3"): explicitly not this phase. It is a behavior change with its own
  migration/rollback plan, not a cleanup. The `.env` mirror and
  `config_store`/`reload` implications stay as documented in
  `docs/platform-p6-plan.md` §2.5/§7.
- `availability.json` retirement / `model_status`-only (master-plan P6.3 item):
  deferred with the config swap.
- `client_tracking` → `apps` view, `relay apps` (master-plan P6.4): deferred.
- Provider onboarding DX (registry conformance doc, `relay providers list`,
  `relay models`): master-plan §5.1, deferred unless trivial.
- **Shim deletion** and **`RUNTIME_READY` restructuring**: deferred by the
  approved scope adjustments.

### 5.4 Migration impact

- **None to stored data.** No schema version bump (stays v5), no table
  changes, no migration step. All changes are code/doc/test-level.
- Provider loading behavior is unchanged: the rc tests patch
  `Relay._load_providers` off in tests only; production wiring is untouched.
- `DEFAULT_PROVIDER` is informational-only: removing it changes no runtime
  behavior; the dashboard tile now shows the runtime top-priority provider.
- `--data-dir` remains accepted (alias), so existing scripts keep working.

### 5.5 Rollback strategy

- Each change is small and independently revertible:
  - **RC fixture**: revert restores the two `setattr` lines; test-only.
  - **Shims**: deprecation comments are inert; revert is a comment delete.
  - **Dedupe helpers** (D5-D7): revert restores the local definitions; the
    shared helper is behavior-identical (covered by existing tests). The
    `list_terminal` boundary alignment is a dry-run-only fix.
  - **`DEFAULT_PROVIDER`**: revert restores parsing + UI row; pure additive
    informational field.
  - **Docs/naming**: doc-only, revert is a find/replace back.
- No irreversible action: nothing deletes data, keys, shims, or legacy files
  (`relay migrate` is untouched; shims are not deleted).
- Full suite re-run after each work item; the phase cannot pass the gate with
  any new failure.

---

## 6. Testing / cleanup verification

Verification commands and expected results (baseline in §0):

| Scope | Command | Gate |
| --- | --- | --- |
| rc_validation (the 28) | `pytest tests/test_rc_validation.py -q` | **28 passed, 0 failed** |
| Full suite | `pytest -q` | **0 failures** (reviewed skips only) — this is the master-plan §8 regression gate |
| Packaging | `pytest tests/test_packaging.py -q` | 25 passed (no shim refs) |
| CLI | `pytest tests/test_key_cli.py tests/test_key_prune.py tests/test_key_rotate.py tests/test_provider_keys.py tests/test_provider_factory.py tests/test_provider_migrate.py tests/test_migrate.py -q` | all passed; add `--state-dir` + alias both work |
| Provider | `pytest tests/test_nvidia_provider.py tests/test_lmstudio_provider.py tests/test_lmstudio_integration.py tests/test_lmstudio_real.py tests/test_provider_registry.py tests/test_provider_conformance.py tests/test_openai_conformance.py tests/test_async_provider_clients.py -q` | all passed |
| Redaction/error-body dedupe | `pytest tests/test_redaction.py tests/test_openai_compat_client.py tests/test_security_hardening.py -q` | all passed; new shared-helper contract covered by existing tests |
| Conformance | `pytest tests/test_provider_conformance.py -q` | passed (master-plan gate) |

Additional checks:
- `grep -rn "create_nvidia_provider\|create_openai_provider\|create_provider" tests/` → only the known 5 test-file consumers remain (shims intact, no new imports).
- `grep -rn "relay.db" docs/` → only historical planning docs remain.
- Doc sweep: `grep -rn "relay_state.db" docs/ README.md .env.example` → only
  migration-source/history framing remains.
- No `DEFAULT_PROVIDER`/`GOOGLE_API_KEY` references outside historical docs.

---

## 7. Decisions requested

| # | Decision | Status |
| --- | --- | --- |
| D1 | RC-test debt | **repair** the fixture (rewrite `prod_components`); keep all 28 tests |
| D2 | Provider shims | **deprecate + prove independence; deletion deferred** (adjustment #1) |
| D3 | Dead settings | remove `GOOGLE_API_KEY` (already absent) + `DEFAULT_PROVIDER`; keep OpenRouter/Groq parsing as reserved |
| D4 | D6 migration guard | **keep** (load-bearing); document as permanent for the legacy-import lifetime |
| D5 | Error-body helper | consolidate `_safe_provider_body`/`safe_error_body` into `app/services/redaction.py` |
| D6 | `_text_content` | hoist shared helper for anthropic/gemini clients |
| D7 | Prune predicate | add `KeyStore.list_terminal(cutoff)`; CLI dry-run reuses it (fixes boundary drift) |
| D8 | Scope vocabulary | **dropped** (not in approved continue-list) |
| D9 | `RUNTIME_READY` | **no change** (adjustment #2: no concrete benefit, minimize churn) |
| D10 | CLI exit codes | audit only; unknown provider stays exit 1; no broad churn |
| D11 | `--data-dir` | `--state-dir` canonical + hidden alias; `dest` unchanged |
| J | `relay keys provider` alias | keep as documented convenience |

## 8. Exit gate

1. `tests/test_rc_validation.py` 28/28 green; full suite **0 failures**.
2. Shims carry deprecation headers, are not deleted, and are proven
   independent of the runtime registry path by a new test.
3. `RUNTIME_READY` and the registry are unchanged (no rename).
4. Naming sweep complete; only historical/migration-source `relay_state.db`
   and `relay.db` references remain in docs.
5. No schema change (`SCHEMA_VERSION == 5`); no data migration; legacy D6 guard
   intact.
6. Packaging, CLI, provider, and conformance suites green.
7. `docs/configuration.md`, `docs/deployment.md`, `docs/security.md`,
   `README.md`, `.env.example`, `docs/tui-guide.md` updated; `PROJECT_LOG.md`
   untouched.
