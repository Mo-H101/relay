# Relay — v1.0 Release Readiness Plan

Status: **planning only — no code changed, no commit, `PROJECT_LOG.md` untouched.**
Date: 2026-08-06

Purpose: audit the completed P0–P8 platform and define everything that must
happen before the `v1.0.0` tag. This plan records the remaining blockers,
decisions, risks, checklists, and release preparation phases. Nothing here is
implemented; each phase requires separate approval and follows the
established workflow: Audit → Plan → Approval → Implementation → Tests →
Commit.

Scope: **v1.0.0 readiness only.** Explicitly out of scope: new features,
post-v1 roadmap work, and any runtime/API/schema/persistence change that is
not a pre-release decision item listed below.

Sources audited: `docs/platform-implementation-roadmap.md`,
`docs/platform-recommended-order.md`, `docs/platform-missing-components-report.md`,
`docs/roadmap-post-p7-audit.md`, `docs/roadmap-release-alignment-audit.md`,
`docs/platform-p8-plan.md`, `docs/blockers-before-public-release.md`,
`docs/release-candidate-checklist.md`, `docs/known-limitations.md`,
`docs/rollback-procedure.md`, `docs/deployment.md`, `docs/security.md`,
`pyproject.toml`, `app/__version__.py`, `.github/workflows/ci.yml`,
`.gitignore`, and current repo state.

---

## 1. Current state summary

- **Roadmap:** P0–P8 complete. Last commit `0a682cf` (P8 client guides).
  M5 / v1.0.0 gate is **code-complete** — every phase the roadmap attached to
  the platform milestone has landed.
- **Tests:** 2055 passed, 20 skipped (verified twice, e.g. 193.71s). RC
  offline suite (`tests/test_rc_validation.py`) 28 passed. CI green
  (`.github/workflows/ci.yml`): compileall + full suite on ubuntu 3.10/3.11/3.12/3.13
  and windows 3.12; packaging job builds sdist/wheel and smokes
  `relay --help` / `relay --version` on ubuntu 3.12.
- **Version:** `app/__version__.py` = `"0.1.0"`; `pyproject.toml` derives the
  version from it (`dynamic = ["version"]`); `relay --version` prints
  `relay 0.1.0`. No tags exist.
- **Known blockers (from `blockers-before-public-release.md`):** OpenAI key
  out of quota (hard blocker for OpenAI traffic; NVIDIA validated 6/6);
  default profile is unauthenticated and non-persistent until configured.
- **Repo hygiene:** tracked tree clean. 24 plan/audit docs under `docs/`
  remain untracked by design. `.env` (live keys), `.relay/platform.db`,
  `.venv/`, `dist/`, `build/`, `__pycache__` are present on disk and
  gitignored.

---

## 2. Remaining blockers

| # | Blocker | Status | Required action | Owner decision |
| --- | --- | --- | --- | --- |
| B1 | **OpenAI key out of quota** | HARD BLOCKER (`blockers-before-public-release.md` §1) | Restore billing on the account behind `OPENAI_API_KEY`; re-run `python tests/run_live_smoke.py` and confirm 6/6 | Operator (external) |
| B2 | **Unauthenticated-by-default profile** | Deployment decision | Deployed profile must set `RELAY_API_KEY=<long-random>`; otherwise an exposed instance is unauthenticated (§5 hardening) | Release owner |
| B3 | **Non-persistent-by-default** | Deployment decision | Deployed profile must set `PERSISTENCE_ENABLED=true` + `PERSISTENCE_PATH` | Release owner |
| B4 | **Version/tag alignment** | Warning W1 from `roadmap-release-alignment-audit.md` | `v1.0.0` tag must land in the same change as `0.1.0 → 1.0.0`; never tag v1.0.0 at 0.1.0 | Release owner |
| B5 | **No license** | Missing entirely | No `LICENSE` file, no `license` field in `pyproject.toml` — must be resolved before any public release (§5) | License decision required |
| B6 | **No changelog/release notes** | Missing | `CHANGELOG.md` + v1.0.0 release notes required | Release owner |

B1 is the only *environment* blocker; the rest are decisions/artifacts this
plan turns into a checklist.

---

## 3. Required decisions

Each item is a **decision to record**, not a code change by this plan:

| # | Decision | Options | Notes |
| --- | --- | --- | --- |
| D1 | **OpenRouter/Groq wiring** (Decision H, P4 remainder) | (a) wire both providers, or (b) formally drop the reserved keys | Keys parsed in `app/core/config.py:294/316`, spec entries `config_spec.py:408/423`, `.env.example:125-138` label them "reserved" with no client. If dropped: remove the dead config surface. If wired: registry entries + async OpenAI-compatible clients. |
| D2 | **P1 CLI deviation record** | Record as intentional deviation | Roadmap P1 named `status/providers/models/routing/logs/test`; shipped CLI is `setup/tui/serve/keys/provider keys/migrate/events/apps/config` — the TUI covers the status/models/providers/routing surfaces and `events` covers logs. Record in release notes, not code. |
| D3 | **Retry hardening profile** | Enable `RETRY_HONOR_RETRY_AFTER` (+ optional backoff/budget) or keep immediate-retry defaults | Default-off is documented (`known-limitations.md` §1). Operator decision for the deployed profile. |
| D4 | **Model/provider priorities** | Pin `NVIDIA_MODEL_PRIORITY` / `OPENAI_MODEL_PRIORITY` to models the account can invoke | Mitigates the 221-model over-list walk (`known-limitations.md` §4). |
| D5 | **License choice** | Choose a license (process in §5); do not publish before this is resolved. Covers: repository license choice, dependency license audit, attribution requirements, third-party notices, and contributor/license policy | Affects `LICENSE`, `pyproject.toml`, packaging; decided after D10. |
| D6 | **PyPI publishing** | Publish to PyPI, or distribute by git/installer only | README says "From PyPI (once published)" and "Publishing to PyPI … is planned". If publishing, add classifiers, long_description, license, and a publish CI job. |
| D7 | **Env-retirement audit closure** | Confirm executed or re-schedule | `platform-p6-phase3-plan.md` listed an env-retirement audit + shared-helper extraction as follow-ups; verify status before the gate (cleanup, not feature). |
| D8 | **Roadmap gate baseline renumber** | Update regression-gate baseline (821 → 2055) in the roadmap doc | Open item from `roadmap-release-alignment-audit.md`; doc drift only. |
| D9 | **Plan-doc disposition** | Commit surviving design/audit docs with the release, or keep uncommitted | 24 untracked docs currently; decide the commit point (recommended: with the v1.0.0 release commit). |
| D10 | **Public release posture** | (a) personal/self-hosted tool, (b) open-source community project, (c) commercial/product foundation | Decided before D5; affects license choice, contribution policy, support expectations, and documentation tone. |

---

## 4. Release preparation phases

Each phase is a separate approved work item. No phase starts until the
previous decisions it depends on are made. Every phase follows the
established workflow: Audit → Plan → Approval → Implementation → Tests →
Commit.

- **Phase R1 — Decisions & legal (strictly decision-gathering).** Resolve and
  record D1–D10 only. **No implementation, no version bump, no `LICENSE`
  file, and no `CHANGELOG.md` generation.** Deliverable: the decision record
  (`docs/release-decisions.md`) capturing each choice and the release
  requirements it implies. Nothing else changes.
- **Phase R2 — Code & artifacts.** If D1 chose wiring: provider additions
  (registry + clients + tests). If D1 chose drop: remove reserved keys/env
  surface. Pre-release version bump `0.1.0 → 1.0.0rc1`
  (`app/__version__.py`) so the candidate has a PEP 440 version; add
  `LICENSE`, `CHANGELOG.md`, `pyproject.toml` license/classifiers; update
  `.env.example` and README version/install references. No `PROJECT_LOG.md`
  change until the release commit (per workflow).
- **Phase R3 — Verification.** Full suite green; RC offline suite green; live
  smoke 6/6 once B1 is cleared; security checklist (§6) executed on the
  candidate; deployed profile exercised with the §7 hardening block.
- **Phase RC1 — Release candidate.** After R3 passes: tag the candidate
  (`v1.0.0-rc.1`, `app/__version__.py` at `1.0.0rc1` per the single-source
  design); clean-machine installation test (Windows `install.cmd`, Linux
  `install.sh`); fresh user setup test (bare `relay` wizard → TUI); upgrade
  test (`relay migrate` from the legacy 0.x layout + rollback drill);
  adversarial security review (§6 adversarial stage). **Only after RC1 fully
  passes is `v1.0.0` cut.**
- **Phase R4 — Release.** Final bump to `1.0.0` (from the `1.0.0rc1`
  candidate), `git tag v1.0.0` landed in the same change (B4/W1), GitHub
  release notes; optional PyPI publish if D6 = publish; post-release
  `/diagnostics` + RC suite on the tag.
- **Phase R5 — Post-release.** Upgrade/rollback drill from 0.x → 1.0.0
  (`relay migrate`, `docs/rollback-procedure.md`); record any field issues in
  `known-limitations.md`.

---

## 5. Licensing / legal preparation checklist

Current state: **no `LICENSE` file, no `license` field in `pyproject.toml`**
(no `[project] license` metadata), no `NOTICE`/`THIRD_PARTY` file, no
`CHANGELOG.md`. Nothing in the repo is licensed today, so **public
distribution is blocked on this checklist**:

1. **Repository license choice (decision D5, made after D10).** D10 (release
   posture) frames the choice: personal/self-hosted tool, open-source
   community project, or commercial/product foundation. Recommended process
   (not a choice): evaluate permissiveness vs. copyleft fit for a self-hosted
   gateway using permissively-licensed dependencies; consult a
   license-selection guide (e.g. choosealicense.com); record the choice and
   rationale in the decision record. Do not choose here — the user decides.
2. **Add `LICENSE`** at the repo root once chosen.
3. **Add license metadata** to `pyproject.toml`:
   `license = { text = "<SPDX id>" }` (or `file = "LICENSE"`) and any SPDX
   classifier in `[project.classifiers]`.
4. **Dependency license audit (D5).** All runtime deps are permissive:
   fastapi/httpx/uvicorn (BSD-3), pydantic/python-dotenv (MIT), rich/textual
   (MIT), platformdirs (MIT), keyring (MIT); dev: pytest (MIT),
   pytest-asyncio (Apache-2.0), openai (Apache-2.0), build/setuptools/wheel
   (MIT). Verify pinned versions from `.venv` `dist-info` (all present) and
   record in a `THIRD_PARTY_LICENSES.md` (or NOTICE) if the distribution
   format requires it. Use `pip-license`/`pip-licenses` or the `.dist-info`
   files at release time.
5. **Attribution requirements & third-party notices (D5).** For any
   dependency whose license requires reproducing text (BSD/MIT do for the
   source-form distribution) — decide whether the wheel's
   `METADATA`/license-files mechanism (setuptools ≥ 68) satisfies this, or a
   `THIRD_PARTY_LICENSES.md`/`NOTICE` file must be shipped with the artifact.
6. **Contributor/license policy (D5).** Set the contribution policy that
   matches D10: whether external contributions are accepted, the CLA/DCO
   requirement, and the copyright holder line in the `LICENSE`; record it for
   the repository and release notes.
7. **Trademarks/model names.** Guides reference provider model ids
   (NVIDIA/OpenAI naming) — provider names in docs are factual references, not
   endorsement; no action unless publishing a commercial product (record).
8. **Changelog/release notes.** Create `CHANGELOG.md` (Keep-a-Changelog
   format) capturing P0–P8 highlights and the v1.0.0 entry; release notes for
   the GitHub release mirror the same content. (Not created in R1 — Phase
   R2.)

---

## 6. Security checklist

Executed against the release candidate, before the tag:

### Authentication model
- [ ] `RELAY_API_KEY` set in the deployed profile (constant-time bootstrap
      tier, checked first).
- [ ] Store-backed keys (`RELAY_AUTH_STORE=true`) exercised: scoped keys
      (`--scopes chat,v1`), scope denial on `/admin/*` (403), revoked/expired
      key → 401, store outage fails closed (401) — covered by
      `tests/test_auth_*.py`; re-verify live.
- [ ] Identical 401 body for every failure (no oracle); `WWW-Authenticate:
      Bearer` present.
- [ ] `/` and `/health` remain the only public paths; `/docs`, `/redoc`,
      `/openapi.json` protected (they are real endpoints behind the guard).

### Secret handling
- [ ] Relay keys hashed (scrypt, per-key salt) at rest in `platform.db`; raw
      key printed exactly once by `relay keys add`; never in `list`/`test`.
- [ ] Upstream provider keys out of plaintext where keyring is available
      (`RELAY_KEYRING=true`); `.env` remains the documented fallback.
- [ ] Redaction contract verified: provider error bodies bounded (200 chars),
      control chars stripped, API key scrubbed; admin responses report
      secrets by field name only; reload errors redact to
      "Invalid value for <ENV_VAR>".
- [ ] Memory/privacy contract: no prompts, responses, keys, or correlation
      ids in the database or logs (`memory_contract.py` + tests).

### Keyring behavior
- [ ] Keyring-first resolution with `.env` fallback confirmed on both
      Windows (Windows Credential Manager) and Linux (Secret Service /
      plaintext backend configured via `RELAY_KEYRING_BACKEND`).
- [ ] `relay provider keys migrate --dry-run`/`--yes` runbook executed on the
      release candidate; output shows only `********abcd` tails.

### At-rest secret protection — decision
- [ ] Record the **no-encryption-at-rest** decision explicitly
      (`deployment.md` §2 documents it): provider keys are protected at rest
      only by the OS keyring; a plaintext `.env` fallback must be protected by
      disk encryption + service-account ACLs. If v1 requires stronger
      guarantees, a keyring/encrypted-store enhancement is a separate project
      (do not scope-creep it into v1.0.0).

### Permission handling
- [ ] `.env` written with `0600` on POSIX after config changes; confirm the
      same intent on Windows (per-user profile / ACL) and note any gap in
      `security.md`.
- [ ] `platform.db` and `state_dir` permissions reviewed (writable by the
      service account only).
- [ ] Installer behavior: PATH modification, admin-required operations
      documented; no secrets written by installers.

### Audit logging
- [ ] Durable event log verified: `auth.success`, `auth.failure` (with
      reasons, never secrets), `key.create`, `key.rotate`, `key.prune`,
      `config.reload`; retention bounded by `PERSISTENCE_RETENTION_DAYS`;
      `relay events` tail works.
- [ ] Log records never contain prompts, responses, keys, or proxy
      credentials.

### Abuse scenarios (review, then test where feasible)
- [ ] Unauthenticated default: an instance with empty `RELAY_API_KEY` and
      `RELAY_AUTH_STORE=false` accepts all requests — confirm the release
      notes/deployment profile make this explicit, and that the deployed
      profile is authenticated.
- [ ] Brute force / credential stuffing on `/v1`: no auth rate limiting today
      (documented as future work). Decide whether v1 accepts this risk or adds
      a minimal fail-safe (decision, not implementation here).
- [ ] Key leakage channels: 401 bodies, provider errors, `/diagnostics`,
      `/metrics`, admin responses, and streamed error chunks — none may
      expose key material; covered by redaction tests; re-verify on the tag.
- [ ] Proxy credential handling: `HTTP(S)_PROXY` honored only when
      `PROXY_ENABLED=true`; proxy credentials never logged.
- [ ] Base-URL configurability (SSRF surface): provider `base_url` is
      operator-controlled by design; document that only trusted operators
      change provider URLs.

### Adversarial review stage (executed at RC1, independent reviewer)

Performed by a reviewer other than the implementer, against the tagged
`v1.0.0-rc.1`:

- [ ] **Unauthorized config modification:** verify no unauthenticated path can
      change configuration (`.env`, `/admin/reload`, store-backed keys);
      reload without a valid key → 401; config/admin endpoints reject scoped
      keys (`chat,v1` → 403).
- [ ] **Secret leakage:** fuzz 401 bodies, provider errors, `/diagnostics`,
      `/metrics`, admin responses, and streamed error chunks for key material,
      raw `OPENAI_API_KEY`/provider keys, or `.env` content; confirm the
      redaction contract (bounded 200-char bodies, control chars stripped,
      `********abcd` tails, secrets by field name only).
- [ ] **API abuse:** unauthenticated `/v1/*` traffic, brute-force/credential
      stuffing on the auth guard, invalid `Authorization` schemes, malformed
      bodies, and oversized payloads — expect consistent 401/400/422 with
      identical 401 bodies (no oracle), no crashes, and no state corruption.
- [ ] **Downgrade/rollback abuse:** downgrade attack via older-version config
      or migrated DB (`relay migrate --rollback`), schema downgrade paths, and
      legacy 0.x config/state interaction — verify the tool refuses or
      back-fills safely and never silently degrades auth or persistence.
- [ ] **No "toy project" weaknesses:** confirm no default credentials, no
      debug/secret endpoints, no world-writable state, no secrets in logs, and
      no unauthenticated admin surface remains before the gate.

---

## 7. Deployment checklist

Executed on the release candidate:

- [ ] **Fresh install:** clean Windows machine via `install.cmd` and clean
      Linux via `install.sh`; `relay --version` prints `1.0.0`; bare `relay`
      opens the wizard; post-setup handoff to the TUI works.
- [ ] **Wheel install:** CI packaging job green on the tag; fresh-venv
      `pip install dist/*.whl` → `relay --help`/`--version`/`relay serve`
      work.
- [ ] **Upgrade path:** from a 0.x-style legacy install, `relay migrate
      --dry-run` → `--yes` consolidates `relay_keys.db`/`relay_state.db`/
      `availability.json` into `platform.db` with backup + verify;
      re-running is idempotent.
- [ ] **Configuration migration:** `.env` compat retained (all documented
      vars still honored); `RELAY_ENV_FILE` override works; `relay config
      show/validate/diff` on a real `.env` (no secrets printed); env-retirement
      audit closed (D7).
- [ ] **Rollback:** `docs/rollback-procedure.md` exercised for code (git
      checkout of previous tag), config (restore `.env` + `/admin/reload`),
      and data (`relay migrate --rollback`).
- [ ] **Production profile** (the block below) applied and verified
      (`/diagnostics` shows auth on, persistence available):
      ```dotenv
      RELAY_API_KEY=<long-random-value>
      PERSISTENCE_ENABLED=true
      PERSISTENCE_PATH=/var/lib/relay/platform.db
      HEALTH_FEEDBACK_ENABLED=true
      TELEMETRY_ENABLED=true
      HEALTH_AWARE_ROUTING=true
      # optional, per D3/D4:
      # RETRY_HONOR_RETRY_AFTER=true
      # NVIDIA_MODEL_PRIORITY=<invocable-models>
      # OPENAI_MODEL_PRIORITY=<invocable-models>
      ```
- [ ] **Single-process constraint** documented and honored (SQLite
      single-writer; no `--workers N`).
- [ ] **TLS at the reverse proxy** for any non-local exposure; `X-Relay-Correlation-Id`
      available on success and error responses for client correlation.
- [ ] **Live smoke:** `python tests/run_live_smoke.py` 6/6 after B1 cleared.

---

## 8. Testing checklist

- [ ] **Full suite** green on the release commit: `python -m pytest tests -q`
      → expected 2055 passed, 20 skipped (current baseline; re-run and record
      the actual number on the tag).
- [ ] **RC offline suite** green: `python -m pytest tests/test_rc_validation.py -q`
      → 28 passed.
- [ ] **Known flake** named and dispositioned: one pre-existing timing flake
      (baseline-reproduced at `d344116`); confirm it is not a P0–P8
      regression and record whether to fix or accept before release.
- [ ] **CI** green on the tag: ubuntu 3.10/3.11/3.12/3.13 + windows 3.12
      compileall + suite; packaging job builds and smokes the wheel
      (now printing `relay 1.0.0`).
- [ ] **Gap analysis (recorded, not blocking unless so decided):**
      - No automated fresh-install E2E on a clean OS (manual in §7).
      - No automated auth-brute-force / abuse test (risk accepted or a
        minimal test added in Phase R3).
      - No load/scale test beyond the single-process documented limit.
      - P8 guide commands verified manually against a live instance
        (documented in the P8 implementation); consider a doc-lint test later.
- [ ] **No new failures** relative to the 2055/20 baseline after all Phase R2
      code changes (0 regressions).

---

## 9. Files expected to change later (during release prep, after approval)

- `app/__version__.py` — `"0.1.0"` → `"1.0.0rc1"` (Phase R2) → `"1.0.0"`
  (Phase R4); the `v1.0.0` tag must land with the `1.0.0` change.
- `pyproject.toml` — license metadata + classifiers; possibly a publish job
  reference (D6).
- `LICENSE` (new) — once D5 is decided.
- `CHANGELOG.md` (new) — P0–P8 highlights + v1.0.0 entry.
- `THIRD_PARTY_LICENSES.md` (new, or NOTICE) — dependency license audit
  (§5.4).
- `.env.example` — remove reserved `OPENROUTER_API_KEY`/`GROQ_API_KEY` if D1 =
  drop; document them if D1 = wire.
- `app/core/config.py`, `app/core/config_spec.py`,
  `app/providers/registry.py`, `app/providers/` — **only if** D1 = wire
  OpenRouter/Groq; or `config.py`/`config_spec.py`/`.env.example` removal of
  the dead vars if D1 = drop.
- `README.md` — version/install/PyPI references once D6 is decided.
- `docs/` — `docs/release-decisions.md` (or a `CHANGELOG.md` section) for
  D1–D10; deployment profile and upgrade notes; renumber of the roadmap
  regression-gate baseline (D8).
- `PROJECT_LOG.md` — updated **only at the release commit**, per workflow.
- Optionally `.github/workflows/ci.yml` — a publish/release job if D6 =
  publish (explicitly out of the current CI scope until decided).

## 10. Files that must remain untouched

- `app/` runtime, `tests/`, `pyproject.toml` runtime deps, `requirements*.txt`
  — no changes unless an approved decision (D1/D6) requires them.
- `PROJECT_LOG.md` — untouched until the release commit (per workflow).
- Plan/audit docs (`docs/platform-*.md`, `docs/roadmap-*.md`,
  `docs/v1.0.0-final-audit.md`, this plan) — remain untracked until the
  disposition decision (D9).
- `.github/workflows/ci.yml` — unchanged unless D6 = publish (then a separate
  approved change).
- `.env`, `.gitignore` — never committed/edited for secrets; `.env` stays
  local.
- The existing `docs/clients/` guides and all signed-off docs — no edits
  unless a release fact changes (e.g., version strings in examples).

---

## 11. Rollback strategy

The release-prep changes are small and independently revertible; the
deployment rollback procedure is already documented in
`docs/rollback-procedure.md`.

- **Pre-release code changes (Phase R2):**
  - Version bump: one-line revert (`git revert <commit>` or re-edit
    `app/__version__.py`).
  - License/changelog/third-party files: additive — removing them restores
    the exact prior state.
  - D1 code (if wiring OpenRouter/Groq): revert the provider commit; if
    dropping reserved keys: restore the removed env vars (no data impact).
  - No schema/persistence migration is introduced by any Phase R2 change, so
    **no database rollback is required** for the release itself.
- **Tag:** before push, a bad `v1.0.0` tag can be deleted and re-created;
    after push, do not rewrite history — cut `v1.0.1` for a fix.
- **Deployment:** per `docs/rollback-procedure.md` — code (previous tag +
    reinstall), config (restore `.env` + `/admin/reload?dry_run` → reload),
    data (`relay migrate --rollback` or manual `platform.db` restore).
- **Signals to roll back:** new regressions in the 2055/20 baseline, auth or
  persistence failing in the deployed profile, fresh-install failures, or
  live-smoke failures after B1.

---

## 12. v1.0.0 acceptance criteria

The tag may be placed only when **all** of the following are true:

- [ ] **Blockers cleared:** OpenAI quota restored and live smoke 6/6 (B1);
  deployed profile authenticated and persistent (B2/B3).
- [ ] **Decisions recorded:** D1–D10 resolved and written into the decision
      record; release posture (D10) and license (D5) decided; OpenRouter/Groq
      either wired or dropped (no dead "reserved" keys).
- [ ] **Version alignment:** `app/__version__.py` is `1.0.0`,
  `relay --version` prints `relay 1.0.0`, and the `v1.0.0` tag is created in
  the same change (B4).
- [ ] **Licensing:** license chosen (D5), `LICENSE` present, `pyproject.toml`
  license metadata set, dependency license audit recorded (B5).
- [ ] **Release artifacts:** `CHANGELOG.md` + release notes present (B6);
  PyPI decision recorded (D6); `.env.example` matches reality (D1 outcome).
- [ ] **Quality gate:** full suite green with 0 new failures (expected
  2055/20 baseline), RC offline suite green (28), CI green on the tag,
  fresh install verified on Windows + Linux, known flake dispositioned.
- [ ] **Release candidate passed:** RC1 executed and green — tagged
      `v1.0.0-rc.1`, clean-machine install, fresh user setup, upgrade test,
      and the adversarial security review (§6) — before the `v1.0.0` tag.
- [ ] **Security checklist (§6) executed** on the release candidate,
  including the at-rest-secrets decision and abuse-scenario review.
- [ ] **Deployment checklist (§7) executed**, including the production
  profile, upgrade path, and rollback drill.
- [ ] **Docs consistent:** deployment profile, upgrade notes, and roadmap
  regression-gate baseline updated; plan/audit docs dispositioned (D9).
- [ ] `PROJECT_LOG.md` updated at the release commit (workflow step) and the
  release notes published.

---

## Recommendation

Work the phases in order R1 (decisions + legal) → R2 (code/artifacts) → R3
(verification) → RC1 (release candidate) → R4 (release) → R5 (post-release),
because the code changes (version, license, D1) and the acceptance gate depend
on decisions first. R1 is strictly decision-gathering — no implementation, no
version bump, no `LICENSE`, no `CHANGELOG.md`; it produces only the decision
record. Decide D10 (release posture) before D5 (license). The OpenAI quota
blocker (B1) is the only item with an external dependency — start the account
fix in parallel with R1 so the live-smoke re-run is ready for R3/RC1.
