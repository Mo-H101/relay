# Implementation Audit — Request Path vs. Intended Design

Status: phase-1 audit, verified against source at commit `8d0c2f4` (`master`).
Date: 2026-08-13.

This document answers one question for every intended feature: **is it actually
wired into the live request path, or is it dead/partial code?** Everything below
was confirmed by reading the source; line numbers are exact.

---

## 1. The live request call chain (`/v1/chat/completions`)

1. `POST /v1/chat/completions` → `openai_chat_completion`
   (`app/api/openai.py:265-266`).
2. Continuity scope resolved from headers (`X-Relay-Conversation-Id`,
   `X-Relay-Project-Id`, `X-Relay-Resume-Token`) by
   `_resolve_continuity_scope` → `resolve_scope`
   (`app/api/openai.py:41-61`, `app/services/continuity_headers.py:121-185`).
   Off unless `continuity_enabled` and the request is key-scoped. Malformed
   header → generic 400. A presented resume token is validated immediately
   (`relay.validate_resume`, `app/api/openai.py:60`).
3. **Candidate construction is a literal upstream-model string match:**
   ```python
   candidates = [
       (p, req.model) for p in relay.provider_manager.all()
       if req.model in p.models
   ]
   ```
   (`app/api/openai.py:293-297`). `req.model` is **required** by the schema
   (`app/schemas/openai.py`, `ChatCompletionRequest`); unknown names → 400
   `Model 'X' not available from any provider.` (`app/api/openai.py:298-304`).
   **No virtual model, no task classifier, no `CandidateBuilder`, no
   `DecisionEngine` on this path.**
4. Continuity turn opened: `turn = relay.begin_continuity_turn(continuity_scope)`
   (`app/api/openai.py:310`; `app/core/relay.py:178`).
5. Stream path → `async_chat_svc.achat_across_stream_messages(candidates, …)`
   (`app/api/openai.py:317`; `app/services/async_chat_service.py:634`).
   Non-stream path → `achat_across_messages(candidates, …)`
   (`app/api/openai.py:419`; `app/services/async_chat_service.py:481`).
   Both walk `candidates` in order, fail over on provider/model errors, and
   record telemetry/health per attempt (`app/api/openai.py:438`).
6. `/v1/models` exposes the raw upstream catalog: every provider model id
   verbatim, `owned_by=p.name` (`app/api/openai.py:475-481`).

**Legacy `/chat` path** (`app/api/chat.py:128-129`): resolves a task via
`_resolve_task` → `classify_task` (`app/api/chat.py:106-122`) and flows it into
`relay.achat(..., task=task)` → `candidate_builder.build(providers, task=task)`
(`app/core/relay.py:369, 423`). So task-based routing machinery exists and runs
here — but it is confined to `/chat` and does not run on `/v1`.

---

## 2. Findings per intended feature

### F1. Unified, Relay-facing model interface — MISSING on `/v1`
The product vision (Prompt A §1) requires clients to not know provider model
names. On `/v1` the exact opposite is enforced: `model` is required and must be
a literal upstream model id (`app/api/openai.py:293-304`). The abstraction layer
that exists (`candidate_builder`, `task_classifier`, `decision_engine`) is never
touched. `/v1/models` even leaks raw upstream ids with provider ownership
(`app/api/openai.py:475-481`). This is the single largest gap.

### F2. Task-aware model selection — implemented, but only on `/chat`
`task_classifier.py` → `candidate_builder.build(providers, task=…)` →
`model_catalog` scoring (`app/services/model_catalog.py:139-175`) all work, and
are exercised by `/chat` (`app/api/chat.py:106-122`, `app/core/relay.py:369,
423`). Nothing on `/v1` calls them.

### F3. Decision engine — implemented but decorative
`DecisionEngine` is wired (`app/core/relay.py:87-88`) and `decide()` is called,
but its return value is **discarded** in both the `/chat` and relay paths:
`app/core/relay.py:371-372` and `app/core/relay.py:425-426` both do
`self.decision_engine.decide(...)` without using the result. Ordering is decided
by `CandidateScorer`/`CandidateBuilder`, and the engine's output only feeds
statistics/`/decision/explain` (`app/api/decision.py:34-68`). Disabled by
default (`DECISION_ENGINE_ENABLED`, `app/core/config_spec.py:322`).

### F4. Continuity / model handoff — protocol verified, content NOT preserved
The continuity stack is real and well-tested: key-scoped scope resolution
(`continuity_headers.py`), metadata-only durable store (`conversation_store.py`),
envelope + resume (`handoff.py`), failover-driven auto-switch in
`achat_across_messages`. **But the envelope carries metadata, not the work:**
- Turns persisted by `append_turn` hold only `provider, model, outcome, task,
  tokens_in, tokens_out, latency_ms` (`app/services/conversation_store.py:564-582`).
- `_build_envelope` builds `summary` + `tail` from `state.committed_turns`, which
  are metadata turns; the "tail" is `serialize_tail(turns)`
  (`app/services/handoff.py:643-724`).
- The injected system message renders that envelope (`app/services/handoff.py:183-186`).
- So "model B continues model A's work" delivers a *summary of which models did
  what*, not the actual decisions/results — consistent with the privacy contract
  (see F5), but not with the vision's "context preservation".

### F5. Privacy contract (Option C) — the binding constraint
`memory_contract.py` classifies `prompts`, `responses`, `generated_content`,
and any key shaped like `message`/`content` as **NEVER** (`app/services/memory_contract.py:32-69, 74-98`),
and `conversation_store.py:20-26` states raw prompts/responses are never stored.
This is why F4 is metadata-only. **Any feature that persists conversation
content is blocked unless the contract changes.** Content-derived context is
only legal if it is ephemeral (never persisted) and redacted.

### F6. Automatic context compaction — partially implemented, dormant
Compaction exists and is exercised **inside the handoff envelope builder** when
the token estimate overflows the budget (`app/services/handoff.py:691-712`,
`CompactionReason.PREFLIGHT`). It is automatic, budget-aware, and persisted as
metadata (`summarize_and_persist`/`record_compaction`). But `summarize_and_persist`
has **no production call site** — nothing drives compaction on the raw request
path, there is no client-visible compaction trigger, and compaction operates on
metadata turns, not on request messages. There is no production detection of a
provider context-limit error feeding a compaction/retry loop.

### F7. Observability — present and consistent
`/decision/provider` + `/decision/explain` (`app/api/decision.py:12-68`),
`/health`, `/health/deep`, `/metrics`, `/diagnostics`, TUI, event log, admin
`/admin/reload` + `/admin/events` (`app/api/admin.py:44,91`). None of these leak
content; memory-contract negative tests enforce it.

---

## 3. The central architectural tension

The vision requires **"continuing a project with another model without losing
what has been accomplished"**; the privacy contract forbids persisting the
accomplishments' content. Current code resolves this by transferring *metadata*
only, which is weaker than the vision.

Recommended resolution (within the contract, no contract change):
- OpenAI-compatible clients resend the full message history on every request.
- Therefore Relay can derive a **content-aware context from the in-request
  `messages` array** — processed ephemerally, never persisted — and inject that
  summary/tail into the continuity envelope instead of (or alongside) the
  metadata tail.
- This preserves Option C (nothing content-shaped is written to any store) while
  giving model B a real description of what model A accomplished in the current
  conversation. Compaction then operates on in-request messages, not metadata.

This is the key decision to confirm before Phases 5/6.

---

## 4. Feature classification summary

| Feature | Live on `/v1`? | Status |
|---|---|---|
| Unified model interface (virtual names, omitted `model`) | No | **Missing** — literal match at `openai.py:293-304` |
| Task-aware routing | No | Partial — `/chat` only (`chat.py:106-122`) |
| Decision engine | No | Decorative — output discarded (`relay.py:371-372, 425-426`) |
| Continuity / model handoff | Yes | Partial — protocol solid, content not preserved |
| Auto context compaction | No | Dormant — envelope-internal only (`handoff.py:691-712`) |
| Provider failover | Yes | Working (`async_chat_service.py:481, 634`) |
| `/v1/models` abstraction | No | Raw catalog leak (`openai.py:475-481`) |

---

## 5. Key references

- `/v1` handler: `app/api/openai.py:265-472`
- Literal candidate match: `app/api/openai.py:293-304`
- `/v1/models`: `app/api/openai.py:475-481`
- Legacy `/chat` task path: `app/api/chat.py:106-122`, `app/core/relay.py:369-372, 423-426`
- Decision engine (output discarded): `app/core/relay.py:371-372, 425-426`
- Continuity scope: `app/services/continuity_headers.py:121-185`
- Metadata-only turns: `app/services/conversation_store.py:513-595`
- Envelope build: `app/services/handoff.py:643-724`; inject: `handoff.py:183-186`
- Privacy contract: `app/services/memory_contract.py:32-98`
- Model catalog fallback chain: `app/services/model_catalog.py:139-175`

---

## 6. Post-audit implementation status

- **Phase 3 done (2026-08-13):** the unified Relay-facing model interface is now
  live on `/v1`. `model` is optional (`app/schemas/openai.py`); omitted models,
  virtual names (`auto`/`default`/`relay`), and task names route through
  `_resolve_candidates` → `candidate_builder.build` with task classification when
  enabled (`app/api/openai.py`). Literal upstream ids keep the verbatim
  passthrough. The wire payload model is bound per attempt
  (`app/services/async_chat_service.py:445-448, 625-630`), and `/v1/models`
  advertises Relay-facing names alongside the upstream catalog. Full suite:
  **2476 passed, 8 skipped, 0 failed**.
- **Phase 4 done (2026-08-13):** the decision engine now runs on the `/v1`
  request path for routed requests when `DECISION_ENGINE_ENABLED` is set
  (`app/api/openai.py`, decision pass after candidate resolution). Parity and
  observability only — engine output never changes ordering, and explicit
  upstream passthrough skips it.
- **Phase 5/6 done (2026-08-13):** content-aware handoff behind the opt-in
  `continuity_content_context_enabled` flag (default off). `inject_payload`
  (`app/services/handoff.py`) appends a bounded, redacted content summary of the
  in-request messages to the envelope and compacts an over-budget array
  (redacted digest + recent tail) before forwarding
  (`app/services/ephemeral_context.py`). Ephemeral only — never persisted,
  logged, exported, or surfaced in metrics/events; the memory contract is
  untouched.
- Remaining: Phase 7 (CI branch fix) is applied (`main` → `master`),
  docs/release prep, and a final full-suite verification of Phases 4–6.
