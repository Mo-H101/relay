# P6.4 — Final hardening and release readiness (analysis)

Status: **proposed — analysis only. No code changes, no `PROJECT_LOG.md`
changes. Stops here for approval.**
Source: user directive (2026-08-06) after P6.3 cleanup (commit `aee18f2`).
Scope: full post-P6.3 architecture audit, classified into three tiers
(real release blockers / future improvements / optional cleanup), a security
re-check, a test-coverage and release-gates review, and explicit decisions on
shims, naming inconsistencies, and untracked plan docs.

No code changes, no commit, no `PROJECT_LOG.md` edits, no `tests/bench_nvidia_models.py`
or `tests/test_lmstudio_real.py` changes, plan docs stay untracked.

---

## 0. Post-P6.3 baseline (measured 2026-08-06)

- Full suite: **1916 passed, 18 skipped, 0 failed** (1934 collected).
- Collection: 1934 tests in 2.98s from 93 of 98 collectible test files;
  5 files are non-collectible by design (live-provider smoke suites and
  intentionally non-collectible helpers).
- Commit base: `aee18f2` (P6.3); prior `0474ce0` (P6.2), `0f49419` (P6.1).
- Pre-existing release evidence (all to be reconciled by this plan):
  - `docs/blockers-before-public-release.md` — OpenAI key quota is the only
    **hard blocker** (NVIDIA-only until restored). Unchanged by P6.1–P6.3.
  - `docs/v1.0.0-readiness-report.md` — pre-P6.1 (821 passed); open items:
    pin `openai` in dev deps, Linux CI for POSIX-only tests, `.env.example`
    surface gaps.
  - `docs/release-candidate-checklist.md` — pre-P6.1; needs re-run against
    post-P6.3 numbers.
  - `docs/known-limitations.md` — 429 `Retry-After` handling, streaming start
    failures; needs accuracy re-check.
  - `docs/rollback-procedure.md` — legacy files remain post-rollback; needs
    accuracy re-check.
  - `docs/platform-missing-components-report.md` — target-design reference
    for requirement #16 (connected applications) and config swap.

---

## 1. Release-readiness audit (post-P6.3)

### 1.1 Auth

- API-key auth on the gateway surface, hash-based (SHA-256 of the key), keys
  stored in `platform.db.api_keys` or via `RELAY_API_KEY` env; optional.
- `client_tracking` (middleware `app/api/middleware.py:119`) records only
  metadata (request counters, trimmed User-Agent, auth-scheme label); the
  `Authorization` header value is never stored.
- Dedicated test suites cover the admin/keys/audit surfaces and `/v1` surface
  parity.
- **Open verification item V1**: confirm whether auth endpoints are rate
  limited; if not, either add a limit or record it in `known-limitations.md`.
  (Not asserted as a blocker until verified.)

### 1.2 Key management

- Provider keys live in `.env` (plaintext) as the config surface; `relay
  provider keys set/import` writes to the OS keyring (SecretService / macOS
  keychain / Windows Credential Manager via `keyring==25.7.0`) when available.
- At-rest protection for provider secrets uses `secretbox`; the symmetric key
  is stored in the keyring when present, and is otherwise **generated per run
  and discarded** — meaning when the keyring is unavailable there is **no
  durable at-rest encryption** and stored secrets are effectively
  undecryptable on restart. See **B1**.
- Gateway keys are stored hashed only (correct by design); no plaintext keys
  in `platform.db`.

### 1.3 Registry / runtime loading

- `PROVIDER_REGISTRY` + `build_runtime_provider` (factory) build providers
  from definitions; `provider_manager` resolves stable ids with a legacy
  name-keyed fallback (by design, retained).
- `config_store` maps env → provider definitions; **config is still
  `.env`-only** — the provider-config swap into `platform.db` (master-plan
  P6.3 label) is still deferred. See **F1**.
- Provider clients exist for the chat hot path and for the setup-wizard
  surface (catalog listing + availability probes); the wizard and
  `app/providers/availability.py` share the redaction/dedupe helpers.

### 1.4 Persistence / `platform.db`

- Schema v5 (8 tables): `api_keys`, `learned_state`, `telemetry`,
  `telemetry_failures`, `quality_aggregates`, `decision_stats`,
  `model_status`, `events`. Versioned via `PRAGMA user_version` with replay of
  the legacy `relay_keys.db` (v1) schema.
- D6 guard: fresh `platform.db` creation is refused while legacy sources
  exist without `--state-dir` override.
- `availability.json` (`app/setup/persistence.py`) is **still written** by the
  setup wizard (`wizard.py:182`) and the UI re-scan path (`ui/data.py:596`);
  the P6.3 plan intended to retire writes. Deferred — see **F3**.
- No `providers`, `request_log`, or `apps` tables; the "connected
  applications" surface is the bounded in-memory `client_tracking`
  (200 entries) store. See **F2**.
- `docs/rollback-procedure.md` states legacy files remain after rollback;
  re-verify against the current migrate/rollback behavior.

### 1.5 Migrations / rollback

- `relay migrate` imports legacy P5 files (state db, `.env`, availability
  snapshot) into `platform.db`; events are never imported (D5); availability
  imports into `model_status`; `.env` is backed up.
- Rollback restores legacy files; `platform.db` remains as a side artifact.
  Re-verify the documented procedure still matches implementation.

### 1.6 CLI

- Present: `relay setup`, `relay serve`, `relay tui`, `relay keys`,
  `relay provider keys …`, `relay migrate`, `relay events`.
- Missing onboarding/DX commands from the master plan: `relay status`,
  `relay providers list`, `relay models`, `relay config`. See **F4**.

### 1.7 TUI

- Screens: dashboard, providers, models, chat, applications, events, etc.
- Reads the availability snapshot plus runtime health store; the applications
  screen is fed by in-memory `client_tracking` (non-durable). See **F2**.

### 1.8 Docs

- Encoding corruption (mojibake) in `README.md`, `pyproject.toml` description,
  and `.gitignore` comments. See **C1**.
- `.env.example` documents ~52 of ~98 parsed env vars (retry, scoring, health
  thresholds, task routing, proxy, keyring/auth-store, ops window are
  undocumented). See **B5**.
- Stale "until P6.3" / "P6 config swap" comments in `setup/persistence.py`
  and `services/client_tracking.py` docstrings. See **C3**.
- 15 untracked plan docs (P4.2 … P6.3). See **C4**.

---

## 2. Risk classification

### Tier 1 — Real release blockers (fix before public release)

- **B1 — Provider-secret durability without a keyring.** When the keyring is
  absent the `secretbox` master key is generated per run and discarded: stored
  provider keys become undecryptable, and the documented fallback is plaintext
  `.env`. Fix direction: persist the master key in a platform-managed file
  with locked-down permissions (or force keyring use), document the trade-off,
  and add tests covering both keyring-present and keyring-absent paths.
- **B2 — No CI.** POSIX-only tests (file-permission enforcement on state dir /
  key material, SecretService backend) never run on Windows dev machines, so
  they silently skip. Add a Linux CI job (install + full suite + packaging
  smoke). This is a hard release gate.
- **B3 — `openai` unpinned in `requirements-dev.txt`** (readiness-report
  required action, still open). Pin it.
- **B4 — Stale release evidence.** `v1.0.0-readiness-report.md` (pre-P6.1,
  821 passed) and `release-candidate-checklist.md` must be refreshed to
  post-P6.3 numbers and each checklist item re-verified or explicitly waived.
- **B5 — `.env.example` under-documents the config surface** (~52 of ~98).
  Sync documented vars with `config.py`; without this, operators cannot
  configure retry, scoring, health, task, proxy, keyring, and auth-store
  behavior.
- **B6 — Windows-permission limitation undocumented.** POSIX file-permission
  enforcement is untested on Windows; document the limitation in the release
  notes and rely on **B2** for coverage.

### Tier 2 — Future improvements (deferred, not blockers)

- **F1 — Provider-config swap:** `providers` table in `platform.db` +
  runtime `config_store` swap (master-plan P6.3 label; explicitly out of scope
  for P6.3, still open).
- **F2 — Connected applications:** labeled keys × `request_log` replacing the
  in-memory `client_tracking` (requirement #16 partial today).
- **F3 — `availability.json` write retirement:** persist solely via
  `model_status`; remove wizard/UI snapshot writes and the legacy import path.
- **F4 — Onboarding DX commands:** `relay status`, `relay providers list`,
  `relay models`, `relay config`.

### Tier 3 — Optional cleanup

- **C1 — Mojibake/encoding fixes** in `README.md`, `pyproject.toml`
  description, `.gitignore` comments.
- **C2 — Remaining naming inconsistencies** flagged by the audit (CLI
  help/wording, doc terminology); review list below in §5.2.
- **C3 — Stale "until P6.3 / P6" comments** in `setup/persistence.py`,
  `services/client_tracking.py`.
- **C4 — Decision on the 15 untracked plan docs** (§5.3).
- **C5 — `known-limitations.md` / `rollback-procedure.md` accuracy re-check**
  (429 `Retry-After`, streaming start, legacy files post-rollback) +
  optional addition of V1 if auth rate limiting is absent.
- **C6 — Stale `.gitignore` entries** (`relay_state.db`, `health.json` under
  "Stale operational artifacts") vs the actual `platform.db` layout.

---

## 3. Security re-check

- **Secret leakage:** `app/services/redaction.py` (deduped from
  `availability.safe_error_body`) covers provider errors; `client_tracking`
  stores metadata only; gateway keys are stored hashed. Re-verify redaction
  coverage across the `provider keys` CLI and the audit/events surface.
- **Redaction:** shared helper used by the wizard path, clients, and
  `health_checker`; confirm no error path emits raw `Authorization` or
  provider key material.
- **File permissions:** POSIX permission enforcement exists but only runs on
  Linux (**B2**); document the Windows gap (**B6**).
- **Key lifecycle:** set / list / delete / import / export via
  `relay provider keys`; rotation path exists; durability gap is **B1**.
- **Audit logging:** `events` table + events/audit surfaces have dedicated
  test suites; confirm auth-scheme labels are logged without values.
- **Failure behavior:** auth is fail-closed; health-feedback thresholds guard
  against flapping. Verify rate-limiting question (**V1**) before release.

---

## 4. Test coverage review and missing release gates

- **No test rewrites unless necessary.** Coverage is strong on the hot path:
  `/v1` surface parity, admin/keys/audit, provider conformance
  (`test_provider_conformance.py`, largest at 216), packaging, CLI sets.
- **Coverage gaps to add (only where they are release gates):**
  - POSIX file-permission tests run in CI (**B2**).
  - Keyring-absent durability behavior for provider secrets (**B1**).
  - Linux install smoke (`install.sh`) + packaging smoke in CI.
  - End-to-end `relay serve` smoke in CI.
- **Missing release gates:**
  1. Linux CI with full suite + packaging + install smoke (**B2**).
  2. `requirements-dev.txt` `openai` pinned (**B3**).
  3. Release checklist re-run with post-P6.3 numbers (**B4**).
  4. `python -m build` + fresh-venv install smoke (dev deps already include
     `build`/`setuptools`/`wheel`).
  5. Lint/type gate: no lint configuration exists (Ruff-style `BLE001`
     noqa’s appear by convention, 45 noqa across 19 files); decide whether to
     introduce a lint gate or defer. Verification item **V2**.

---

## 5. Decisions to confirm

### 5.1 Provider shims (P6.3 kept deprecated-but-not-deleted)

- **Recommendation: keep through v1.0.0.** No active caller depends on them
  (registry/factory paths are independent, proven by P6.3 tests); removal is
  churn with zero user-visible value before release. Add a follow-up cleanup
  item to delete them post-release. If preferred instead, remove them now —
  flag before implementation.

### 5.2 Remaining naming inconsistencies

- Audit flagged minor CLI help/wording and doc terminology mismatches
  (exact list to be enumerated at implementation time from `app/cli/` and the
  UI help strings). Recommendation: fix wording in the same pass as **C1/C3**
  only if zero behavior change; otherwise defer.

### 5.3 Untracked plan docs (15 files, P4.2 … P6.3)

- Recommendation: **keep untracked** (they are planning artifacts, not
  product docs), but run a one-line consistency check that referenced commits
  exist and referenced paths still resolve. Document this decision in the
  release notes if desired.

---

## 6. Proposed execution order (post-approval)

1. **B1** (keyring-absent durability) — design decision + tests.
2. **B2 + B3** (CI + dev-deps pin).
3. **B5 + B6** (`.env.example` sync, Windows limitation note).
4. **B4** (refresh readiness report + re-run checklist).
5. **Tier 3 batch** (C1, C3, C5, C6) — zero-behavior cleanup + docs.
6. **V1/V2** verification items; record outcomes.
7. Full suite + targeted suites; commit; update `PROJECT_LOG.md`.

Tier 2 (F1–F4) stays deferred and is tracked in the master plan.

---

## 7. Out of scope / non-goals

- Provider-config swap (**F1**), connected-applications persistence (**F2**),
  `availability.json` retirement (**F3**), DX CLI commands (**F4**).
- No API contract changes, no persistence/schema changes, no provider runtime
  behavior changes.
- No changes to `PROJECT_LOG.md`, `tests/bench_nvidia_models.py`, or
  `tests/test_lmstudio_real.py`.
- No new features beyond the hardening/release items above.
