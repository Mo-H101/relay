# Relay — Release Decisions (Phase R1)

Status: **DECIDED — approved 2026-08-07 (D1/D5/D10 answered; D2–D4, D6–D9
confirmed). R2 execution is in progress; `PROJECT_LOG.md` untouched,
`LICENSE`/`CHANGELOG.md` now created.**
Date: 2026-08-06
Phase: R1 of `docs/v1-release-readiness-plan.md` (workflow: Audit → Plan →
Approval → Implementation → Tests → Commit).

This document records the decisions required before the `v1.0.0` gate. Each
entry states the current state, the options considered, the recommendation
(where one is appropriate), a **final decision** field, and the impact on
v1.0.0. Nothing here is implemented.

Decision numbering follows the R1 directive list. Two readiness-plan items are
folded elsewhere: the plan's D6 (PyPI publishing) and D9 (plan-doc
disposition) are captured under **D9** below as release actions.

---

## D1 — OpenRouter/Groq wire-or-drop decision

- **Current state:** `OPENROUTER_API_KEY` (`app/core/config.py:294`) and
  `GROQ_API_KEY` (`app/core/config.py:316`) are parsed and listed as secret
  restart fields in `config_spec.py:408/423` and as "reserved" in
  `.env.example:125-138`. **No client or registry entry exists** — the runtime
  registry wires exactly six providers: NVIDIA, OpenAI, Anthropic, Google
  Gemini, LM Studio, Ollama (`app/providers/registry.py:80-170`).
  `configuration.md:79` labels these keys "Reserved for future providers;
  parsed but unused." This is the only dead config surface remaining from P4
  Decision H.
- **Options considered:**
  - (a) Wire both providers — add registry `ProviderDefinition`s and
    OpenAI-compatible clients (OpenRouter and Groq both expose
    OpenAI-compatible endpoints), plus tests.
  - (b) Drop the reserved keys — remove `OPENROUTER_API_KEY`/`GROQ_API_KEY`
    from `config.py`, `config_spec.py`, and `.env.example`; re-audit
    `configuration.md`.
  - (c) Keep as-is (dead config surface) — rejected: the post-P7 audit and the
    alignment audit both call for wire-or-drop, and a shipped "reserved" key
    is misleading to operators.
- **Recommendation:** **(b) drop for v1.0.0**, defer wiring to post-v1.
  Rationale: no production key exists for either provider, the OpenAI-compat
  surface is already validated through NVIDIA/OpenAI/LM Studio, and removing
  dead config shrinks the release surface. Wiring is additive and can land
  post-v1 without a migration.
- **Final decision:** **APPROVED — drop (b).** The `OPENROUTER_API_KEY` /
  `GROQ_API_KEY` reserved keys are removed from `config.py`,
  `config_spec.py`, and `.env.example` in R2; `configuration.md` is
  re-audited. Wiring is deferred post-v1.
- **Impact on v1.0.0:** if (b), `config.py`/`config_spec.py`/`.env.example`
  lose two secret fields in R2 and `.env` compat is unaffected (unknown vars
  are ignored). If (a), R2 adds providers + tests and the acceptance gate
  grows accordingly.

---

## D2 — P1 CLI deviation record

- **Current state:** The roadmap P1 subcommand list
  (`docs/platform-implementation-roadmap.md:49-50`) named `serve`, `status`,
  `providers`, `models`, `keys`, `routing`, `apps`, `config`, `logs`, `test`.
  The shipped CLI (`app/cli/__init__.py:131-207`) is `setup`, `tui`, `serve`,
  `keys`, `provider keys`, `migrate`, `events`, `apps`, `config`. Missing vs.
  the roadmap: `status`, `providers`, `models`, `routing`, `logs`, `test`.
  The TUI provides the status/models/providers/routing surfaces; `events`
  provides log tailing.
- **Options considered:**
  - (a) Record as an **intentional deviation** in the release notes (TUI +
    `events` supersede the listed subcommands). No code change.
  - (b) Implement the missing subcommands — rejected: duplicates TUI
    surfaces, expands the v1 surface, and no operator workflow needs them.
  - (c) Leave unrecorded — rejected: the post-P7 audit explicitly flags this
    as unrecorded deviation.
- **Recommendation:** (a). Document in the v1.0.0 release notes under a
  "Command-line interface" note stating the TUI supersedes the originally
  planned `status`/`providers`/`models`/`routing`/`logs`/`test` commands and
  that `relay events` is the log surface.
- **Final decision:** **CONFIRMED** — record as intentional deviation (a).
  `[x]`
- **Impact on v1.0.0:** docs/release-notes only; zero code or test impact.

---

## D3 — Retry-hardening decision

- **Current state:** 429 retries are **immediate by default**;
  `RETRY_HONOR_RETRY_AFTER` defaults to `false`, `RETRY_BACKOFF_BASE_SECONDS`
  to `0`, `RETRY_BACKOFF_MAX_SECONDS` to `60`,
  `REQUEST_TIMEOUT_BUDGET_SECONDS` to `0` (unlimited)
  (`app/core/config.py:206-222`, `config_spec.py:176-184`). Behavior is
  tested (`tests/test_retry_hardening.py`) and documented
  (`known-limitations.md` §1, `blockers-before-public-release.md` §3). The
  only open item is the **deployed profile** setting.
- **Options considered:**
  - (a) Enable `RETRY_HONOR_RETRY_AFTER=true` in the production profile
    (optionally with `RETRY_BACKOFF_BASE_SECONDS` and a
    `REQUEST_TIMEOUT_BUDGET_SECONDS`).
  - (b) Keep the immediate-retry defaults in production.
- **Recommendation:** (a) — enable `RETRY_HONOR_RETRY_AFTER=true` in the
  deployed profile (with a bounded backoff, e.g. base 1–2s, and an explicit
  request budget) to avoid retry-slot thrash under sustained 429s. Defaults
  stay immediate for casual use; this is profile-only, no code change.
- **Final decision:** **CONFIRMED** — enable in the production profile (a),
  exact backoff/budget values set at deploy time. `[x]`
- **Impact on v1.0.0:** profile block in `deployment.md` only; defaults and
  tests unchanged.

---

## D4 — Model priority policy

- **Current state:** `NVIDIA_MODEL_PRIORITY` / `OPENAI_MODEL_PRIORITY`
  default to empty (`config.py:256/259`); `NVIDIA /models` over-lists 221
  models but an account can invoke a subset, so empty priorities lengthen
  `/chat` candidate walks with non-retryable 404 `invalid_request`
  (`known-limitations.md` §4). The live smoke pins `SMOKE_NVIDIA_MODEL` for
  this reason.
- **Options considered:**
  - (a) Policy: **priorities are required in the production profile**, pinned
    to the deploying account's invocable model ids (chosen against the live
    account at deploy time).
  - (b) Leave empty and accept the walk.
- **Recommendation:** (a). Exact ids cannot be fixed today because they depend
  on the live account and B1 (OpenAI quota) is unresolved; the policy is
  therefore "pin priorities at deploy time in RC1/R4, sourced from a live
  `/v1/models` run and the smoke-run's working ids."
- **Final decision:** **CONFIRMED** — pin-at-deploy policy (a). `[x]`
- **Impact on v1.0.0:** `deployment.md` profile block + RC1 deploy step; no
  code change.

---

## D5 — License decision

- **Current state:** **No `LICENSE` file, no `license` field in
  `pyproject.toml` (`[project]` has only name/description/readme/deps/dynamic
  version), no `THIRD_PARTY_LICENSES.md`/`NOTICE`, no contributor policy.**
  Nothing in the repo is licensed; public distribution is blocked on this
  decision. All runtime and dev dependencies are permissively licensed
  (MIT/BSD-3/Apache-2.0 — see the readiness plan §5.4). Depends on **D10**
  (release posture).
- **Options considered:**
  - (a) Permissive (MIT) — simplest; matches the dependency ecosystem; no
    copyleft obligations; permissive contribution flow.
  - (b) Permissive (Apache-2.0) — MIT-plus patent grant and explicit
    contributor terms; common for community infra projects.
  - (c) Copyleft (GPL-3.0 / AGPL-3.0) — strong share-alike; AGPL matters only
    if distributed as a network service others deploy for profit.
  - (d) Proprietary / source-available — no public license; only if the
    posture is commercial/product.
- **Recommendation (conditional on D10):** if D10 = personal tool or
  open-source community → **MIT** (recommended); if D10 = commercial
  foundation → **proprietary/source-available** and skip public publishing.
  The dependency license audit (permissive only) is compatible with any of
  these.
- **Final decision:** **APPROVED — MIT.** `LICENSE` (MIT, "Relay
  contributors") created at repo root, `pyproject.toml` license metadata +
  SPDX classifier added, `THIRD_PARTY_LICENSES.md` generated from installed
  dist metadata (all permissive) — all in R2.
- **Impact on v1.0.0:** `LICENSE` file, `pyproject.toml` license metadata +
  classifiers, `THIRD_PARTY_LICENSES.md` (if required), contributor/CLA or
  DCO policy, README badge/tone, and the release notes. Blocks the release if
  unresolved.

---

## D6 — Deployed auth profile

- **Current state:** Defaults are insecure-by-design: `RELAY_API_KEY` empty →
  authentication disabled; `PERSISTENCE_ENABLED=false`.
  `blockers-before-public-release.md` §5 requires the deployed profile to set
  `RELAY_API_KEY=<long-random>`, `PERSISTENCE_ENABLED=true`,
  `PERSISTENCE_PATH`. Two auth tiers exist: constant-time bootstrap
  (`RELAY_API_KEY`) checked first, plus store-backed scoped keys when
  `RELAY_AUTH_STORE=true` (`app/security/auth.py:10-57`); scoped keys deny
  `/admin/*` (verified live: `chat,v1` key → 403). The recommended production
  profile is already drafted in `deployment.md:50-58`.
- **Options considered:**
  - (a) Full profile: `RELAY_API_KEY` (bootstrap) **and**
    `RELAY_AUTH_STORE=true` with per-app scoped keys, plus
    `PERSISTENCE_ENABLED=true` + `PERSISTENCE_PATH`.
  - (b) Bootstrap-only: `RELAY_API_KEY` set, store off.
  - (c) Minimal/no auth — rejected for any public exposure.
- **Recommendation:** (a) for the public release; (b) is an acceptable
  minimum for a private single-operator deployment. Both are profile-only.
- **Final decision:** **CONFIRMED** — (a) full auth+persistence profile for the
  public release. `[x]`
- **Impact on v1.0.0:** `deployment.md` profile block; RC1 deployed-profile
  verification (auth on, persistence available); release notes must state the
  insecure-by-default behavior for unconfigured instances.

---

## D7 — Environment retirement closure

- **Current state:** The P6.3 plan's approved scope ("Continue with: env
  retirement audit, shared helper extraction…" —
  `platform-p6-phase3-plan.md:22`) is **open**; no env-retirement audit was
  executed (`roadmap-post-p7-audit.md:155-157` flags it as unconfirmed).
  Today's parsed-but-unused config is limited to `OPENROUTER_API_KEY` and
  `GROQ_API_KEY` (subject to **D1**); `OLLAMA_BASE_URL` is functional
  (`app/providers/registry.py:177`). `configuration.md:79` is stale — it also
  lists `ANTHROPIC_API_KEY` as unused, but Anthropic is a wired runtime
  provider (see D8).
- **Options considered:**
  - (a) Execute a **bounded env-retirement audit in R2** (cleanup only): list
    every var in `config.py`/`config_spec.py`/`.env.example`, remove dead
    vars consistent with the D1 outcome, fix stale doc claims, no behavior or
    schema change.
  - (b) Defer post-v1.
- **Recommendation:** (a). Scope is tiny (two dead keys + one stale doc line)
  and it closes a named open item before the gate.
- **Final decision:** **CONFIRMED** — execute the bounded audit in R2 (a).
  `[x]`
- **Impact on v1.0.0:** small R2 cleanup commit; `.env` compat preserved
  (unknown vars ignored); closes the post-P7 audit item 5.

---

## D8 — Baseline / documentation number alignment

- **Current state:** The roadmap regression gate is stale: "existing **821
  tests** stay green" (`platform-implementation-roadmap.md:10`), written
  pre-P6.1; the measured baseline is now **2055 passed, 20 skipped**.
  `release-candidate-checklist.md:14` also lags at "1916 passed, 18 skipped".
  The alignment audit explicitly recommends renumbering
  (`roadmap-release-alignment-audit.md:89-91,195-196`).
- **Options considered:**
  - (a) Renumber the roadmap gate and RC checklist to the current measured
    baseline (2055/20) and note it is refreshed at each phase.
  - (b) Leave historical numbers.
- **Recommendation:** (a). Docs-only edit in R2; also correct the stale
  `configuration.md:79` claim that Anthropic is unused (see D7).
- **Final decision:** **CONFIRMED** — align numbers (a). `[x]`
- **Impact on v1.0.0:** doc consistency only; the gate definition is unchanged
  ("no new failures vs. the current baseline").

---

## D9 — Remaining release blockers

- **Current state:** `blockers-before-public-release.md` "Definition of done"
  (lines 88-101) status:
  - [ ] OpenAI quota restored; live smoke 6/6 vs OpenAI — **HARD BLOCKER,
    external, unresolved**
  - [x] `/v1` health feedback per-attempt — resolved
  - [x] 429 `Retry-After` policy — resolved (opt-in; profile decision = **D3**)
  - [ ] Retry hardening enabled in deployed profile → **D3**
  - [ ] Model priorities pinned → **D4**
  - [ ] Deployed with `RELAY_API_KEY` + `PERSISTENCE_ENABLED=true` → **D6**
  - [ ] Full regression green — **verified: 2055 passed / 20 skipped**
  - [ ] RC checklist signed off — **verified: `test_rc_validation.py` 28
        passed; live smoke blocked on quota**
  Additional gate items: license (**D5**, blocked on D10), version/tag
  alignment (W1: `v1.0.0` tag in the same change as `0.1.0 → 1.0.0`),
  `CHANGELOG.md` + release notes, known timing flake disposition
  (baseline-reproduced at `d344116`), RC1 stage (install/setup/upgrade +
  adversarial security review), PyPI publishing decision, and plan-doc
  disposition.
- **Options considered:** none — these are gate conditions, not alternatives.
- **Recommendation:** treat the list above as the release gate. The only
  external item is B1 (OpenAI quota) — start the account fix now, in parallel
  with R2, so live smoke can run at R3/RC1.
- **Final decision:** **CONFIRMED** — adopt this blocker list as the v1.0.0 gate
  (accepting no unresolved blockers at tag time). `[x]`
- **Impact on v1.0.0:** defines the R2–RC1 workload and the §12 acceptance
  criteria of the readiness plan.

---

## D10 — Public release posture

- **Current state:** Undecided. The README states "From PyPI (once published)"
  and "Publishing to PyPI … is planned", implying eventual distribution, but
  the intent — personal tool, community project, or commercial foundation —
  is not recorded. This decision frames **D5** (license), contribution policy,
  support expectations, and documentation tone.
- **Options considered:**
  - (a) **Personal / self-hosted tool** — no contribution expectations; MIT
    or even no public distribution; docs target the owner.
  - (b) **Open-source community project** — public repo + PyPI; permissive
    license; contribution policy (DCO/CLA) and issue templates; support is
    community-driven.
  - (c) **Commercial / product foundation** — proprietary or source-available
    licensing; support/SLA and contribution policy driven by the product
    roadmap; public distribution only if intended.
- **Recommendation:** if Relay is to be shared at all, (b) with **MIT** is the
  lowest-friction community posture. If it is strictly personal, (a) and a
  private (or unlicensed-but-unpublished) repo. (c) only if there is a
  productization intent.
- **Final decision:** **APPROVED — (b) open-source community project, limited
  public release.** v1.0.0 is published source-available (public repo +
  `v1.0.0` tag + release notes) under MIT with documented caveats: the
  single-process SQLite ceiling, the B1 OpenAI-quota caveat, and the framing
  of Relay as a proxy layer rather than a full agent framework. PyPI
  publishing is deferred to post-v1 (installer/`pip install .` path is
  supported); contribution policy is community-driven (MIT, no CLA). `[x]`
- **Impact on v1.0.0:** drives D5 (license), README/docs tone, contribution
  policy, release notes framing, and whether PyPI publishing is pursued.

---

## R1 summary

- **Deliverable:** this document plus the approved decision record. R1 is
  **complete**: D1 (drop OpenRouter/Groq), D5 (MIT), D10 (open-source
  community, limited public release) answered; D2–D4, D6–D9 confirmed.
- **Next step:** R2 execution — D1 drop, §5.2 key variants + tests, §6
  artifacts (LICENSE/CHANGELOG/version `1.0.0rc1`/`THIRD_PARTY_LICENSES.md`),
  D7 env-retirement audit, D8 baseline renumbering, and §2 documentation
  completion (architecture/configuration/deployment/known-limitations).
  `PROJECT_LOG.md` remains untouched until the release commit.
