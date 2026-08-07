# Relay — Roadmap Verification Audit (P0–P8 vs. the original roadmap)

Status: **audit only — no code changed, no commit, `PROJECT_LOG.md`
untouched.**
Date: 2026-08-06
Scope: verify the completed P0–P8 phases against
`docs/platform-implementation-roadmap.md` (rev 2). P9 is **reserved** for a
future major feature (Project Continuity and Model Handoff Layer) and is not
started and not assessed here.

Evidence sources: roadmap, phase plans and completion reports under `docs/`,
git history (`0a682cf` = P8, `c893a27` = P0+P1), current repo state, current
test baseline (**2055 passed, 20 skipped**; RC offline suite 28 passed; live
smoke 6/6 vs NVIDIA, OpenAI blocked by quota).

Workflow status: Audit → Plan → Approval → Implementation → Tests → Commit
remains in effect. This document is the audit step; it makes no changes.

---

## Phase-by-phase verification

| Phase | Roadmap deliverable | Verified state | Verdict |
| --- | --- | --- | --- |
| P0 | Packaging: `pyproject.toml` + pinned deps, `[project.scripts] relay`, single version source, `requirements.txt`, installers, wheel smoke | `pyproject.toml` (setuptools, `relay = "app.cli:main"`, `app/__version__.py` single source), `requirements.txt` mirrors the 9 pins, `install.cmd`/`install.ps1`/`install.sh`, `tests/test_packaging.py` (25 passed), CI packaging job builds sdist/wheel + fresh-venv smoke | **Complete** (git+ URL install documented, not CI-testable — no public repo; see gaps) |
| P1 | CLI package, bare-`relay` wizard-or-TUI, setup wizard (per-provider, live key validation, progress bar, summary), subcommand list | `app/cli/`, bare `relay` runs wizard if unconfigured else TUI (`app/cli/__init__.py:241-245`), wizard with classified key validation and summary | **Complete** with deviations: subcommand list differs (see §2, D2) |
| P2 | Textual TUI: Chat, Model test, Providers, Keys, Model priority, Routing rules, Connected applications, System status, Configuration; setup→TUI handoff; `relay tui` | Screens: `dashboard`, `chat`, `models` (availability + priority controls), `providers`, `configuration` (routing/failover/settings), `applications`, `diagnostics`; `relay tui`; textual pilot tests in suite | **Complete** (Keys handled via CLI, Model test folded into Models/Chat — minor deviation) |
| P3 | Async hot path: async service + failover + streaming, endpoints `async def`, lock-safe stores | `app/services/chat_service.py` async failover, streaming generator, `/v1` + `/chat` async; `tests/test_async_*.py`, reliability matrix green | **Complete** |
| P4 | De-string-named provider system; async-first clients; add Anthropic/OpenRouter/Groq/Ollama/custom OpenAI-compatible; wire unused keys | Registry-driven wiring (`registry.py`), async clients (`test_async_provider_clients.py`), Anthropic + Gemini + Ollama enabled; LM Studio = local OpenAI-compatible | **Gap**: OpenRouter + Groq keys parsed but **not wired** (D1) |
| P5 | App keys (hash/salt/scopes/expiry/rotation/revoke), `relay keys`, admin API, store-backed auth, keyring for upstream keys, per-key correlation | `relay keys create/list/remove/test`, scoped keys, `RELAY_AUTH_STORE` tier + constant-time bootstrap, keyring-first upstream keys (`provider keys migrate`), admin `/admin/keys`; live-verified 401/403 | **Complete** |
| P6 | `platform.db` migrations; `api_keys`/`request_log`/`model_status`/`events`; 3-state availability; connected-apps; durable log + retention | `relay migrate`, `platform.db` schema doc, `relay apps`, `relay events`, `PERSISTENCE_RETENTION_DAYS`, privacy tests green | **Complete** |
| P7 | `relay config show/validate/reload/diff`, secret masking, TUI config panel, all config via commands | `relay config show/validate/diff/set/unset/reload`, masking (secrets masked), TUI Configuration panel, reload parity tests | **Complete** |
| P8 | Client guides (Cline/OpenCode/Continue + generic); pytest config; CI (lint/full suite/packaging/TUI boot) | Guides committed (`0a682cf`, verified live); CI = compileall + full suite (ubuntu 3.11/3.12, windows 3.12) + packaging job; no separate lint (optional per roadmap) | **Complete** |

---

## 1. Phases that are fully complete

**P0, P2, P3, P5, P6, P7, P8** are fully complete against the roadmap:
packaging and installers, the Textual TUI, the async hot path, API-key
security (store + keyring), the platform database and availability/usage
tracking, configuration management, and the client guides + quality gate/CI.
P1 is complete in substance with a recorded CLI-surface deviation.

## 2. Phases with remaining gaps

- **P4 — OpenRouter/Groq not wired.** The roadmap required wiring the unused
  parsed keys (`OPENROUTER_API_KEY`, `GROQ_API_KEY`). They remain parsed but
  unused (decision D1 of `docs/release-decisions.md`): either wire or drop.
  This is the only *phase-level* functional gap. Ancillary P4 doc staleness:
  `configuration.md:79` still lists `ANTHROPIC_API_KEY` as "parsed but
  unused" although Anthropic is a wired runtime provider.
- **P1 — CLI surface deviation (unrecorded until R1).** Roadmap named
  `status`/`providers`/`models`/`routing`/`logs`/`test`; shipped CLI is
  `setup`/`tui`/`serve`/`keys`/`provider keys`/`migrate`/`events`/`apps`/
  `config`. TUI + `events` cover the missing surfaces. Also the `app/cli.py`
  shim the roadmap asked for was implemented as the package `app/cli/__init__.py`
  directly (functionally equivalent — `python -m app.cli` works).
- **P2 — minor screen consolidation.** No dedicated TUI "Keys" or standalone
  "Model test" screen; keys are CLI-managed and Model test/priority are
  handled by the Models and Chat screens. Within the P2 exit criterion
  ("live dashboard with all panels; chat works"), this is an accepted
  consolidation.
- **P0 — one-command GitHub install not CI-verifiable.** Documented in README
  with a placeholder `<org>/<repo>`; wheel install smoke is CI-green, but the
  `pip install git+https://…` path cannot run in CI until the repo is public.

## 3. Deferred decisions (open, tracked in `docs/release-decisions.md`)

| # | Decision | Blocks |
| --- | --- | --- |
| D1 | OpenRouter/Groq wire or drop (P4 Decision H) | P4 gap closure, `.env.example`/docs consistency |
| D2 | P1 CLI deviation record | Release notes accuracy |
| D3 | Enable retry hardening in the deployed profile | Production profile |
| D4 | Pin model priorities for the deploying account | Production profile, telemetry cleanliness |
| D5 | License choice (blocked on D10) | `LICENSE`, packaging, public release |
| D6 | Deployed auth + persistence profile | Public exposure safety |
| D7 | Env-retirement audit execution (P6.3 follow-up) | Doc/code surface cleanliness |
| D8 | Baseline renumber (821 → 2055) + stale doc fixes | Doc alignment |
| D9 | Adopt the release-blocker list as the gate | Release readiness |
| D10 | Public release posture | D5, contribution policy, docs tone |
| W1 | Version/tag alignment (`v1.0.0` tag in same change as `0.1.0 → 1.0.0`) | Tagging |
| — | Known timing flake disposition (baseline-reproduced at `d344116`) | v1 gate |
| — | PyPI publishing decision | Distribution path |
| — | Plan/audit doc disposition (24 untracked docs) | Repo hygiene |

## 4. Technical debt before v1.0

1. **Provider shims** (`app/providers/nvidia.py`, `openai.py`, `lmstudio.py`)
   are deprecated-but-present; deletion explicitly deferred to a later
   cleanup release (P6.3 decision). Runtime no longer depends on them.
2. **Hand-maintained classification flags** in `config_spec.py`
   (editable/restart/live/secret); guarded by the P7.3 `MASKING_VIOLATIONS`
   check, but new fields can drift.
3. **Dead config surface:** `OPENROUTER_API_KEY`/`GROQ_API_KEY` parsed and
   advertised ("reserved") with no client — until D1 resolves it.
4. **Stale docs:** roadmap regression gate (821), `release-candidate-checklist.md`
   (1916/18 vs. actual 2055/20), `configuration.md:79` (Anthropic "unused").
5. **Known timing flake** (pre-existing, excluded from 2055/20) — must be
   dispositioned before the tag.
6. **Missing release artifacts** (not code debt): no `LICENSE`, no
   `CHANGELOG.md`, no `pyproject.toml` license metadata.
7. **OpenAI quota (B1)** — external environment blocker; gateway is
   NVIDIA-only in practice.

None of the code debt is *new* for v1.0; all is either already
baseline-accepted or mapped to R1 decisions / R2 cleanup.

## 5. Missing tests / documentation

Tests:
- No automated fresh-install E2E on a clean OS (manual step in the RC1
  release-candidate stage).
- No automated auth brute-force / abuse tests (adversarial review is manual
  at RC1; a minimal test is an optional R3 item).
- No load/scale testing beyond the documented single-process SQLite limit.
- No automated lint job in CI (roadmap marked ruff/mypy optional); CI covers
  compileall + full suite + packaging + TUI boot (via `test_ui_*`).

Documentation:
- No `CHANGELOG.md` / release notes (B6; R2 deliverable).
- `configuration.md:79` stale claim about Anthropic (see §2/§4).
- OpenRouter/Groq guidance exists only as "reserved" labels pending D1.
- Client guides (`docs/clients/`) are complete and live-verified.
- Everything else (deployment, security, rollback, troubleshooting, client
  setup) is present and consistent with shipped behavior.

## 6. Is Relay ready to enter release preparation?

**Yes — the codebase is roadmap-complete and ready to enter release
preparation now (Phase R1).** P0–P8 gate M5 is code-complete: full suite
2055/20 green, RC offline suite 28 green, CI green, live smoke 6/6 against
NVIDIA (OpenAI pending quota). The remaining items before the `v1.0.0` tag
are **decisions, artifacts, and verification** — not missing implementation —
with two caveats:

- **R1 decisions must be approved** (D1/D5/D10 require owner input; D2-D4,
  D6-D9 are proposed in `docs/release-decisions.md`).
- **B1 (OpenAI quota)** is external and should be fixed in parallel so live
  smoke can run at R3/RC1.

Relay is **not** ready to *tag* v1.0.0 yet (see §7), but it is ready to
begin the approved release-preparation workflow.

## 7. What must be fixed before tagging v1.0.0

1. **Resolve and approve all R1 decisions** (D1–D10), especially D1
   (OpenRouter/Groq), D5 (license), D10 (posture).
2. **Clear B1:** restore OpenAI billing/quota; live smoke 6/6 (including
   OpenAI).
3. **License (D5):** add `LICENSE`, `pyproject.toml` license metadata +
   classifiers, dependency license audit, contributor policy.
4. **Version alignment (W1):** bump `app/__version__.py` `0.1.0 → 1.0.0rc1`
   (R2) then `→ 1.0.0`, and land the `v1.0.0` tag in the same change as the
   final bump.
5. **Release artifacts (B6):** `CHANGELOG.md` + release notes; decide PyPI.
6. **D1 closure:** wire or drop OpenRouter/Groq; align `.env.example`,
   `configuration.md`.
7. **Production profile (D6/D3/D4):** `RELAY_API_KEY` + `RELAY_AUTH_STORE`,
   `PERSISTENCE_ENABLED=true` + path, retry hardening, pinned model
   priorities.
8. **RC1 stage:** tag `v1.0.0-rc.1`; clean-machine install (Windows/Linux),
   fresh user setup, upgrade/migrate test, adversarial security review.
9. **Quality gate on the tag:** full suite green with 0 new failures vs.
   2055/20, RC suite 28 green, CI green, known flake dispositioned.
10. **Doc alignment (D8/D7/D2):** renumber roadmap gate and RC checklist
    baseline, run the bounded env-retirement audit, record the P1 CLI
    deviation.
11. **Repo hygiene:** disposition the 24 untracked plan/audit docs; update
    `PROJECT_LOG.md` only at the release commit (per workflow).

---

## P9 — explicit boundary

P9 (Project Continuity and Model Handoff Layer) is **reserved** for a future
major feature. It is not part of the P0–P8 v1.0 gate, was not started, and
must not be initiated during release preparation. Any proposal would follow
the same Audit → Plan → Approval → Implementation → Tests → Commit workflow
after v1.0.0.

---

## Verdict summary

- Fully complete: **P0, P2, P3, P5, P6, P7, P8** (+ P1 in substance).
- Gap: **P4** (OpenRouter/Groq unwired → D1).
- Ready to enter release preparation: **yes**, pending R1 decision approval.
- Tagged only after §7 items 1–11 clear.
