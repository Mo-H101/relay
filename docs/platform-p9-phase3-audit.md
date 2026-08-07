# P9 — Phase 3 (P9c) Audit: Model Handoff Coordinator

Date: 2026-08-06.
Status: **Audit / research document — no code.** P9c implementation does not
begin until this document is approved. `PROJECT_LOG.md` is not modified.
No commits.

Prerequisites (all approved / landed):
- `docs/platform-p9-research-plan.md` — approved decisions (Option C memory,
  `CONTINUITY_ENABLED` default `false`, opaque headers).
- `docs/platform-p9-architecture-design.md` — approved architecture, §6
  handoff protocol, §9 loop prevention, §16 P10 boundary.
- `docs/platform-p9-implementation-plan.md` — P9c DoD (§12).
- **P9a landed** (committed): schema v7, config, memory contract,
  `ConversationStore`, `ContinuityFlusher`, facade wiring, lifespan hooks.
- **P9b landed** (committed): `ContextManager`, `summarizer`,
  `summary_verifier`, continuity dataclasses, overflow retry helper.
- Full suite green: **2114 passed / 22 skipped** (P9b commit `26ada25`).

Scope of this document: the mandated 7-section audit for **P9c — Handoff
Coordinator** (envelope, switch caps, provider-switch integration, header
plumbing, SSE handoff events). Sections 5 and 7 are grounded in a verified
read of the current code (file:line references) rather than the plan only.

---

## 1. Current request lifecycle trace

### 1.1 Shared entry and authentication (both chat surfaces)

1. **Global auth dependency**: `app/main.py:67` mounts
   `Depends(require_api_key)` on the whole app; `MetricsMiddleware` is
   added at `app/main.py:73`. Routers mount at `app/main.py:75-84`.
2. **Lifespan**: `app/main.py:40-42` starts `relay.continuity_flusher`
   and runs the startup retention prune; `app/main.py:56-61` stops it and
   performs the final flush on shutdown. The flusher object is `None`
   unless `CONTINUITY_ENABLED` (wired in `app/core/relay.py:104-127`).
3. **Auth resolution** (`app/security/auth.py`):
   - Public allowlist `/` and `/health` (`auth.py:34`, checked `:267`).
   - Bootstrap key path: constant-time compare (`auth.py:95-102`,
     `:280-290`). **Does not set `request.scope["relay_key_id"]`**
     (audit actor is `"bootstrap"`).
   - Store-backed path: `_grant_store` sets
     `request.scope["relay_key_id"] = meta["id"]` at **`auth.py:240`**,
     then enforces route scopes (`auth.py:242-246`).
   - **P9c seam**: `relay_key_id` is the authoritative key scope. The
     store-backed path is the only one that produces a `key_id`; the
     bootstrap key and unauthenticated traffic get **no continuity**
     (headers ignored), consistent with the implementation plan §11.

### 1.2 `/chat` (non-streaming, sync facade)

4. Handler: `app/api/chat.py:83` — task resolution (`:85-99`), generation
   kwargs extraction (`:101-116`).
5. Facade: `relay.achat(...)` at `app/core/relay.py:307`:
   - correlation id (`relay.py:322`), `provider_manager.ranked()`
     (`:324`), `candidate_builder.build(providers, task)` (`:333`),
     optional `decision_engine.decide` (`:335`),
   - `async_chat_service.achat_across(candidates, message,
     max_retries, ...)` (`:338`),
   - `request_logger.chat(result)` (`:347`), telemetry (`:349-354`),
     health feedback (`:356-397`).
6. Attempt loop: `achat_across` (`app/services/async_chat_service.py:127`).
   Per candidate: `_atry_once` (`:54`) resolves a client through
   `ClientRegistry` (`:72`) and calls `client.achat(...)` (`:73-78`);
   exceptions are classified by `failure_classifier.classify`
   (`async_chat_service.py:81`). Failure policy:
   - `PROVIDER_LEVEL` → skip the provider (`:192-194`);
   - `RETRYABLE` and retries remain → backoff via
     `retry_wait_seconds` (`:196-208`);
   - otherwise → next candidate.
   Success returns the result dict with `attempts` (`:172-188`).
7. Response: `chat.py:127` sets `X-Relay-Correlation-Id`; `:129-140`
   maps failures to 502 (provider) / 503 (no provider) preserving the
   correlation header; `:149-152` returns `ChatResponse`.
8. Metrics/ops: `_record_chat` (`chat.py:34-57`) → `relay_metrics` +
   `ops_store`.

### 1.3 `/v1/chat/completions` (OpenAI surface, async service)

9. Handler: `app/api/openai.py:188` — correlation id (`:193`),
   `tool_choice` validation (`:197`), candidate filter by model
   (`:206-209`), verbatim payload (`:219`).
10. Non-streaming: `achat_across_messages` (`async_chat_service.py:397`)
    → `_atry_once_messages` (`:341`) → `client.achat_messages`
    (`:360`); same loop/failure semantics as §1.2.
11. Streaming: `achat_across_stream_messages`
    (`async_chat_service.py:509`) → `_atry_stream_once_messages`
    (`:488`) → `client.achat_stream_messages`. First-chunk
    verification (`:548`); failed starts recorded per-attempt, no
    retry (candidate list is the failover path). The API layer's
    `stream_generator` (`openai.py:265-309`) wraps chunks as
    `data: {json}\n\n`, emits `data: [DONE]` (`:281`), and on a
    mid-stream exception emits an error chunk then `[DONE]`
    (`:282-293`); telemetry/health/metrics recorded in `finally`
    (`:294-309`). Response is `text/event-stream` with the
    correlation header (`:311-315`).
12. Non-streaming passthrough returns the provider's parsed response
    dict unchanged (`openai.py:365-369`).

### 1.4 Verified continuity attachment seams (P9c hooks)

| Seam | Location | P9c hook |
| --- | --- | --- |
| Key scope | `auth.py:240` `request.scope["relay_key_id"]` | continuity enabled only when a `key_id` exists |
| Request headers | `chat.py:83`, `openai.py:189` | validate `X-Relay-Conversation-Id` / `X-Relay-Project-Id` (new `continuity_headers`), derive `project_key` |
| Context input | `achat_across*` `message` / `payload` args (`async_chat_service.py`) | inject synthetic `[summary block] + [tail]` context for post-first attempts |
| Switch trigger | candidate walk `PROVIDER_LEVEL` / retry-exhausted paths (`async_chat_service.py:192-208`, `:458-474`) | `HandoffCoordinator.on_switch(...)` builds the envelope before the next candidate |
| Sync parity | `chat_service.chat_across*` (`app/services/chat_service.py:105,256,369,482,575`) | identical continuity hooks on the sync stack |
| SSE stream | `openai.py:265-309` generator | additive `relay:*` lines (`relay:conversation`, `relay:compacted`, `relay:model_switched`) beside `data:` lines |
| Response headers | `chat.py:127`, `openai.py:194` | echo `X-Relay-Conversation-Id` |
| Turn commit | success branch of the candidate walk | enqueue metadata to `ContinuityFlusher` (in-memory → SQLite on the flusher thread); **never direct SQLite** |
| Logging | `app/services/log_service.py:94` `RequestLogger.chat` | add `conversation_id` to JSON records (metadata only; `request_log` table untouched) |
| Metrics | `app/services/metrics.py:550-568` continuity block | add switch / compact / replay counters |

### 1.5 Hot paths that must remain untouched (byte-identical when flag off)

- `provider_manager.ranked()/all()/enabled()`, `candidate_builder.build`,
  `decision_engine.decide`, `failure_classifier.classify`,
  `chat_policy` (`budget_exhausted`, `retry_wait_seconds`,
  `fallback_reason`), `ClientRegistry` and all provider clients.
- `request_log` table and its privacy contract; `ops_store`; `events`
  table (additive vocabulary only, already extended in P9a).
- `StateStore` / `StateFlusher` / `HealthStore` / `HealthRefresher`;
  persistence and telemetry paths.
- With `CONTINUITY_ENABLED=false`: no headers honored, no envelope, no
  continuity rows, no new SSE lines — responses byte-identical
  (`test_continuity_parity.py` asserts this before P9c merges).

---

## 2. Comparison with existing systems

Assessment lens (mandated): **losing progress, surviving context, avoiding
repeated work, incomplete-task detection, failure recovery**. Sources:
public docs/web research for OpenCode, Cline, Continue.dev, Aider, Codex
(2026).

### 2.1 OpenCode — context compaction

- **Model**: sessions; durable session messages retained; when the
  context overflows, older context is replaced by a checkpoint: a
  structured **summary + serialized tail** of recent context. Token
  estimate is `len(text) // 4` chars per token. Compaction runs
  automatically as a preflight; on a context-overflow error it retries
  **once** with the compacted context; a second overflow surfaces as an
  error.
- Losing progress: local TUI/CLI; session files persist across restarts
  on one machine; no server-side cross-device handoff.
- Surviving context: summary + tail checkpoint; durable session messages
  kept for later recall.
- Repeated work: avoided by keeping the tail verbatim and summarizing
  only the old region.
- Incomplete-task detection: per-message; no explicit task lifecycle.
- Failure recovery: retry-once on overflow, then degrade to error.

**Takeaway for Relay**: Relay already mirrors this in P9b (§4/§9): the
4-char heuristic, `summary + tail` split, preflight + overflow
retry-once, and "degrade, never fail the request". P9c only has to
*couple* these to the provider switch path.

### 2.2 Cline — task persistence

- **Model**: tasks are self-contained sessions with a unique id, dedicated
  storage, and full history; **git-snapshot checkpoints**; auto-compact;
  explicit "continue" vs new-task.
- Losing progress: on-disk task state survives restarts; per-project,
  per-device.
- Surviving context: full history + compaction; continue replays prior
  context.
- Repeated work: checkpointing and auto-compact reduce re-explaining.
- Incomplete-task detection: user-driven (task stays open in the UI).
- Failure recovery: local retries; provider outage ends the session.

**Takeaway**: Cline proves durable, keyed, resumable task state is the
right shape (Relay's `conversations`/`conversation_turns`), but its
checkpoints are filesystem/git-bound — **out of scope** for Relay P9
(no filesystem access; `project_key` is opaque).

### 2.3 Continue.dev — context providers

- **Model**: context is user-selected via `@` providers (`@Codebase`,
  `@Search`, `@Repository Map`); no automatic compaction described; chat
  context is per-session.
- Losing progress: in-memory per session; nothing durable server-side.
- Surviving context: re-injection is manual.
- Repeated work: repo map reduces re-reading; conversation continuity is
  not the mechanism.

**Takeaway**: provider-selected context (repo map) is orthogonal to
Relay's conversation continuity; not adopted.

### 2.4 Aider — repo-aware context

- **Model**: whole-repo map of symbols/signatures, graph-ranked to fit a
  `--map-tokens` budget; commits per change.
- Losing progress: git is the state machine; uncommitted state signals
  incomplete work.
- Surviving context: repo map is injected per request within a token
  budget.
- Incomplete-task detection: repository status / uncommitted changes.
- Failure recovery: git revert / re-apply.

**Takeaway**: Aider is repo-model continuity (project state), not
conversation-model. Relay's `project_state` surface (last models,
counters) is the only analogous element and is already landed in P9a.

### 2.5 Codex — thread lifecycle

- **Model**: threads are persisted server-side with a lifecycle
  (fork / resume / archive); an agent-loop harness exposes JSON-RPC
  (App Server); the rollout model is durable, resumable sessions.
- Losing progress: server-side durable threads; resume replays history.
- Surviving context: full windowed history (no compaction referenced in
  the surveyed material).
- Repeated work: resume avoids restarting the task.
- Incomplete-task detection: thread state marks open vs archived.
- Failure recovery: thread resume; provider-level failures are surfaced.

**Takeaway**: Codex is the closest server-side durable-conversation
analogue; Relay's resume/replay protocol (P9d) and `archive` status
(P9a) adopt the thread lifecycle shape. P9c's switch cap / envelope is
the piece Codex does not describe.

### 2.6 Synthesis

| Axis | OpenCode | Cline | Continue | Aider | Codex | **Relay P9c picks** |
| --- | --- | --- | --- | --- | --- | --- |
| Progress persistence | session files | task storage + git | none durable | git | server threads | durable `conversations`/`conversation_turns` (P9a) |
| Context survival | summary+tail | full history+compact | manual re-inject | repo map | full window | envelope = summary+tail (P9b/P9c) |
| Repeated-work avoidance | compaction | checkpoint/continue | manual | map+commits | resume | envelope + turn commit |
| Incomplete-task detection | per-message | task stays open | none | git status | thread state | turn `outcome` + `unresolved` in summaries |
| Failure recovery | retry-once | local retry | none | revert | resume | retry-once + degrade ladder + switch caps |

Nothing new is required: every P9c mechanism maps to an already-landed
building block or an approved architecture decision.

---

## 3. P9c boundary

**In scope for P9c** (implementation-plan §1, architecture §16, P9c DoD §12):

- `HandoffCoordinator` (`app/services/handoff.py`): context-envelope
  assembly, `on_switch(...)`, switch caps
  (`MAX_SWITCHES_PER_TURN=3`, `MAX_SWITCHES_PER_WINDOW=5`).
- `continuity_headers` (`app/services/continuity_headers.py`): header
  bounds/charset validation, `project_key` derivation
  (`sha256(key_id || ":" || project_id)[:16 bytes]` hex),
  conversation-id generation.
- Integration into **all four async entry points**
  (`achat_across`, `achat_across_messages`, `achat_across_stream`,
  `achat_across_stream_messages`) and the **four sync counterparts**
  (`chat_service.chat_across*`).
- API plumbing in `app/api/chat.py` and `app/api/openai.py`:
  read/validate headers, echo `X-Relay-Conversation-Id`, additive
  `relay:*` SSE lines, handoff-aware error mapping (existing 502/503
  shape preserved).
- Envelope injection for post-first attempts; `relay:model_switched`
  emitted on switch; switch accounting; turn commit via the flusher.
- `RequestLogger` JSON gains `conversation_id` (metadata only).
- P9c suites: `test_continuity_handoff.py`, `test_continuity_http.py`,
  `test_continuity_parity.py`.

**Explicitly NOT P9c** (later P9 phases or P10):

- Resume/recovery protocol, `relay:resume_token`, replay caps,
  S1/S2 reconnect semantics → **P9d** (`continuity_recovery.py`).
- `relay conversations` CLI, TUI diagnostics, docs, `.env.example` →
  **P9d**.
- Security-best-practices gate and adversarial pass → **P9e**.
- Multi-agent execution, parallel model collaboration, autonomous
  file editing, task delegation, AI developer teams → **P10**
  (architecture §16; no agent concept in any P9 path).
- Repo-map / git-checkpoint context (Aider/Cline model) →
  **out of scope for all of P9**.

Migrations: **none** (v7 landed in P9a). Config keys: **none new**
(all `MAX_SWITCHES_*`, `MAX_RESUME_REPLAYS`, `CONTINUITY_*` already in
`app/core/config.py`).

---

## 4. Handoff protocol design

### 4.1 Envelope structure

Built by `HandoffCoordinator.envelope(...)` from the persisted summary +
tail (architecture §6; P9b provides the parts):

```
continuity: {
  conversation_id,      # opaque uuid4 hex
  project_key,          # opaque, key-scoped hash
  summary_version,      # SUMMARY_VERSION = 1 (parsers reject unknowns)
  summary,              # derived block or summary_ref (dedupe by up_to_seq)
  tail,                 # verbatim metadata turns (serialized, bounded)
  token_budget_remaining,
  model_chain,          # bounded JSON array (cap 8)
  resume_token, ts
}
```

Rendered as an **additive synthetic context block** in the forwarded
payload (`[summary block] + [tail]`); provider clients are unchanged.
Bounded by the compaction budgets (§4.3).

### 4.2 Data transferred per switch

- **Summary**: derived-only (never verbatim), versioned, provenance
  `(method: extractive|llm, model, tokens_in/out)`, bounded by
  `CONTINUITY_SUMMARY_MAX_CHARS=4096` and the summary token budget.
  Only produced by the P9b summarizer; verified before persist
  (`summary_verifier.verify`, redaction hard guard).
- **Tail**: the newest metadata turns kept verbatim, capped by
  `CONTINUITY_TAIL_MAX_ITEMS=20` and the tail token budget; serialized
  deterministically by `ContextManager.serialize_tail`.
- **Project state**: `last_models` + bounded counters merged from the
  existing `project_state` row (`conversation_store.update_project_state`),
  refreshed after each committed turn.
- **Accounting**: `CompactionResult` fields `from_tokens`, `to_tokens`,
  `summary_tokens`, `tail_tokens`, `reason`, `method` are carried into
  `compaction_records` and the SSE `relay:compacted` provenance.

### 4.3 Token budget math (already landed in P9b, reused as-is)

- `estimate_tokens(text) = max(1, len(text) // 4)`.
- Compact when `estimate(full context) > budget - reserve`, with
  `budget = CONTINUITY_CONTEXT_TOKEN_BUDGET = 32768`,
  `reserve = CONTINUITY_OUTPUT_RESERVE_TOKENS = 2048`.
- Split: `usable = budget - reserve`;
  `summary_budget = floor(usable * 0.4)`;
  `tail_budget = usable - summary_budget`.

### 4.4 Switch trigger

1. During the existing candidate walk, a **provider-level failure**
   (`PROVIDER_LEVEL` classification) or retry-exhaustion is observed.
2. `HandoffCoordinator.on_switch(conversation_id, from_model, to_model,
   reason)` is invoked **off the hot path**; it applies the caps (§4.5)
   and assembles the envelope (§4.1).
3. The next candidate call includes the envelope.
4. Success → turn committed (`provider, model, outcome=ok, tokens,
   latency, resume_token`), `project_state` updated, SSE
   `relay:model_switched` emitted (metadata + provenance only), stream
   continues without restarting the turn.
5. Failure → next candidate; all candidates exhausted → existing 502/503
   with correlation id, turn outcome `failed`.

Mid-stream: a provider error during an open stream surfaces through the
existing `stream_generator` exception path (`openai.py:282-293`); P9c
emits `relay:model_switched` + a continued SSE section from the new
provider rather than restarting the turn (P9c DoD).

### 4.5 Loop prevention (P9c-owned)

- **Switch cap**: `MAX_SWITCHES_PER_TURN=3`, `MAX_SWITCHES_PER_WINDOW=5`
  (per conversation, sliding window). Exhaustion stops failover, returns
  the last failure, turn outcome `failed`, audit row (`continuity.switch`
  / `continuity.denied`). Prevents A→B→A→B thrash.
- **Compaction cap**: at most one preflight compaction + one overflow
  retry per request; a second overflow degrades to **current-request-only**
  (no re-compact) and never fails the request.
- **Retry cap**: continuity adds no retries beyond the existing
  classifier policy; each candidate attempt counts once.
- **Summary dedupe**: summaries keyed by `(conversation_id, up_to_seq)`
  (UNIQUE in schema); never regenerated from another summary.
- **Feedback**: continuity metadata feeds existing telemetry/quality
  with the same weights as per-request metadata; no new self-referential
  signal.

---

## 5. Required files

### 5.1 New files

| File | Purpose |
| --- | --- |
| `app/services/handoff.py` | `HandoffCoordinator`: `envelope(...)`, `on_switch(...)` (caps + degradation action), handoff metadata recording |
| `app/services/continuity_headers.py` | `validate_conversation_id`, `validate_project_id`, `derive_project_key`, `new_conversation_id`; bounds (≤128 bytes, printable ASCII, no control chars); values never echoed |
| `tests/test_continuity_handoff.py` | envelope assembly, switch caps, failover-with-context (async + sync, stream + non-stream, messages + string payloads), mid-stream `relay:model_switched` |
| `tests/test_continuity_http.py` | header validation (bounds/charset → 400, no echo), response `X-Relay-Conversation-Id`, additive SSE events, unknown-event-tolerant client |
| `tests/test_continuity_parity.py` | flag-off byte-identical parity (headers ignored, no rows, no SSE lines) |

### 5.2 Modified files

| File | Change |
| --- | --- |
| `app/api/chat.py` | read/validate headers, continuity context through the facade call, response `X-Relay-Conversation-Id`, handoff-aware 502/503 mapping (shape unchanged) |
| `app/api/openai.py` | same for `/v1/chat/completions` (non-stream + stream); additive `relay:*` SSE lines in the stream generator |
| `app/core/relay.py` | wire `HandoffCoordinator` (lazy, only when `CONTINUITY_ENABLED`); continuity hooks in `chat()`/`achat()`; echo conversation id |
| `app/services/async_chat_service.py` | optional continuity context threading through the four `achat_across*`; envelope injection before post-first attempts; switch accounting; turn-commit enqueue |
| `app/services/chat_service.py` | sync parity for the four `chat_across*` |
| `app/services/log_service.py` | `RequestLogger.chat` JSON gains `conversation_id` (metadata only; table untouched) |
| `app/services/metrics.py` | continuity counters for switches / compactions / replay denials (extend existing block at `metrics.py:550-568`) |

### 5.3 Explicitly unchanged

- **Migrations**: none (v7 landed in P9a).
- **Config / spec**: none new; all `CONTINUITY_*` / `MAX_*` keys already
  validated in `app/core/config.py:784-845`.
- `platform_store.py`, `conversation_store.py`, `continuity_flusher.py`,
  `context_manager.py`, `summarizer.py`, `summary_verifier.py`,
  `memory_contract.py`, `event_log.py`, `models/continuity.py` — landed
  in P9a/P9b; P9c only calls into them.
- `request_log` table and privacy contract; `ops_store`; provider
  clients; `client_registry.py`; `failure_classifier.py`;
  `chat_policy.py`; routing/decision layers.

---

## 6. Risks

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Stale/wrong context on switch (summary out of sync with tail, model-chain overflow) | wrong answers, wasted tokens | versioned summary; dedupe by `up_to_seq`; envelope assembled from the same compaction that is persisted; chain cap 8; `relay:compacted` provenance |
| Switch thrash / infinite failover | cost, latency, provider hammering | `MAX_SWITCHES_PER_TURN` / `MAX_SWITCHES_PER_WINDOW`; audit rows; drift metric flags pathological compaction |
| State corruption (partial turn, double commit) | inconsistent history | turn commit via flusher only; ephemeral in-flight marker; restart semantics S2; `UNIQUE(conversation_id, seq)` |
| Privacy leak (summary carries raw content; header values echoed) | data exposure | redaction hard guard at write (`contains_never_captured`); headers never echoed in errors/logs/metrics; `relay:*` events carry metadata/provenance only; negative tests |
| Header misuse (project id treated as a path) | injection/FS risk | project id opaque; key-scoped one-way hash; no filesystem access from any continuity path |
| Flag-off regression | behavior drift | parity suite gates every P9c commit; additive-only changes; default off |
| Sync/async drift | inconsistent behavior between `/chat` and `/v1` | share decision helpers via `chat_policy`; mirrored integration tests |
| Mid-stream switch complexity | broken SSE / restarted turns | staged: non-stream first, then stream-start failover, then mid-stream switch; client tolerates unknown `relay:*` events |
| Cost/DoS via summarizer or envelope | expense | off-hot-path assembly; budget caps; existing per-key rate-limit seams; no SQLite on request paths |
| Concurrency on one conversation | torn state | single-writer flusher; per-request turn markers; `busy_timeout 5000`; no DB on request path (asserted) |

---

## 7. Implementation plan

Ordered steps, each ending with its verification. **No commit until the
full P9c DoD is green and the phase is approved for commit.**

1. **`continuity_headers.py`** (pure validation + derivation) — unit
   tests: bounds/charset accept+reject, `derive_project_key` stability
   and key-scoping, `new_conversation_id` shape, nothing echoed.
2. **`handoff.py` `HandoffCoordinator`** — pure envelope assembly +
   switch caps; unit tests: envelope fields, budget accounting,
   per-turn/window cap enforcement and degradation action.
3. **Facade wiring** in `app/core/relay.py` — coordinator instantiated
   only when `CONTINUITY_ENABLED`; no-flag behavior unchanged.
4. **Async integration** — `async_chat_service.py` context threading +
   envelope injection + switch accounting; `app/api/openai.py` header
   read/validate, `X-Relay-Conversation-Id` echo, additive `relay:*`
   SSE lines; `tests/test_continuity_handoff.py` +
   `tests/test_continuity_http.py` (async non-stream, stream-start,
   mid-stream).
5. **Sync parity** — `chat_service.py` + `app/api/chat.py`; mirrored
   tests for `/chat`.
6. **Metadata/log/metrics** — `RequestLogger` `conversation_id`;
   `metrics.py` counters.
7. **Parity + gate** — `tests/test_continuity_parity.py` (flag off =
   byte-identical); full regression gate (`python -m pytest`,
   `python -m compileall -q app`); no ruff/mypy gate exists in this
   repo — pytest + compileall is the gate.
8. **Stop and report.** No P9d work (recovery/resume, CLI/TUI, docs)
   without a separate approval.

**P9c DoD (implementation-plan §12, verified after step 7):**

- Envelope assembly + switch-cap unit tests green;
- async + sync failover-with-context integration tests green
  (non-stream and stream; messages and string payloads);
- `relay:model_switched` emitted on switch; response header
  `X-Relay-Conversation-Id` present;
- mid-stream switch does not restart the turn;
- flag-off parity suite green; full suite green (2114/22 baseline,
  plus new suites).

---

**Stop condition**: this audit is delivered for approval. Implementation
does not start until approved. No code, no commits, `PROJECT_LOG.md`
untouched.
