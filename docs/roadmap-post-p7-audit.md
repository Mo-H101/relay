# Relay — Roadmap Post-P7 Audit

Status: **analysis only — no code changed, no commit, `PROJECT_LOG.md` untouched.**
Date: 2026-08-06

Purpose: re-baseline the roadmap after P7 completed (P7.1/P7.2/P7.3, landed as
`c5c1e76`, `fd22fdb`, `4d7408c`), confirm what remains before the roadmap's
v1 gate (M5), and recommend the next phase with acceptance criteria.

Sources read: `docs/platform-implementation-roadmap.md` (rev 2, 2026-08-03),
`docs/platform-recommended-order.md`, `docs/platform-missing-components-report.md`,
`docs/roadmap-release-alignment-audit.md` (pre-P7 baseline), `docs/platform-p7-plan.md`,
`docs/release-candidate-checklist.md`, `docs/blockers-before-public-release.md`,
`docs/known-limitations.md`, and current code (`app/cli/__init__.py`,
`app/cli/config.py`, `app/core/config_spec.py`, `app/setup/persistence.py`).

---

## 1. Current implementation status

All roadmap phases P0–P7 are now complete in code, in the recommended order
(P0 → P4 → P5 → P6 → P1 → P2 → P3 → P7 → P8):

| Phase | Roadmap definition | Status | Commit evidence |
| --- | --- | --- | --- |
| P0 | Packaging & distribution | ✅ | `c893a27` (pyproject, console script, installers) |
| P1 | First-run & CLI | ✅ core (see §4 P1 remainder) | `c893a27`, wizard→TUI handoff |
| P2 | TUI (main interface) | ✅ | `10f2b7f` … `ce5ea2c` (9 screens) |
| P3 | Async hot path | ✅ | `202f794` (async `/v1` + streaming) |
| P4 | Provider integrations (async-first) | ⚠️ one deferred decision remains (§4) | `d2c5f8c` … `be3d3e5` |
| P5 | API-key security | ✅ | `23ee1fe` … `11a68ac` |
| P6 | Platform DB + availability + usage | ✅ | `0f49419` … `2c091c3` (incl. P6.5 `apps` + `request_log`) |
| P7 | Configuration management | ✅ | `c5c1e76`, `fd22fdb`, `4d7408c` |
| P8 | Client guides + quality gate & CI | ⚠️ CI done; guides missing (§5) | `d344116` (CI in P6.4) |

Full suite after P7.3: **2055 passed, 20 skipped** (verified twice: 226.61s /
202.50s). Targeted config/TUI suite: 190 passed, 3 skipped. Working tree is
clean of tracked modifications; the plan/audit docs under `docs/` are
intentionally uncommitted.

---

## 2. P7 completeness — confirmed

P7's roadmap scope is fully delivered across its three approved sub-phases:

- **P7.1** (`c5c1e76`): configuration registry (`app/core/config_spec.py`,
  103 specs) + read-only `relay config` CLI.
- **P7.2** (`fd22fdb`): safe mutation workflow — `relay config
  set/unset/reload`, single-writer routing through `config_store`, dry-run
  validate → real apply → rollback on failure.
- **P7.3** (`4d7408c`): TUI Configuration panel derived from the registry
  (was 23 hardcoded rows).

Final P7.3 classification (verified): 103 specs → Runtime 60 / Network 6 /
Providers 17 / Security 11 / Storage 4 / Logging 4 / UI 1; 94 editable; 23
restart; 79 live; 1 info; 8 secrets; `MASKING_VIOLATIONS: []`. The `relay
config` surface now has `show` / `validate` / `diff` / `set` / `unset` /
`reload` subcommands. Requirements 4 and 18 ("all config reachable without
editing files") are closed. The P7 plan's explicit out-of-scope items (v1
release prep, licensing, `PROJECT_LOG.md`) remain out of scope — correct.

---

## 3. Remaining roadmap phases

Two phases are left in the original P0–P8 roadmap:

- **P8 — Client integration guides + quality gate & CI.** The quality-gate
  half already landed in P6.4 (`.github/workflows/ci.yml`: compileall + full
  suite on ubuntu/windows, packaging smoke; `[tool.pytest.ini_options]`
  present). **The only outstanding roadmap item is requirement 19: per-client
  setup guides** (Cline / OpenCode / Continue) plus a generic OpenAI-compatible
  section.
- **P7 and P3 are done**, so the M5 cut-line's remaining definitional
  component is P8. There is no post-v1 phase in the original roadmap; the
  release milestone itself is the final step.

Per `docs/platform-recommended-order.md`, M5 (performance & hardening) = P3 +
P7 + P8. With P3 and P7 complete, **P8 is the last phase before the M5 /
v1.0.0 gate**.

---

## 4. Remaining P4 / P6 / P7 gaps

| Area | Gap | Verdict |
| --- | --- | --- |
| **P4 remainder** | OpenRouter and Groq keys are parsed but unwired (`app/core/config.py:294/316`, `app/core/config_spec.py:408/423`) — no client/registry entry. This was P6 Decision H, deferred. | Open decision: wire both, or formally drop the reserved keys. Small, isolated either way. |
| **P6 remainder** | None. P6.5 (`2c091c3`) built the durable `request_log`, connected-apps view, and `relay apps` CLI — closing the M3 marker (`relay apps` after restart) and requirement 16. `availability.json`'s live write was retired (P6.5 F3); `read_all`/`iter_model_status` remain only as `relay migrate` legacy-import hooks. | Clean. |
| **P7 remainder** | None. All three P7 sub-phases are complete and committed. | Clean. |
| **P1 remainder** (context, not v1-blocking) | Roadmap P1 exit criterion named subcommands `status`, `providers`, `models`, `routing`, `logs`, `test`. The CLI now ships `setup`, `tui`, `serve`, `keys`, `provider keys`, `migrate`, `events`, `apps`, `config`. Missing from the CLI: `status`, `providers`, `models`, `routing`, `test` (`logs` is covered by `events`). The TUI covers those surfaces (Dashboard/Models/Providers/Configuration tabs). | These were superseded by the TUI as the main interface (P2). Not v1-blocking; note in the release gate as an intentional deviation. |

---

## 5. What P8 contains

P8 = **Cline / OpenCode / Continue setup guides (requirement 19)** + a generic
OpenAI-compatible section, on top of the CI that already landed in P6.4:

- One setup guide per client pointing at Relay's `/v1` endpoint (local
  `http://127.0.0.1:8000/v1` and the deployed URL), including:
  - key creation via the documented flow (`relay keys add --label "opencode"`),
  - the client's provider/base-URL config snippet,
  - model-id selection (from `/v1/models`),
  - a verification step (one test-message round trip),
  - per-client troubleshooting (auth mismatch, model-not-found, traffic
    routing to the client's own default provider).
- A generic "any OpenAI-compatible client" section for everything else.
- The existing `docs/ux-validation-guide.md` has manual connection walkthroughs
  for Cline/OpenCode (§7/§8) to build on; they are validation procedures, not
  the client setup guides req 19 asks for.

Scope discipline: **docs only** — no runtime code change required (or minimal,
e.g. nothing anticipated). CI is already the quality gate.

---

## 6. Should P8 be next? — Yes

- It is the **only remaining roadmap phase**, and the M5/v1 gate is defined as
  P3 + P7 + P8, of which two are already done.
- It is **low-risk and additive** (docs only), matches the recommended-order
  track D ("quality gate — low risk, do last"), and is **independent** of the
  two open decisions (P4 OpenRouter/Groq closure, P1 subcommand deviation).
- It unlocks the v1.0.0 gate: after P8, the M5 checklist is code-complete and
  the remaining work is the release milestone (blockers + version bump), not
  more feature phases.

Alternative (not recommended): tag the current state as `v0.x` "gateway GA"
first (per `docs/roadmap-release-alignment-audit.md` §6) and defer P8.
Defensible but unnecessary — P8 is docs-only and short, so completing it
before the milestone avoids two release cycles.

---

## 7. Hidden technical debt before P8

Not blockers for a docs phase, but should be surfaced in the v1 gate:

1. **Reserved-but-unused provider keys.** `OPENROUTER_API_KEY` / `GROQ_API_KEY`
   are parsed and listed as "Security" in `config_spec.py` (and `.env.example`)
   with no client wired. Either wire the providers or remove the dead config
   surface (Decision H closure).
2. **Known timing flake.** One pre-existing test flake, baseline-reproduced at
   `d344116`; it predates P7 and is excluded from the 2055/20 result. Not a
   P7 regression, but should be named in the v1 gate.
3. **Hand-maintained registry classification.** Editable/restart/live/secret
   flags in `config_spec.py` are maintained by hand; drift risk if new fields
   are added without updating the spec. The P7.3 `MASKING_VIOLATIONS: []`
   check is the guard; keep it in CI.
4. **P1 CLI deviation unrecorded.** The roadmap's subcommand list vs. the
   shipped CLI (TUI supersedes) was never formally resolved. Document it as an
   intentional deviation in the release gate.
5. **Env retirement audit.** The P6 phase-3 plan listed an env-retirement audit
   and shared-helper extraction as follow-ups; confirm whether these were
   executed or remain open before the gate (they are cleanup, not features).

---

## 8. Security / release risks

Carried over from `docs/blockers-before-public-release.md`,
`docs/release-candidate-checklist.md`, `docs/known-limitations.md`; unchanged
by P7:

- **OpenAI key out of quota (HARD BLOCKER):** no OpenAI completion can
  succeed until billing is restored; gateway is NVIDIA-only in practice.
  Re-run `tests/run_live_smoke.py` (6/6) after the fix.
- **Defaults are insecure-by-default by design:** `RELAY_API_KEY` empty →
  auth disabled; `PERSISTENCE_ENABLED` false. Deployed profile must set
  `RELAY_API_KEY`, `PERSISTENCE_ENABLED=true`, `PERSISTENCE_PATH`.
- **Retry-hardening knobs default off:** immediate 429 retries unless
  `RETRY_HONOR_RETRY_AFTER=true`; operator must decide the deployed profile.
- **NVIDIA over-lists models:** pin `NVIDIA_MODEL_PRIORITY` /
  `OPENAI_MODEL_PRIORITY` to invo-cable models to avoid the 404 walk.
- **Version alignment:** package is `0.1.0` / `relay --version` prints
  `relay 0.1.0`; do not tag `v1.0.0` without a 1.0.0 version bump in the same
  release (warning W1 from the alignment audit).
- **Process:** plan/audit docs remain uncommitted by design; commit the
  surviving design docs with the phase they belong to, not as silent
  orphans.

---

## 9. Recommended next milestone

**P8 — client integration guides** (docs-only), then the **v1.0.0 gate**:

1. Ship P8: Cline / OpenCode / Continue setup guides + generic
   OpenAI-compatible section (req 19), verified against the real key-creation
   flow.
2. Close the two open decisions at the gate:
   - P4 Decision H: wire OpenRouter + Groq, or drop the reserved keys.
   - P1 deviation: record the CLI-subcommand scope as superseded by the TUI.
3. Run the release gate: RC checklist + blockers list (OpenAI quota,
   retry-hardening decision, model priorities, deployed auth/persistence
   profile) + full 2055-test regression green.
4. Tag: bump package version to `1.0.0` **and** tag `v1.0.0` in the same
   change (resolves W1). Do not tag `v1.0.0` before P8.

---

## 10. Acceptance criteria for the next phase (P8)

- [ ] Three per-client guides exist (Cline, OpenCode, Continue), each with:
      install prerequisites, provider/base-URL config pointing at Relay's
      `/v1`, key creation via `relay keys add --label <client>`, model-id
      selection, a verification round trip, and a troubleshooting section.
- [ ] A generic "any OpenAI-compatible client" section is included.
- [ ] Each guide's key-creation flow is exercised for real: `relay keys add
      --label "opencode"` returns a scoped key that authenticates against
      `/v1` (the P5 acceptance flow is the reference).
- [ ] No runtime code change (or only the minimal change the guides require);
      full suite stays green (2055 passed, 20 skipped) — the CI merge gate
      verifies on push.
- [ ] Guides land with the phase they belong to; `PROJECT_LOG.md` updated per
      the workflow when approved (not by this audit).
- [ ] The gate records the two open decisions from §9 so the v1.0.0 release is
      the only thing left after P8.
