# Relay — Roadmap ↔ Release Alignment Audit

Status: **analysis only — no code changed, no commit, `PROJECT_LOG.md` untouched**.

Purpose: before any `v1.0.0` tag is placed, re-baseline the current release state
against the *original* 9-phase roadmap and answer four questions:

1. Which phase was originally intended to define v1.0?
2. Are the remaining phases (P7–P9) v1-required or post-v1?
3. Was the v1.0.0 readiness audit premature?
4. What is the recommended milestone naming?

Sources read: `docs/platform-implementation-roadmap.md` (rev 2, 2026-08-03),
`docs/platform-recommended-order.md`, `docs/platform-missing-components-report.md`,
`docs/platform-p6-plan.md`, `docs/platform-db-schema.md`, and the current code
(`app/cli/__init__.py`, `app/ui/app.py`, `app/providers/registry.py`,
`app/core/config.py`).

---

## 1. Phase-numbering note

The roadmap defines **P0–P8** (plus "Phase 9", which is the analysis phase that
produced the four roadmap deliverables — architecture, missing-components,
roadmap, recommended-order — and is already complete).

"P1–P6 complete / P7–P9 remaining" is therefore ambiguous:

| Your numbering | Roadmap phase | Status |
| --- | --- | --- |
| P1 | P0 Packaging & distribution | Complete |
| P2 | P1 First-run & CLI | Partial (see §2) |
| P3 | P2 TUI | Substantially complete |
| P4 | P3 Async hot path | Complete |
| P5 | P4 Provider integrations | Partial (see §2) |
| P6 | P5 API-key security | Complete |
| P7 | P6 Platform database + availability + usage | Partial (see §2) |
| P8 | P7 Configuration management | Not started |
| P9 | P8 Client guides + quality gate & CI | Partial (CI only) |

The remaining work is the same under either reading: **roadmap P6 remainder,
P7, and P8** (plus the release milestone itself).

---

## 2. Phase completion vs. the original plan

| Phase | Original exit criterion | Current state | Gap |
| --- | --- | --- | --- |
| **P0** Packaging | `relay` works after one install | Done — pyproject, console script, packaging smoke tests, CI packaging job | — |
| **P1** First-run & CLI | "Every subcommand functional": `serve`, `status`, `providers`, `models`, `keys`, `routing`, `apps`, `config`, `logs`, `test` | `setup`, `tui`, `serve`, `keys`, `provider keys`, `migrate`, `events` exist; bare-`relay` → wizard → TUI handoff works | **Missing 8 subcommands**: `status`, `providers`, `models`, `routing`, `apps`, `config`, `logs`, `test` |
| **P2** TUI | Screens: Chat, Model test, Providers, Keys, Model priority, Routing rules, Connected applications, System status, Configuration | 7 tabs: Dashboard, Chat, Models, Providers, Configuration, Applications, Diagnostics. Setup→TUI handoff (req 9) done | Applications tab is the interim in-memory surface (§ P6); no separate "Model test" screen (Chat tab covers it) |
| **P3** Async hot path | `/v1` latency parity; no threadpool contention | Done — `async_chat_service`, async endpoints | — |
| **P4** Providers (async-first) | "Every provider selectable/routable"; wire Anthropic, **OpenRouter, Groq**, Ollama, custom | Anthropic, Gemini, Ollama, custom OpenAI-compatible, NVIDIA, LM Studio all wired + async | **OpenRouter and Groq reserved but unwired** (`OPENROUTER_API_KEY`/`GROQ_API_KEY` parsed in `config.py:294/316`, no client/registry entry) — P6 Decision H deferred |
| **P5** Keys | `relay keys create` works end-to-end; upstream keys out of plaintext | Done — store-backed scrypt keys, scopes, rotation, lifecycle, keyring-first upstream keys, `relay keys create/add` | — |
| **P6** Platform DB + availability + usage | Tables incl. `request_log`; **connected applications** (apps = labeled keys × `request_log.api_key_id`, `relay apps` view); status/usage survive restarts | `platform.db` v1–v4 (`api_keys`, learned/telemetry/quality/decision, `model_status`), `relay migrate`, `model_status` 3-state, `relay events`. **`request_log` table and `apps` view not built**; `client_tracking` still in-memory; `availability.json` still the live setup-scan source | P6.4 "usage/apps" sub-phase was **deferred** (`platform-p6-plan.md` §7.1 P6.4: `app/api/apps.py`, `app/cli/apps.py`) — so the M3 marker `relay apps` is unmet |
| **P7** Config management | `relay config show/validate/reload/diff`; secret masking; "all config reachable without editing files" (req 4/18) | Nothing — no `relay config` command. (Reload exists only as the admin API; TUI Configuration panel edits `.env` via `config_store`) | Entire phase |
| **P8** Client guides + quality gate | Cline/OpenCode/Continue setup guides (req 19); one command runs the suite; CI green | CI landed early in P6.4 (`.github/workflows/ci.yml`: compileall + full suite on ubuntu/windows, packaging smoke). `[tool.pytest.ini_options]` present. **No client setup guides** — only README quick-starts and the manual UX-validation procedures | Client guides only |

Net: **5 of 9 phases fully complete**; P1 (CLI subcommands), P4 (OpenRouter/Groq),
P6 (request_log/apps), and P8 (client guides) are partially complete; P7 is
untouched.

---

## 3. Q1 — Which phase was originally intended to define v1.0?

The roadmap never attaches a "v1.0" label to a phase. It defines three optional
milestone cut-lines (`platform-recommended-order.md`):

- **M3 (platform core):** after P4+P5+P6. Marker: `relay keys create` + routed
  chat + **`relay apps` shows it after restart**.
- **M4 (zero-friction UX):** after P1+P2. `relay` → wizard → main interface.
- **M5 (performance & hardening):** after P3+P7+P8. Async hot path, config
  management, client guides, CI green.

The original intent, read against the missing-components report (whose 28
requirements *are* the platform definition) is that **v1.0 = the complete
platform = all of P0–P8, i.e., the M5 cut-line**. M3 and M4 are explicitly
"optional intermediate" cut-lines, not releases of the platform. Supporting
evidence:

- The P6 master plan (§7.5) explicitly defers **P7** (`relay config`,
  TUI panel) and **P8** (CI + client guides) as separate phases beyond P6 —
  i.e., the product was never planned to be "done" at the P6 boundary.
- Requirements 4/18 (configuration without editing files → P7) and 19
  (per-client guides → P8) are part of the approved target design, and are
  unmet today.
- The roadmap's own regression gate for every phase is "existing **821 tests**
  stay green" — written pre-P6.1; the current suite is 1916, so even the
  gate's baseline has moved on.

**Answer: no single phase "was" v1.0. v1.0-as-planned is the M5 milestone
(completion of P0–P8). The current state sits at an M3-ish cut-line.**

---

## 4. Q2 — Are P7–P9 v1-required or post-v1?

Under the original roadmap they are **v1-required**:

- **P7 (config management)** closes requirements 4 and 18 ("all config reachable
  without editing files"). It is described in the recommended-order doc as
  "thin commands over existing reload … polish", i.e. low-risk — but it is still
  in the planned v1 surface, not post-v1.
- **P8 (client guides)** closes requirement 19. CI, the other half of P8, has
  already landed in P6.4 — so P8's remaining work is just the three guides.
- **P6 remainder (request_log + `apps`)** closes requirement 16 and is the only
  unmet part of an explicitly *completed* phase. It is v1-required and,
  strictly, blocks even the **M3** marker (`relay apps` after restart).
- **P4 remainder (OpenRouter/Groq wiring)** is small and was a deferred
  decision (P6 Decision H). Optional for v1; it should be either wired or
  formally dropped (remove the parsed-but-unused key vars).

There is **no post-v1 phase in the original roadmap**. Everything P0–P8 is
pre-v1 by design. The genuine post-v1 candidates are the items already logged
as future improvements (per-app native auth, cross-machine key sync, rate
limiting infra, availability.json retirement is a deferred P6 cleanup, etc.).

**Answer: P7 and P8 are v1-required (P8 is half-done); "P9" is either roadmap
P8 (off-by-one) or the release milestone itself — there is no post-v1 phase in
the plan.**

---

## 5. Q3 — Was the v1.0.0 readiness audit premature?

Relative to the original roadmap: **yes — in scope, not in quality.**

- The audit (`docs/v1.0.0-final-audit.md`) validated the *gateway core*
  (packaging, install, runtime, auth, keys, persistence, migration, docs, CI,
  hygiene) — i.e. the P0–P5 + P6-schema subset — and correctly found it solid.
  Its conclusion ("ready for the v1.0.0 tag, with warnings") is defensible for
  a *gateway* release.
- But roughly half of the planned v1 scope was unbuilt at audit time: P6 apps +
  request_log, P7 config commands, P8 client guides, P4 OpenRouter/Groq. The
  audit did not test against the roadmap's own milestones, and one of its own
  warnings (W1) flags the contradiction: the tag says `v1.0.0` while the
  package version is `0.1.0` and `relay --version` prints `relay 0.1.0`.
- Hardest concrete signal: the **M3** marker is unmet (`relay apps` does not
  exist), yet v1.0.0 tagging was under consideration. Even the *platform-core*
  milestone, let alone M5, is not complete.

So the audit was not wrong about the gateway; it was premature about *calling
that gateway v1.0.0* under the original definition.

**Answer: yes — the audit is valid for a gateway (0.x) release but premature
for the roadmap's v1.0. It should be relabeled as the 0.x gateway GA gate.**

---

## 6. Q4 — Recommended milestone naming

**Do not tag `v1.0.0` now.** Recommended path:

1. **Tag the current gateway state as `v0.1.0`** (aligning the tag with the
   package version and `relay --version`; resolves warning W1). Optionally
   ship it as an intermediate "gateway GA" release.
2. **Reserve `v1.0.0` for the roadmap's M5-equivalent gate**, defined by the
   following checklist (ordered roughly as the roadmap):
   - **P6 remainder:** durable `request_log` (metadata-only) + `apps` derived
     view; `relay apps` / `relay status`; retire in-memory `client_tracking`;
     M3 marker green (`relay keys create` → routed chat → `relay apps` shows it
     after restart).
   - **P7:** `relay config show/validate/reload/diff` + secret masking; config
     surface reachable without editing files (req 4/18).
   - **P8 remainder:** Cline / OpenCode / Continue setup guides + generic
     OpenAI-compatible section (req 19). CI already green from P6.4.
   - **P4 remainder:** wire OpenRouter and Groq, or formally drop the reserved
     keys (P6 Decision H closure).
   - **P6 cleanup:** retire `availability.json` as the live setup-scan source
     (it is already mirrored into `model_status`).
   - **Version bump to `1.0.0` in the same release** as the tag.
3. If a v1 scoped-down to "gateway only" is genuinely preferred over the
   roadmap's platform definition, that is a **deliberate scope re-decision**
   that should be recorded (recommended: in this audit's successor or a rev of
   the roadmap), not a silent tag.

**Answer: stay 0.x and ship the current state as an intermediate `v0.x` GA;
tag `v1.0.0` only when the checklist above (P6 apps, P7, P8 guides, P4
closure) is green — i.e. at the roadmap's M5 cut-line.**

---

## 7. Recommendation summary

| # | Question | Finding |
| --- | --- | --- |
| 1 | Which phase defined v1.0? | None by name; v1.0-as-planned = M5 (all of P0–P8). Current state is an M3-ish cut-line |
| 2 | P7–P9 v1-required? | Yes — P7 (config), P8 (guides; CI done), and the P6 apps/request_log remainder. No post-v1 phase exists in the plan |
| 3 | Was the readiness audit premature? | Yes, in scope: it validated the gateway core as "v1.0" while ~half the planned v1 surface was unbuilt (even M3's `relay apps` marker is unmet) |
| 4 | Recommended naming | Tag the current gateway as `v0.x` (align with package 0.1.0); reserve `v1.0.0` for the M5-equivalent completion checklist in §6 |

Open items that this audit surfaces but does not resolve (each is a decision,
not a code change): renumber/rev the roadmap's regression-gate baseline
(821 → current suite count), and choose the P4 OpenRouter/Groq closure option.
