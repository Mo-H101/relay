# Capability Matrix — Intended Design vs. Implementation

Status: phase-2 deliverable, derived from the implementation audit.
Date: 2026-08-13.

Legend: **Implemented** = live on the production path and tested ·
**Partial** = exists and works but only on a subset of paths, or weaker than the
vision · **Missing** = not wired anywhere.

---

## Intended feature set (from the product vision / handoff)

### 1. Unified Relay-facing model interface
- **Status: Partial** (only on `/chat`; absent on `/v1`).
- Intended: clients name tasks/models through Relay; Relay picks provider+model.
- Reality: `/v1` requires a literal upstream model id and 400s on anything else
  (`app/api/openai.py:293-304`). `/chat` routes by task name
  (`app/api/chat.py:106-122`).
- **Gap:** accept omitted/`auto`/`relay`/task-named models on `/v1`; keep explicit
  upstream ids as passthrough.

### 2. Virtual model name passthrough
- **Status: Missing.**
- Intended: a request may reference a Relay-side virtual model that maps to one
  or more upstream models.
- Reality: no virtual-model registry exists; only exact upstream string matching.
- **Gap:** virtual-model name resolution → candidate set expansion.

### 3. Task-named model routing
- **Status: Partial** (legacy `/chat` only).
- Intended: request `model="coding"` routes to the best coding model.
- Reality: `classify_task` → `candidate_builder.build(providers, task=…)` works
  on `/chat` (`app/core/relay.py:369, 423`) but is never called on `/v1`.
- **Gap:** route task-named models on `/v1`.

### 4. Default / automatic routing (omitted `model`)
- **Status: Missing.**
- Intended: omitted `model` routes by request content/task.
- Reality: `model` is a required schema field; a missing value is a pydantic 422.
- **Gap:** make `model` optional; default to task classification.

### 5. Explicit upstream passthrough preserved
- **Status: Implemented** (verbatim passthrough of `messages`, tools, stream,
  usage; `app/api/openai.py:307-308, 313-472`).
- Note: passthrough currently **requires** an upstream id; keep that behavior
  when an upstream id is supplied.

### 6. Task-aware model selection (classifier + catalog scoring)
- **Status: Implemented** — `task_classifier.py`, `model_catalog.py`
  (exact → family → keyword fallback, `model_catalog.py:139-175`),
  `candidate_builder.py`. Wired on `/chat` only.
- **Gap:** invoke on `/v1` for virtual/omitted/task-named models.

### 7. Decision engine (reasons on the wire)
- **Status: Partial** — implemented, called, but **output discarded**
  (`app/core/relay.py:371-372, 425-426`); disabled by default
  (`DECISION_ENGINE_ENABLED=false`).
- **Gap:** use its decision to order/choose candidates on the request path, or
  explicitly ship it as observability-only.

### 8. Provider failover
- **Status: Implemented** — `achat_across_messages` / `achat_across_stream_messages`
  (`app/services/async_chat_service.py:481, 634`), failover-driven model switch
  (`reason="failover"`), telemetry/health per attempt.

### 9. Model handoff / continuity protocol (headers, scoping, envelope, resume)
- **Status: Implemented** — `continuity_headers.py`, `handoff.py`, resume token
  lifecycle (`conversation_store.py` replay tracking), key-scoped storage.
- See feature 10 for the content gap.

### 10. Continuity of context across models ("model B continues model A's work")
- **Status: Partial** — handoff transfers **metadata** (provider/model/outcome/
  task/token counts, `handoff.py:643-724`), not the work itself.
- Binding constraint: Option C / memory contract — content may never be
  persisted (`memory_contract.py:62-98`, `conversation_store.py:20-26`).
- **Gap:** derive content-aware context from the in-request `messages` array
  (ephemeral, never persisted) and inject it into the envelope. Client resends
  history on every request, so the content source is already on the wire.
- **Decision needed:** confirm this approach before Phases 5/6.

### 11. Automatic context compaction
- **Status: Partial** — compaction logic exists and is budget-aware
  (`handoff.py:691-712`, `summarizer.py`), but runs only inside the envelope
  builder and only on metadata turns; `summarize_and_persist`
  (`summarizer.py:256`) has no production call site; no request-path trigger.
- **Gap:** trigger compaction on the request path (token-budget or context-limit
  detection) and compact the in-request message array.

### 12. Context-overflow / token-limit handling
- **Status: Missing** — no production detection of provider context-limit errors
  feeding a compaction/retry loop (only the dormant `ContextOverflowSignal`).
- **Gap:** detect provider `context_length_exceeded`-class errors and retry
  against a compacted context.

### 13. `/v1/models` as a Relay-facing catalog
- **Status: Missing** — leaks raw upstream ids with provider ownership
  (`app/api/openai.py:475-481`).
- **Gap:** decide the surface (virtual names + upstream ids, or Relay names
  only) and implement.

### 14. Observability (explanations, diagnostics, TUI, health, metrics)
- **Status: Implemented** — `/decision/explain`, `/diagnostics`, `/health`,
  `/metrics`, admin reload/events, TUI; content-safe by construction
  (memory-contract negative tests).

### 15. Privacy / memory contract
- **Status: Implemented and enforced** — `memory_contract.py`, redaction,
  summary verifier, negative tests.
- Any change in Phases 5/6 must stay inside this contract (ephemeral-only
  content).

---

## Gap priority

1. ~~**P0 (Phase 3):** unified model interface on `/v1`~~ — **done** (2026-08-13).
   Optional `model`, virtual names `auto`/`default`/`relay`, task-named routing,
   and task classification now route through the candidate machinery on `/v1`;
   literal upstream ids keep verbatim passthrough; the wire payload model is
   bound per attempt; `/v1/models` advertises Relay-facing names. Full suite:
   2476 passed, 8 skipped, 0 failed.
2. ~~**P1 (Phase 4):** decision engine output on the request path (feature 7)~~ —
   **done** (2026-08-13). Parity + observability: the engine runs on `/v1`
   routed requests when `DECISION_ENGINE_ENABLED`, records stats, and never
   changes ordering; explicit passthrough is skipped.
3. ~~**P1 (Phase 5):** content-aware handoff within the privacy contract
   (feature 10)~~ — **done** (2026-08-13). Opt-in
   `continuity_content_context_enabled`: bounded, redacted content summary of the
   in-request messages is injected into the handoff envelope; ephemeral only
   (never persisted/logged/exported).
4. ~~**P2 (Phase 6):** request-path compaction + overflow handling (features
   11-12)~~ — **done** (2026-08-13). Same flag: an over-budget message array is
   compacted (redacted digest + recent tail) before forwarding.
5. **P2:** `/v1/models` abstraction (feature 13) — implemented as virtual names +
   upstream ids; remaining surface decisions can ride along with Phase 7.
6. ~~**P3:** CI branch fix (`main` vs `master`)~~ — **done** (2026-08-13).
