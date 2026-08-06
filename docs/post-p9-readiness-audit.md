# Relay — Post-P9 Readiness Audit

Date: 2026-08-07
Status: **Audit / analysis document — no code changed, no commits,
`PROJECT_LOG.md` untouched.**
Scope: the entire project state after P9a (foundation), P9b (context
manager), P9c (handoff), P9d (recovery), P9e (adversarial hardening).
Method: verified reads of `app/` and `tests/` plus the phase documents
listed in each section; every claim carries a `file:line` reference.

Headline result: the P9 layer meets its definition of done
(**2338 passed / 22 skipped**, RC suite 28, adversarial + simulation
suites landed), the P10 boundary holds, and the continuity design answers
every external-tool failure mode studied in P9e. The gap between P9's
*nominal* continuity and a *proven* one is **operational validation, not
design**: no live multi-model/multi-provider soak has been run, the
LLM-summarizer and overflow-retry paths have no production call site, and
several documented surfaces (`architecture.md`, readiness docs) are stale.

Release-readiness score: **8 / 10** (see §7.3).

---

## 1. Roadmap status

### 1.1 P0–P9 completion (verified)

| Phase | Scope | Status | Evidence |
| --- | --- | --- | --- |
| P0 | Packaging & distribution | ✅ complete | `pyproject.toml`, console script, installers |
| P1 | First-run experience & CLI | ✅ complete (deviation documented) | setup wizard, `relay` subcommands; P1 subcommand list superseded by the TUI (noted in `docs/roadmap-post-p7-audit.md:92`) |
| P2 | Terminal UI (Textual) | ✅ complete | 9 screens, wizard→TUI handoff |
| P3 | Async provider layer | ✅ complete | async `/v1`, streaming, `httpx.AsyncClient` hot path |
| P4 | Provider integrations | ✅ complete (one open decision) | 6 providers in `PROVIDER_REGISTRY`, `RUNTIME_READY`; OpenRouter/Groq keys parsed but unwired (Decision H, `docs/roadmap-post-p7-audit.md:89`) |
| P5 | API-key security | ✅ complete | scrypt-hash keys, scopes, keyring-first provider keys |
| P6 | Platform DB + availability + usage | ✅ complete | migrations framework, `model_status`, `request_log`, `events`, `apps` |
| P7 | Configuration management | ✅ complete | config registry (103 specs), `relay config set/unset/reload/diff`, TUI panel |
| P8 | Client guides + CI | ✅ complete | `docs/clients/` (cline, continue, opencode, openai-compatible, index); CI workflow |
| P9a | Continuity foundation & schema v7 | ✅ complete | commit `e8cb667`; `ConversationStore`, `ContinuityFlusher`, facade wiring |
| P9b | Context manager & summarizer | ✅ complete | commit `26ada25`; `ContextManager`, `summarizer`, `summary_verifier` |
| P9c | Handoff coordinator | ✅ complete | commit `18e94da`; `HandoffCoordinator`, header plumbing, SSE events |
| P9d | Recovery, retention & surfaces | ✅ complete | commit `81e88f2`; `ContinuityRecovery`, `resume_replays` durability, CLI/TUI surfaces |
| P9e | Adversarial hardening | ✅ complete | commit `dbc2902`; 80 adversarial tests + 6 simulation tests, F-1/F-3 resolved |

### 1.2 Remaining work before v1.0

**Blocking the tag (must do):**

- **Version bump.** Package is `0.1.0` (`app/__version__.py:1`); the
  v1.0.0 tag must be paired with a bump to `1.0.0` (carried forward from
  v1.0.0-final-audit warning W1).
- **Live two-provider validation.** OpenAI quota blocker (B1 in
  `docs/blockers-before-public-release.md`) still leaves the gateway
  NVIDIA-only in practice. Re-run `tests/run_live_smoke.py` when the key
  has quota.
- **PROJECT_LOG.md update** — explicitly deferred to the final release
  commit by the approved P9 workflow (`docs/platform-p9-implementation-plan.md:65`).

**Required before or at the gate (documented warnings):**

- W2 — `docs/configuration.md` still omits 9 env vars (`ANTHROPIC_*`,
  `GEMINI_*`, `OLLAMA_*`, `RELAY_DATA_DIR`); `.env.example` is complete.
- W4 — deployed-profile decisions: pin `*_MODEL_PRIORITY`, decide
  retry-hardening knobs (`RETRY_HONOR_RETRY_AFTER`), ship with
  `RELAY_API_KEY` and `PERSISTENCE_ENABLED=true`.
- P4 Decision H closure: wire OpenRouter/Groq or drop the reserved keys.
- New for P9: **`CONTINUITY_ENABLED` deployment decision** — default is
  `false` (`app/core/config_spec.py:520`); a continuity-enabled deployed
  profile is a documented operator choice, not shipped-by-default.

**Deferred / non-blocking:**

- W5 deprecated provider shims (`nvidia.py`, `openai.py`, `lmstudio.py`);
  W7 untracked plan docs (by policy); W8 stale registry comment; F1–F5
  post-release items; P10 (out of scope for v1.0.0 by design,
  `docs/platform-p9-architecture-design.md:516-558`).

---

## 2. Architecture review

### 2.1 Current Relay architecture

The layering is unchanged and consistent:
`api routers → Relay facade (app/core/relay.py) → services →
providers (httpx clients)` with `app/core/config.py` as the dependency
root (`docs/architecture.md:5-17`). The P9 layer slots in as a set of
services owned by the facade, gated by one flag.

- **Hot path (P3):** `Relay.achat` (`relay.py:395`) and `Relay.chat`
  (`relay.py:340`) both: `provider_manager.ranked()` →
  `candidate_builder.build()` → (optional) `decision_engine.decide()` →
  `begin_continuity_turn()` → `chat_service.chat_across` /
  `async_chat_service.achat_across`.
- **Continuity wiring:** `Relay._init_continuity` (`relay.py:117-142`)
  builds `ConversationStore`, `ContinuityFlusher`, `ContinuityRecovery`,
  `HandoffCoordinator` only when `settings.continuity_enabled`
  (`relay.py:108`). When off, continuity is inert and byte-identical
  (pinned by `tests/test_continuity_parity.py`).
- **Lifespan:** flusher start/prune at startup, reconcile at startup,
  final flush + stop at shutdown (`app/main.py:40-86`).

### 2.2 Continuity layer boundaries (verified)

| Boundary | Enforcement | Evidence |
| --- | --- | --- |
| **Wire** | Continuity engages only when the flag is on, a store-backed key id is present, and a continuity header is sent | `continuity_headers.py:121-185`; `auth.py:240,323` |
| **Facade** | `core.relay.py` is the only bridge between HTTP and continuity internals; chat services see an opaque `TurnContext` | `relay.py:144-207`; `handoff.py:121-122,151-152` |
| **SQLite** | Writes only on the flusher thread; the only chat-path exceptions are the bounded read-only `last_turn` and `resume_envelope` hydration reads | `continuity_flusher.py:7-8`; `continuity_recovery.py:24-27,331-356` |
| **Key scope** | Every store/recovery operation re-validates `key_id`; unknown ids proceed as a fresh conversation (no oracle) | `conversation_store.py:14-18,211-232`; `handoff.py:317-320` |
| **Memory contract** | 8 durable continuity surfaces; `never` class unchanged; `FORBIDDEN_KEYS` forces the `summary_text` export name | `memory_contract.py:45-52,74-94`; `models/continuity.py:276` |

### 2.3 Provider routing boundaries

- Routing is strictly **selection** (one model per request), decoupled
  from continuity: `CandidateBuilder` / `CandidateScorer` / `DecisionEngine`
  inputs are routing, health, telemetry, quality only — no continuity data
  (`candidate_builder.py:86-129`; `scoring.py:69-84`).
- Continuity sits **downstream of selection**: it wraps the failover loop
  (switch caps), injects the envelope, and records turn metadata.
- Two by-design cross-boundary flows to be explicit about:
  1. **Envelope → provider payload**: `inject_payload` inserts a synthetic
     `system` message; `inject_message` prefixes the prompt
     (`handoff.py:167-188`). This is conversation-derived data (summary +
     tail + conversation id + model chain) reaching the provider — mitigated
     by the P9e data-marking frame and instruction-shape guard.
  2. **LLM summarizer → provider layer** when
     `CONTINUITY_SUMMARIZER_MODEL` is set (default empty → extractive only):
     `summarizer.py:191-209`.
- **Observed anomaly (report, not a defect):** on the hot path the decision
  engine is invoked for statistics and its return value is discarded —
  `relay.py:370-371` and `relay.py:424-425` call `self.decision_engine.decide(...)`
  without using the result. Selection still comes from `candidates`.
  The engine's own docs state its selection is identical to the hot path
  (`relay.py:311-314`), so this is observability-only by design; flagging
  it here because a future "use the decision result" change would be
  behaviorally significant.

### 2.4 Dormant paths (verified)

Two fully-implemented paths have **no production call site** (tests only):
`ContextOverflowSignal` / `should_retry_compacted`
(`context_manager.py:318-334`) and `summarize_and_persist`
(`summarizer.py:256-361`). `compact()` itself is reached on the request
path through the envelope builder (`handoff.py:673`); the overflow-retry
and persist-on-compact paths are not wired to production triggers.

---

## 3. Original goal verification

> **"Can Relay maintain a large project across multiple models/providers
> without losing progress?"**

**Honest answer: Yes, within the defined scope, with one important
qualification.**

What is proven (tests + design):

- **Conversation continuity across disconnects and restarts.** Durable,
  key-scoped conversation identity; the 7-state recovery machine re-derives
  state from the store at startup, so nothing wedges (`models/continuity.py:35-83`;
  `continuity_recovery.py:382-461`). Pinned by crash-window and
  stuck-state-restart tests (`tests/test_continuity_adversarial.py`,
  `tests/test_continuity_recovery.py`).
- **Continuity across provider/model switches.** The context envelope
  carries summary + tail across a failover; switch caps bound the walk;
  acknowledged work is never repeated (`exclude_up_to_seq`,
  `continuity_recovery.py:333`). Pinned by handoff + 120-turn simulation
  tests (`tests/test_continuity_handoff.py`,
  `tests/test_continuity_simulation.py`).
- **Context compaction without losing the thread.** Budget-constrained
  summary + tail; the summary is derived, redacted, verified, and delivered
  as labelled data (`handoff.py:214-251`; `summary_verifier.py`).
- **Resume without duplicate work.** Single-use tokens, durable replay
  counter (schema v8), fail-closed validation
  (`conversation_store.py:662-702`; `continuity_recovery.py:221-311`).

The qualification — **what "no lost progress" does and does not mean**:

1. **Granularity is the committed turn, not the in-flight turn.** An
   interrupted in-flight turn is ephemeral by design (S2, S1); the client
   re-sends from the last committed turn. "No progress lost" means no
   *committed* progress is lost, not that a mid-turn crash is free.
2. **Proven in simulation, not yet under live multi-model load.** The
   120-turn simulation is deterministic and in-process
   (`tests/test_continuity_simulation.py`); no live soak across several
   real providers has been run (the live smoke is single-provider NVIDIA).
3. **"Large project" is scoped to conversation continuity.** Relay holds
   conversation/project *metadata*, not repository state. It does not
   index files, track diffs, or reconstruct a workspace after a crash —
   the continuity guarantee is "the conversation's context survives
   provider changes and restarts," which is the correct unit for a proxy
   that the client keeps its own state for.
4. **Extractive-by-default summaries are lossy by contract.** With the LLM
   summarizer off (default), compaction is deterministic metadata
   condensation, not semantic understanding; the guarantee is bounded,
   verified, labelled context — not full-fidelity history.

**Verdict:** the architectural answer is yes; the empirical answer is
"yes in simulation, unproven in a live multi-provider soak." Close the
gap with an operational soak (§7.2 action A1) before advertising the
capability.

---

## 4. Comparison against the six tools

P9e already studied how each tool fails and mapped Relay's structural
answer (`docs/platform-p9-phase5-audit.md` §2, §3.7). This section frames
the *positional* comparison honestly.

| Tool | Relay's relation | Where Relay is stronger | Where Relay is weaker / different |
| --- | --- | --- | --- |
| **OpenCode** | Same user problem (session continuity) | Read-only reconcile + non-persisted denials — recovery cannot brick the session (`continuity_recovery.py:382-461`) | No client-side session UI; relay keeps no git/checkpoint model; tool-call state is the client's |
| **Codex** | Same user problem (resume) | No persisted in-progress flag → nothing wedges (F-4, pinned) | No `resume` CLI with human workflow; no per-task JSONL transparency |
| **Cline** | Same user problem (task continuity) | Structured rows + self-healing open vs a single JSON blob; 128-byte header fuzz rejects malformed input | No incremental backups/auto-restore of a *task* file; continuity is metadata, not a task plan |
| **Continue** | Same user problem (autocompaction) | Reject-not-truncate verification; `CONTINUITY_OUTPUT_RESERVE_TOKENS` centralizes the budget | Fewer context-provider integrations; single summary+tail scheme vs pluggable context providers |
| **Aider** | Different position — Aider is a *git-native coding agent*; Relay is a *gateway/proxy* | WAL + `UNIQUE` + flusher safety net; detect-only reconcile (never silently rewrites state) | No git integration, no undo/rollback of edits — Relay never edits files (P10 boundary), so the comparison is architectural, not feature-for-feature |
| **SWE-agent** | Different position — SWE-agent is a *research harness* | Fail-closed anomaly detection; no replay loop possible | No trajectory replay tooling; no benchmark harness in the product |

**Honest positioning statement:** the five client tools (OpenCode, Codex,
Cline, Continue) and the harness (SWE-agent) are *agents*; Relay is a
*routing + continuity proxy* that those clients sit in front of. Relay
cannot and does not "do the work" — it makes the work survivable across
providers and restarts. The correct comparison is therefore not "does
Relay match their features" but "does Relay close the specific
loss-of-progress failure modes they exhibit" — which P9e verified it does.
The one area where an agent natively beats a proxy is *self-managed
checkpointing* (git/JSONL replay): clients carry that burden today, and a
future Relay-side snapshot could close it (recommendation R5).

---

## 5. Remaining weaknesses

### 5.1 Reliability

- **No live multi-provider soak.** Deterministic simulation only; no
  real cross-provider failover/resume soak has been run (affects §3.2).
- **Overflow-retry and persist-on-compact are dormant.** The retry-once
  overflow path and `summarize_and_persist` have no production caller; the
  most complex context logic is exercised by tests, not by a live trigger.
- **Single-writer single-file SQLite is the durability ceiling.** WAL +
  `busy_timeout 5000` is correct for a local gateway, but there is no
  multi-instance or remote-storage story; a second process pointing at the
  same `platform.db` is unsupported (documented single-process constraint).
- **In-flight turn loss is by design** but should be surfaced to clients
  clearly (S1/S2); the SSE resume protocol already handles resend.

### 5.2 Security

Carried, unchanged by P9, and verified in §2:

- **Auth is off by default** (`RELAY_API_KEY` empty → no auth). Known and
  documented (`docs/known-limitations.md:85`); a deployed profile must set it.
- **`FORBIDDEN_KEYS` is exact-match after lower-casing**
  (`memory_contract.py:107`); variants like `prompt_text` or `secret_value`
  are not caught. The contract is enforced by negative tests, not a
  structural write-time filter.
- **No rate limiting on `validate_resume` itself** (global per-key seams
  exist per architecture §11); protection relies on 256-bit keys + replay cap.
- **Keyring failure silently degrades** to env/empty
  (`provider_key_store.py:42-45`) — practical blast radius is "provider
  unavailable", but the degradation is silent.
- **Provider keys rely entirely on the OS keyring backend** for at-rest
  encryption (no Relay-side encryption; multi-user host caveat, W3).
- **KeyStore singleton never closed** in normal lifecycle
  (`auth.py:206-220`) — a process-lifetime open SQLite connection (low risk).

### 5.3 Scalability

- **In-memory coordinator cap of 512 states** with LRU eviction
  (`handoff.py:45,762-768`) — bounded, but under sustained many-conversation
  load older conversations lose in-memory context until re-derived.
- **Reconcile scan limit of 5000 conversations** (`continuity_recovery.py`)
  bounds startup work but means very large installs may need multiple passes.
- **Single-process / single-writer** architecture (above) is the real
  ceiling for horizontal scale.

### 5.4 Documentation

- **`docs/architecture.md` was not updated with the continuity layer.**
  The P9 implementation plan named it ("continuity services in the layer
  diagram + request flow", `docs/platform-p9-implementation-plan.md:90`),
  but the current `architecture.md` has no continuity section. The
  continuity architecture lives only in
  `docs/platform-p9-architecture-design.md`.
- **`docs/configuration.md` still omits 9 env vars** (W2, verified).
- **`docs/v1.0.0-readiness-report.md` and `docs/v1.0.0-final-audit.md`
  are stale** (P6.4 baseline 1916/18) — they predate P7–P9.
- **Untracked plan docs** by policy (W7) are a deliberate, documented
  choice; the surviving design docs were committed with their phase.
- `.env.example` covers all parsed vars (verified against `config.py`).

### 5.5 Release blockers

- **B1 — OpenAI account quota** (external; gateway is NVIDIA-only until
  restored). The single hard blocker.
- **W1 — version/tag mismatch** (`0.1.0` vs `v1.0.0`).
- **P4 Decision H** — OpenRouter/Groq reserved-but-unwired keys; close at
  the gate.
- **Continuity-enabled deployed profile is undefined** — a new v1.0
  operator decision (flag default off; enabling it changes wire behavior
  with additive headers/SSE only).

---

## 6. P10 boundary verification

**Result: the P10 boundary holds. No multi-agent functionality entered the
codebase.** Verified by a full keyword sweep of `app/` and `tests/`:

- Every `agent` match is the HTTP `User-Agent` header or client-app
  bucketing (`middleware.py:26,114`; `client_detection.py:34-48`) — never
  an LLM agent concept.
- `orchestrat` matches are CLI/setup/compaction pipeline language; no agent
  framework (no langchain/crewai/autogen/MCP in `pyproject.toml` or
  `requirements.txt`).
- **Zero** `subagent`, `swarm`, `agentic`, `planner`, ReAct/reflexion/
  decompose/ensemble patterns in `app/` or `tests/`.
- **No tool execution from the LLM path.** All tool code is verbatim
  passthrough / API-format translation for client-driven loops
  (`app/providers/anthropic_client.py:80-207`; `gemini_client.py:92-254`).
  No `subprocess`/`os.system`/`exec` in the request path.
- **No parallel model collaboration.** `decision_engine.py` selects exactly
  one winning candidate (`_pass`, `decision_engine.py:239-274`); failover
  is sequential across candidates; no `asyncio.gather`/fan-out in `app/`.
- The only relay-initiated model call is the optional single-call LLM
  summarizer (`summarizer.py:208`), with deterministic extractive fallback —
  a background compaction utility, not an agent loop.
- P9's own non-goals section explicitly reserves these for P10
  (`docs/platform-p9-architecture-design.md:516-558`).

The P9 boundary statement ("single-conversation, single-request,
single-candidate-at-a-time") is accurately implemented.

---

## 7. Findings, recommendations, and readiness score

### 7.1 Findings

- **F1 — Architecture is sound and the continuity design is
  failure-mode-complete.** Every external failure mode from the P9e study
  is answered structurally and pinned by tests. No new production
  hardening was needed for the audit itself.
- **F2 — The empirical gap is operational, not architectural.** No live
  multi-provider soak; dormant overflow-retry and persist-on-compact paths.
- **F3 — Documentation lagged the code.** `architecture.md` and the v1.0
  readiness docs were not refreshed with the continuity layer or post-P6
  counts.
- **F4 — The decision engine is hot-path inert by design** (statistics
  only); worth an explicit note so a future change doesn't silently alter
  selection semantics.
- **F5 — Continuity-enabled deployment posture is undefined** for the
  release (flag default off; operator must decide and document it).
- **F6 — The P10 boundary is clean.** No multi-agent functionality exists.

### 7.2 Recommendations

| # | Action | Type | Priority |
| --- | --- | --- | --- |
| A1 | Run a live multi-provider soak: N turns across 2+ real providers with restarts and resume, then publish results | Operational validation | **High** (before advertising the goal) |
| A2 | Wire the dormant overflow-retry and persist-on-compact paths to production triggers, or document them as reserved | Code / decision | Medium |
| A3 | Refresh `docs/architecture.md` with the continuity layer (diagram + request flow) | Docs | **High** |
| A4 | Refresh `docs/configuration.md` (W2) and the v1.0 readiness/final-audit docs to post-P9 counts | Docs | **High** |
| A5 | Add a structural write-time filter for the memory contract (catch key variants, not exact matches) or extend `FORBIDDEN_KEYS` coverage | Security hardening | Medium |
| A6 | Add a per-key rate-limit seam on `validate_resume` when continuity is enabled | Security hardening | Medium |
| A7 | Close P4 Decision H (wire OpenRouter/Groq or drop the reserved keys) at the v1 gate | Release | Medium |
| A8 | Document the continuity-enabled deployed profile (`CONTINUITY_ENABLED=true` + retention + resume-token lifecycle) in `docs/deployment.md` | Docs / release | **High** |
| A9 | Bump version to `1.0.0` in the same change as the tag (W1) | Release | **High** |
| A10 | Re-run the live smoke once the OpenAI key has quota (B1) | Operational | Blocking |

### 7.3 Release-readiness score

**8 / 10 — release-ready with documented warnings.**

- +4 architecture and design (clean layering, failure-mode-complete continuity,
  P10 boundary holds)
- +2 test coverage (2338 passed / 22 skipped; 231 continuity tests across 10
  files; 80 adversarial; 6 simulation; RC 28)
- +1 security posture (defense-in-depth, keyring-first, memory contract,
  P9e adversarial closures)
- +1 packaging/CI/install validation
- −1 operational evidence (no live multi-provider soak; dormant paths)
- −1 documentation lag and open release decisions (W2, version bump,
  continuity deployment profile, Decision H, B1 blocker)

### 7.4 Required actions before v1.0

**Blocking:**
1. Version bump to `1.0.0` in the same change as the tag (W1).
2. Re-run the live smoke when the OpenAI key has quota (B1), or make an
   explicit NVIDIA-ready-only release decision.
3. Update `PROJECT_LOG.md` at the final release commit (per workflow).

**Required (documented warnings):**
4. Complete `docs/configuration.md` (W2).
5. Define and document the continuity-enabled deployed profile (A8).
6. Refresh `architecture.md` and the v1.0 readiness docs (A3, A4).
7. Close P4 Decision H (A7).
8. Fix `docs/v1.0.0-final-audit.md` counts (1916/18 → 2338/22) at the gate.

**Recommended before GA advertising of multi-provider continuity:**
9. Run the live multi-provider soak (A1).

---

**Stop condition:** this audit is delivered for review. No code changed,
no commits, `PROJECT_LOG.md` untouched.
