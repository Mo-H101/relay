# Relay — v1.0 Release Hardening Plan

Status: **planning only — no code changed, no commit, `PROJECT_LOG.md` untouched.**
Date: 2026-08-07
Purpose: take Relay from the post-P9 readiness state (**8/10**) to the
`v1.0.0` tag. This plan converts the findings of
`docs/post-p9-readiness-audit.md`, `docs/platform-p9-phase5-audit.md` (P9e),
and `docs/v1-release-readiness-plan.md` (R1 phases / decisions) into a
concrete hardening work plan. Nothing here is implemented; each item
follows the established workflow: Audit → Plan → Approval → Implementation
→ Tests → Commit.

Scope: **v1.0.0 hardening only.** Explicitly out of scope: new features,
P10 concepts, agents, autonomous execution, and any runtime/API/schema
change beyond the pre-release decision items listed here.

Sources:
- `docs/post-p9-readiness-audit.md` — findings F1–F6, recommendations A1–A10,
  score 8/10.
- `docs/platform-p9-phase5-audit.md` — P9e adversarial closures, dormant-path
  notes, residual risks.
- `docs/v1-release-readiness-plan.md` — release phases R1–R5, decisions
  D1–D10, security/deployment/testing checklists.
- `docs/release-decisions.md` — R1 decision record (D1–D10).
- `docs/blockers-before-public-release.md`, `docs/deployment.md`,
  `docs/release-candidate-checklist.md`, `docs/known-limitations.md`.

Current measured baseline: **2360 passed / 22 skipped** (full suite, R2
final after the §5.2 variant-key coverage; pre-R2 was **2338 / 22** at
P9e, ~173 s), RC offline suite **28** (CI-only; `importorskip("openai")`
in `tests/test_rc_validation.py:25`), adversarial **80**, continuity
simulation **6**, `python -m compileall -q app tests` clean.

---

## 0. Gap-to-goal mapping

The post-P9 audit scores Relay 8/10 with two negative axes: **operational
evidence** (−1) and **documentation lag + open release decisions** (−1).
Each hardening area below closes a scored gap:

| Audit gap (−) | Hardening area | Section |
| --- | --- | --- |
| No live multi-provider soak; dormant overflow/persist paths | Live continuity validation | §1 |
| `architecture.md`, `configuration.md`, v1.0 docs stale | Documentation completion | §2 |
| OpenRouter/Groq unwired (Decision H) | Provider release decision | §3 |
| Continuity deployment profile undefined | Deployment profile | §4 |
| No `validate_resume` rate limit; `FORBIDDEN_KEYS` exact-match; SQLite ceiling | Remaining security improvements | §5 |
| No `LICENSE`/`CHANGELOG`, version/tag mismatch | Release artifacts | §6 |
| 8/10 → gate | Final v1.0 gate checklist | §7 |

Phase mapping into the R1–R5 workflow from `v1-release-readiness-plan.md`:
- **R1 (decisions)** — recorded in `docs/release-decisions.md`; outstanding
  owner choices are **D1** (provider), **D5** (license), **D10** (posture).
- **R2 (code/artifacts)** — §6 artifacts + §3 provider outcome + §5 minimal
  security seams if approved.
- **R3 (verification)** — §1 live continuity validation + full gate.
- **RC1 (release candidate)** — §7 checklist on `v1.0.0-rc.1`.
- **R4 (release)** — version bump + tag + release notes.
- **R5 (post-release)** — upgrade/rollback drill.

---

## 1. Live continuity validation

**Goal:** convert the post-P9 "yes in simulation, unproven live" verdict into
recorded operational evidence. This is a validation/runbook activity; the
scenarios below define *what to run, how, and what passes*. Execution happens
as an approved R3 work item. **No production code change is expected**; any
defect found is filed as a separate fix with its own approval.

Prerequisite: continuity enabled (`CONTINUITY_ENABLED=true`) on a staging
instance with at least two enabled providers; `tests/run_live_smoke.py`
pattern reused for client-side orchestration.

### 1.1 Multi-provider soak

| Item | Definition |
| --- | --- |
| Scenario | N ≥ 300 turns across a single conversation, routed by the decision engine across ≥ 2 providers (e.g. NVIDIA + Anthropic + Gemini when keyed), with `CONTINUITY_ENABLED=true`. |
| Driven by | A client-side driver that issues real chat requests carrying `X-Relay-Conversation-Id` (fixed) and `X-Relay-Project-Id`; records each response. |
| Metrics recorded | `relay_continuity_turns_committed_total`, `continuity_rows_queued` gauge max, `switches_total`, `denials_total`, `resumes_total`, flush failures; per-turn envelope presence via response headers. |
| Mid-run interrupts | Every ~50 turns, hard-kill the Relay process and restart; continue the same conversation id with the last issued resume token. |
| Pass criteria | Zero duplicate work observed (each server `seq` advances monotonically across restarts); no conversation stuck in an operator-visible state after `relay conversations show`; `PRAGMA integrity_check = ok` at the end; privacy negatives hold over `relay conversations list/show` output. |
| Artifact | `docs/v1-live-continuity-validation.md` recording dates, provider set, turn count, kill/restart points, metric deltas, and any filed fixes. |

### 1.2 Long-running project simulation

| Item | Definition |
| --- | --- |
| Scenario | Extend the deterministic in-process simulation (`tests/test_continuity_simulation.py`, 120-turn) to a **staging soak**: multiple conversations in parallel (e.g. 4), each with interleaved provider switches, compactions, resumes, and prune cycles over a sustained period (e.g. 1 hour or 1000+ turns per conversation). |
| Driven by | Existing deterministic harness adapted as a runnable script (not a unit test) so it can run against the staging instance; no new production logic. |
| Assertion contract | Every conversation's seqs are contiguous; summaries reference `up_to_seq` that exists; no conversation ends stuck; `reconcile()` reports healthy or reviewable and never throws; every recorded event passes `contains_never_captured()`. |
| Pass criteria | 100% of conversations finish contiguous; zero replays of acknowledged work; queue gauge bounded (≤ configured cap); flush failures = 0. |
| Artifact | Same validation doc as §1.1 (adds soak results + memory/row-growth figures). |

### 1.3 Forced model switching

| Item | Definition |
| --- | --- |
| Scenario | Force switching via flapping availability: mark the primary provider degraded/unavailable (`POST /admin/reload` or a scripted upstream outage), confirm the `HandoffCoordinator` builds an envelope, the next candidate receives it, `relay:model_switched` SSE is emitted, and switch caps stop a storm. |
| Edge cases | A→B→A oscillation under flapping availability; cap exhaustion records `continuity.denied` + `denials_total`; mid-stream provider death on a streamed request. |
| Pass criteria | Envelope present on post-first attempts (verified via a stub provider capturing the payload); caps enforced end-to-end; turn outcome correct (`ok`/`failed`); no request lost after the winning candidate opens. |
| Artifact | Per-scenario result rows in the validation doc (§1.1). |

### 1.4 Restart recovery validation

| Item | Definition |
| --- | --- |
| Scenario | Walk the S1–S9 matrix from `docs/platform-p9-architecture-design.md:248-258` live: disconnect mid-stream, Relay restart mid-turn, provider failure mid-conversation, compaction over budget, LLM summarizer unavailable, corrupt `platform.db`, scope mismatch, retention prune of inactive, invalid header. |
| Focus | S1/S2 (client resend protocol), S6 (corrupt-file backup-aside-and-reopen), S8 (active conversation never pruned), and the P9e crash-window guarantee (crash between `resume_valid` and envelope hydration → reconcile → resumable). |
| Pass criteria | Every scenario's documented behavior reproduces live; no stuck state; audit rows present with correct outcomes; the fail-closed resume path returns a denial (not an error) when `resume_replays` cannot be persisted. |
| Artifact | S1–S9 live result matrix in the validation doc (§1.1). |

### 1.5 Dormant-path disposition (decision, not code)

The overflow-retry path (`ContextOverflowSignal` /
`should_retry_compacted`, `context_manager.py:318-334`) and
`summarize_and_persist` (`summarizer.py:256-361`) have **no production call
site** (tests only). Two options, to be recorded:

- **(a) Accept for v1.0.0** — document as reserved paths in the release
  notes; the request never fails on compaction today because preflight
  compaction via the envelope builder is the live path (`handoff.py:673`).
- **(b) Wire before v1.0.0** — a code change (separate approval): raise the
  overflow signal on a context-overflow error and persist compactions when
  they occur.

Recommendation: **(a)** — accept for v1.0.0 and record. The live soak (§1.1)
observes whether compaction occurs and whether overflow retries would ever
have fired; if the soak shows overflow in practice, promote (b) to a
post-v1.0.1 fix rather than expanding the v1.0 code surface.

---

## 2. Documentation completion

**Goal:** close the documentation-lag axis (audit F3, A3/A4).

### 2.1 Architecture docs

- **`docs/architecture.md`** — add the continuity layer to the layer diagram
  and request flow (named by `platform-p9-implementation-plan.md:90` but not
  yet done): `ConversationStore`/`ContinuityFlusher`/`ContextManager`/
  `HandoffCoordinator`/`ContinuityRecovery` under the facade, the
  `CONTINUITY_ENABLED` gate, the two provider-facing flows (envelope injection
  `handoff.py:167-188`; optional LLM summarizer `summarizer.py:191-209`),
  and the SQLite boundary (writes only on the flusher thread).
- **`docs/platform-db-schema.md`** — verify it matches schema v8 (it does,
  committed in P9e) and add a note that continuity tables are written only
  when `CONTINUITY_ENABLED=true`.

### 2.2 Configuration docs

- **`docs/configuration.md`** — add the 9 missing env vars (W2:
  `ANTHROPIC_ENABLED`/`ANTHROPIC_BASE_URL`/`ANTHROPIC_MODEL_PRIORITY`,
  `GEMINI_ENABLED`/`GEMINI_BASE_URL`/`GEMINI_MODEL_PRIORITY`,
  `OLLAMA_ENABLED`/`OLLAMA_MODEL_PRIORITY`, `RELAY_DATA_DIR`) and the full
  `CONTINUITY_*` / `MAX_SWITCHES_*` / `MAX_RESUME_REPLAYS` surface
  (`app/core/config.py:784-845`). Cross-check every var against
  `config_spec.py` (103 specs) and `.env.example`.
- Correct the stale claim that Anthropic is unused (D7/D8 — it is a wired
  runtime provider).

### 2.3 Continuity documentation

- **`docs/deployment.md`** — add the continuity-enabled profile block (§4),
  the single-process storage model (§4.2), and a resume-token lifecycle note
  (single-use, durable replay cap, retention pruning).
- **`docs/clients/*`** — optional: note continuity headers
  (`X-Relay-Conversation-Id` / `X-Relay-Project-Id`) as additive per-client
  options; default off, no client change required.
- **Release notes** — describe continuity as an opt-in capability (flag
  default off), the resume protocol, and the "no progress lost = committed
  turns" contract (§3 of the post-P9 audit).

### 2.4 v1.0 docs refresh

- **`docs/v1.0.0-final-audit.md`** and **`docs/v1.0.0-readiness-report.md`** —
  renumber the P6.4 baseline (1916/18) to the current measured baseline
  (2338/22) and note P7–P9 were completed after those documents were signed.
- **`docs/platform-implementation-roadmap.md`** — renumber the regression gate
  (821 → 2338) per D8.

---

## 3. Provider release decision

### 3.1 OpenRouter / Groq (D1)

- **Current state:** `OPENROUTER_API_KEY` (`config.py:294`) and `GROQ_API_KEY`
  (`config.py:316`) parsed and listed as secret restart fields
  (`config_spec.py:408,423`), labeled "reserved" in `.env.example:125-138`;
  **no client or registry entry** — the runtime registry wires exactly six
  providers (`app/providers/registry.py:80-170`).
- **Options:** (a) wire both (registry entries + OpenAI-compatible clients +
  tests); (b) drop the reserved keys (remove from `config.py`,
  `config_spec.py`, `.env.example`, re-audit `configuration.md`); (c) keep
  as-is (rejected: misleading dead surface, flagged by two audits).
- **Recommendation:** **(b) drop for v1.0.0**, defer wiring post-v1. No
  production key exists for either; the OpenAI-compatible surface is already
  validated through NVIDIA/OpenAI/LM Studio; removing dead config shrinks the
  release surface and closes the last P4 Decision-H residue.
- **Final decision:** REQUIRES YOUR CHOICE — `[ ] wire (a) / [ ] drop (b)`
  *(choose b, or override)*.

### 3.2 Supported provider list for v1.0.0

| Provider | Registry id | Client | Status at v1.0.0 |
| --- | --- | --- | --- |
| NVIDIA | `nvidia` | `nvidia_client` (OpenAI-compat) | **Supported** — live-validated 6/6 |
| OpenAI | `openai` | `openai_client` (OpenAI-compat) | **Supported** — blocked on account quota (B1), otherwise validated |
| Anthropic | `anthropic` | `anthropic_client` | **Supported** — wired, conformance-tested |
| Google Gemini | `gemini` | `gemini_client` | **Supported** — wired, conformance-tested |
| LM Studio | `lmstudio` | `lmstudio_client` | **Supported** (local) — real-endpoint tests skip by design (W6) |
| Ollama | `ollama` | `ollama_client` | **Supported** (local) |
| OpenRouter | — | — | **Not in v1.0.0** (D1: drop or defer) |
| Groq | — | — | **Not in v1.0.0** (D1: drop or defer) |

Release-notes statement: "v1.0.0 supports six providers: NVIDIA, OpenAI,
Anthropic, Google Gemini, LM Studio, and Ollama. OpenRouter and Groq are
reserved for a post-v1 release."

---

## 4. Deployment profile

### 4.1 Recommended production setup

Add to `docs/deployment.md` the **full hardened profile** (superset of the
existing block at `deployment.md:50-68`, adding continuity):

```dotenv
# --- Identity & transport ---
RELAY_API_KEY=<long-random-value>
RELAY_AUTH_STORE=true                  # per-client scoped keys (D6)
HOST=0.0.0.0                           # TLS at the reverse proxy

# --- Persistence (single SQLite file) ---
PERSISTENCE_ENABLED=true
PERSISTENCE_PATH=/var/lib/relay/platform.db
PERSISTENCE_FLUSH_INTERVAL_SECONDS=60
PERSISTENCE_RETENTION_DAYS=30

# --- Routing & learning ---
HEALTH_FEEDBACK_ENABLED=true
TELEMETRY_ENABLED=true
HEALTH_AWARE_ROUTING=true
HEALTH_REFRESH_ENABLED=true
HEALTH_REFRESH_INTERVAL_SECONDS=60

# --- Providers (only the six supported) ---
NVIDIA_ENABLED=true
OPENAI_ENABLED=true
NVIDIA_MODEL_PRIORITY=<account-invocable-ids>    # D4: pin at deploy time
OPENAI_MODEL_PRIORITY=<account-invocable-ids>    # D4: pin at deploy time

# --- Provider keys ---
RELAY_KEYRING=true
# RELAY_KEYRING_BACKEND=<dotted.module.Class>    # headless servers

# --- Retry hardening (D3) ---
RETRY_HONOR_RETRY_AFTER=true
RETRY_AFTER_MAX_SECONDS=60
RETRY_BACKOFF_BASE_SECONDS=1
REQUEST_TIMEOUT_BUDGET_SECONDS=120

# --- Continuity (opt-in) ---
CONTINUITY_ENABLED=true
CONTINUITY_RETENTION_DAYS=30
CONTINUITY_FLUSH_INTERVAL_SECONDS=5
CONTINUITY_CONTEXT_TOKEN_BUDGET=32768
CONTINUITY_OUTPUT_RESERVE_TOKENS=2048
CONTINUITY_SUMMARY_SHARE=0.4
CONTINUITY_SUMMARY_MAX_CHARS=4096
CONTINUITY_TAIL_MAX_ITEMS=20
CONTINUITY_CHARS_PER_TOKEN=4
# CONTINUITY_SUMMARIZER_MODEL=<model-id>         # empty = extractive only (default)
MAX_SWITCHES_PER_TURN=3
MAX_SWITCHES_PER_WINDOW=5
MAX_RESUME_REPLAYS=3
```

Deployment notes:
- **Run exactly one Relay process** (single-writer SQLite; no `--workers N`)
  — carried forward from `deployment.md:82-90`.
- **Enable continuity only when intended**: the flag is `false` by default;
  enabling it is additive (headers, SSE `relay:*` events, new tables) and
  older clients ignore it.
- If the LLM summarizer is desired, set `CONTINUITY_SUMMARIZER_MODEL` and
  ensure that model id is invocable (add it to a priority list); every
  failure degrades to the extractive path.

### 4.2 Supported storage model

| Aspect | Model |
| --- | --- |
| Database | Single SQLite file `platform.db` (schema **v8**), WAL + `busy_timeout 5000`, `0600` + sidecars, corrupt-file backup-aside-and-reopen |
| Writer | Single guarded connection per process; continuity writes only on the `ContinuityFlusher` thread; hot-path reads bounded (single-row `last_turn` + `resume_envelope` hydration) |
| Process model | Single process, single writer; no horizontal scale (scale by isolated instances with separate `PERSISTENCE_PATH`) |
| Backup | `relay migrate` backup/rollback copies `platform.db` whole; operators back up `state_dir/` (db + `-wal`/`-shm` + `.env`) |
| Retention | `CONTINUITY_RETENTION_DAYS` (30) + `PERSISTENCE_RETENTION_DAYS` (30); active conversations never pruned (S8) |
| Migration | Additive v6→v7→v8, idempotent, guarded by `PRAGMA user_version`; newer-version files refused; `relay migrate --rollback` restores backups |

### 4.3 Resource requirements

| Resource | Estimate | Notes |
| --- | --- | --- |
| CPU | 1 core nominal | Async I/O (`httpx.AsyncClient`); health refresher probes on an interval; no heavy local compute |
| Memory | ~100–300 MB | FastAPI/uvicorn + in-memory coordinator (≤ 512 states, LRU) + flusher queue (≤ 10,000 rows, bounded) + ops/metrics windows |
| Disk | < 1 GB sustained | Metadata-only rows; growth bounded by retention pruning; `platform.db` + WAL sidecars; `.env` + keyring |
| Network | 2+ provider endpoints | Outbound to provider base URLs; inbound behind a reverse proxy (TLS) |
| OS keyring | Required for at-rest provider secrets | `RELAY_KEYRING_BACKEND` on headless servers; `.env` fallback must be disk-encrypted + ACL'd (W3) |

---

## 5. Remaining security improvements

**Goal:** close the audit security notes (F5/A5/A6) with **minimal, additive
seams** — each is a separate approved item; the default posture for v1.0.0
is record-and-decide where a code change is not mandatory.

### 5.1 Rate limiting on `validate_resume` (A6)

- **Finding:** no per-key rate limit on the resume path itself; protection
  relies on 256-bit conversation/token space + the durable replay cap
  (`MAX_RESUME_REPLAYS`, default 3).
- **Options:**
  - (a) Reuse an existing per-key rate-limit seam (documented in P9
    architecture §11) as a config-guarded counter on `validate_resume` —
    small code change, additive, off by default.
  - (b) Document-only: the replay cap + fail-closed store behavior already
    bound abuse; defer a dedicated limiter.
- **Recommendation:** **(b) for v1.0.0** (document-only), revisit post-v1 if
  the live soak (§1.1) shows resume-path abuse. If the release owner wants
  defense-in-depth now, (a) is a bounded change.

### 5.2 `FORBIDDEN_KEYS` improvement (A5)

- **Finding:** `FORBIDDEN_KEYS` is exact-match after lower-casing
  (`memory_contract.py:107`); variants (`prompt_text`, `user_message`,
  `secret_value`, `model_response`) are not caught. Enforcement is by
  negative tests, not a structural write-time filter.
- **Options:**
  - (a) **v1.0.0:** extend `FORBIDDEN_KEYS` with the documented variant list
    and add negative tests for each — small, additive, no behavior change for
    the current surface (test-only coverage growth).
  - (b) Post-v1: a structural write-time filter (substring/prefix matcher)
    applied at every store/logger boundary — larger change, deferred.
- **Recommendation:** **(a)** — extend the key list + tests in R2; record (b)
  as post-v1. The P9e summary/header surfaces already pass
  `contains_never_captured()`; this hardens the backstop.

### 5.3 SQLite scaling considerations

- **Findings:** single-writer single-file SQLite is the durability ceiling;
  no multi-instance or remote-storage story; reconcile scans bounded at 5000
  conversations; coordinator bounded at 512 in-memory states.
- **v1.0.0 stance (document, do not build):**
  - Document the single-process limit in `deployment.md` (§4.2) and
    `known-limitations.md` (new item): a second process against the same
    `platform.db` is unsupported; scale by isolated instances.
  - Document `CONTINUITY_REFRESH`/reconcile and coordinator caps in
    `configuration.md` so operators understand bounds at large conversation
    counts.
  - WAL checkpointing: operators may run `PRAGMA wal_checkpoint(TRUNCATE)`
    during maintenance; documented, not automated.
- **Post-v1 candidates (recorded, not scoped):** WAL archive/offline backup
  (`sqlite.org/backup.html`), a multi-instance storage backend, and
  server-side snapshot for client checkpointing (post-P9 audit
  recommendation R5).

---

## 6. Release artifacts

### 6.1 LICENSE (B5, D5, D10)

- **State:** no `LICENSE`, no `[project] license` metadata, no
  `THIRD_PARTY_LICENSES.md`; all runtime/dev deps are permissively licensed.
- **Actions:**
  1. Decide **D10 (posture)** first: (a) personal/self-hosted, (b)
     open-source community, (c) commercial/product.
  2. Decide **D5 (license)**: MIT (recommended if community/personal),
     Apache-2.0, GPL/AGPL, or proprietary. **REQUIRES YOUR CHOICE.**
  3. Add `LICENSE` at repo root; add `license = {...}` + SPDX classifier to
     `pyproject.toml`.
  4. Generate `THIRD_PARTY_LICENSES.md` from `.venv` `.dist-info` files at
     R2 (all permissive — fastapi/httpx/uvicorn BSD-3; pydantic/rich/textual/
     platformdirs/keyring/dotenv/build/setuptools/wheel MIT;
     pytest-asyncio/openai Apache-2.0).
  5. Record contributor/CLA-or-DCO policy matching D10.

### 6.2 CHANGELOG.md (B6)

- New `CHANGELOG.md` in Keep-a-Changelog format:
  - `[1.0.0]` entry — highlights across P0–P9 (packaging, TUI, async hot
    path, six providers, key security, platform DB, config registry, client
    guides, **project continuity P9a–P9e**), plus the two release notes
    statements from §3.2 (providers) and §2.3 (continuity opt-in).
  - "Unreleased" section for post-v1 notes.
- The D2 CLI deviation note goes here too: the TUI supersedes the originally
  planned `status/providers/models/routing/logs/test` subcommands; `relay
  events` is the log surface.

### 6.3 Version bump (B4 / W1)

- `app/__version__.py` `"0.1.0"` → `"1.0.0rc1"` (Phase R2) → `"1.0.0"`
  (Phase R4). `pyproject.toml` derives version dynamically; `relay
  --version` must print `relay 1.0.0`.
- **The `v1.0.0` tag must land in the same change as the `1.0.0` bump.**

### 6.4 Release notes

- GitHub release notes mirroring `CHANGELOG.md`, adding:
  - Provider list (§3.2) and the OpenRouter/Groq outcome (§3.1).
  - Continuity feature summary + default-off flag + "committed turns" contract.
  - Production profile reference (§4.1).
  - Security checklist execution statement (§5) and the at-rest-secrets
    decision (W3).
  - Known limitations (carried from `known-limitations.md`).

### 6.5 PyPI decision (D6)

- Decide publish vs. git/installer-only. If publish: add classifiers,
  `long_description`, license metadata, and a publish CI job (separate
  approved change). Recommendation: defer publishing until the tag is cut;
  the installer path is already validated.

---

## 7. Final v1.0 gate checklist

All items must be complete before the `v1.0.0` tag. Supersedes/extends
`v1-release-readiness-plan.md` §12 with the post-P9 additions.

### Blockers
- [ ] **B1** OpenAI quota restored; `python tests/run_live_smoke.py` 6/6 vs
      OpenAI (or explicit NVIDIA-ready-only release decision).
- [ ] **B2/B3** Deployed profile authenticated (`RELAY_API_KEY` +
      `RELAY_AUTH_STORE=true`) and persistent (`PERSISTENCE_ENABLED=true` +
      `PERSISTENCE_PATH`).
- [ ] **B4** Version `1.0.0` and tag `v1.0.0` in the same change.
- [ ] **B5** License decided (D5/D10), `LICENSE` + `pyproject.toml` metadata +
      `THIRD_PARTY_LICENSES.md`.
- [ ] **B6** `CHANGELOG.md` + release notes.

### Decisions
- [ ] **D1** OpenRouter/Groq wire-or-drop recorded (recommend drop).
- [ ] **D2** CLI deviation recorded in release notes.
- [ ] **D3** Retry hardening profile enabled in `deployment.md`.
- [ ] **D4** Model priorities pinned for the deploying account.
- [ ] **D5/D10** License + posture decided.
- [ ] **D7** Env-retirement audit executed (bounded cleanup in R2).
- [ ] **D8** Roadmap gate baseline renumbered (821 → 2338).
- [ ] **D9** Plan/audit docs dispositioned (recommend: commit with the release).
- [ ] §1.5 Dormant-path disposition recorded (recommend: accept for v1.0.0).

### Live continuity validation (§1)
- [ ] Multi-provider soak run, pass criteria met, `docs/v1-live-continuity-validation.md` written.
- [ ] Long-running project simulation (staging) pass criteria met.
- [ ] Forced model-switching scenarios pass (envelope, `relay:model_switched`, caps).
- [ ] S1–S9 restart-recovery matrix passes live.
- [ ] No stuck state; `integrity_check` ok; privacy negatives hold.

### Documentation (§2)
- [ ] `architecture.md` continuity layer added.
- [ ] `configuration.md` complete (W2 vars + continuity surface; Anthropic claim fixed).
- [ ] `deployment.md` continuity profile + storage model + resources.
- [ ] `v1.0.0-final-audit.md` / `v1.0.0-readiness-report.md` renumbered to 2338/22.
- [ ] `known-limitations.md` updated (single-process SQLite; resume protocol).

### Security (§5)
- [ ] §5.1 resume-path rate-limit decision recorded (recommend document-only).
- [ ] §5.2 `FORBIDDEN_KEYS` variant coverage + negative tests (if (a) chosen).
- [ ] §5.3 SQLite scaling limits documented.
- [ ] Release-candidate security checklist executed (`v1-release-readiness-plan.md` §6), including the adversarial review stage.

### Deployment (§4)
- [ ] Production profile (§4.1) applied and verified on the candidate
      (`/diagnostics` shows auth on, persistence available).
- [ ] Fresh install Windows + Linux; wheel install smoke; `relay --version`
      → `1.0.0`.
- [ ] Upgrade path (`relay migrate` from legacy 0.x) + rollback drill.

### Quality gate
- [ ] Full suite green: **2360 passed / 22 skipped** (R2 re-run actual; the
      tag actual is re-recorded at RC1/R4).
- [ ] RC offline suite green: **28 passed** (CI, where `openai` is installed).
- [ ] Adversarial (80) + simulation (6) suites green.
- [ ] CI green on the tag (ubuntu 3.10/3.11/3.12/3.13, windows 3.12, packaging job).
- [ ] `python -m compileall -q app tests` clean.
- [ ] Known timing flake dispositioned.

### Release
- [ ] `v1.0.0-rc.1` tagged, RC1 stage passed (install/setup/upgrade +
      adversarial review), then `v1.0.0` cut in the same change as the
      `1.0.0` bump.
- [ ] GitHub release notes published.
- [ ] `PROJECT_LOG.md` updated at the release commit (workflow step, per
      `platform-p9-implementation-plan.md:65`).
- [ ] Post-release: upgrade/rollback drill 0.x → 1.0.0 (R5).

---

## Stop condition

This plan is delivered for approval. On approval: R1 decisions (D1/D5/D10)
are confirmed, then R2 (artifacts + §3/§5 outcomes), R3 (verification incl.
§1 live validation), RC1, R4, R5 proceed per the established workflow. No
code, no commit, `PROJECT_LOG.md` untouched until the release commit.
