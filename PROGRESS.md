# PROGRESS.md — Relay session state

High-level status for cross-tool handoffs. Updated at session boundaries.
Base all work on: current code + this file + `git log --oneline -10`.

## Current state

- **HEAD:** `df4b127` — unified /v1 model interface, decision parity,
  content-aware handoff (see commit message).
- **Suite:** 2497 passed, 8 skipped, 0 failed (Python 3.14 / pydantic 2.12).
- **Remote:** `github.com/Mo-H101/relay` (private, branch `master`).
  Workflow: pull before starting, commit + push at natural checkpoints,
  only one tool edits the repo at a time.

## What is done

- **Phase 3 — Unified /v1 model interface** (`app/api/openai.py`,
  `app/schemas/openai.py`, `app/services/async_chat_service.py`):
  `model` is optional. Omitted models, virtual names (`auto`/`default`/
  `relay`), and task names (`coding`, `reasoning`, ...) route through the
  candidate machinery (`_resolve_candidates` → `candidate_builder.build`,
  task classification when `TASK_CLASSIFICATION_ENABLED`). Literal
  upstream ids keep verbatim passthrough. Wire payload model is bound per
  attempt (virtual names never reach upstream). `/v1/models` advertises
  Relay-facing names + task categories before the raw upstream catalog.
- **Phase 4 — Decision engine on the request path** (`app/api/openai.py`):
  routed `/v1` requests run `decide()` when `DECISION_ENGINE_ENABLED`.
  Parity/observability only — never changes ordering; passthrough skips it.
- **Phase 5/6 — Content-aware handoff (P9f)**
  (`app/services/ephemeral_context.py` new, `app/services/handoff.py`):
  opt-in `CONTINUITY_CONTENT_CONTEXT_ENABLED` (default off) derives a
  bounded, redacted content summary of in-request messages and compacts
  over-budget arrays before forwarding. Ephemeral only — never persisted,
  logged, exported, or surfaced in metrics/events; memory contract
  untouched.
- **Fixes:** CI workflow trigger `main`→`master`; EmbeddedServer readiness
  poll (`app/core/server.py`); health-store freshness determinism.
- **Docs:** `docs/implementation-audit.md` (request-path audit + phase
  status), `docs/capability-matrix.md`, configuration/known-limitations
  updated for the new flag.

## What is next (candidate backlog)

- Per-request decision explanation surface (Problem F): `/decision/explain`
  is predictive, not a record of what a request actually did; the engine
  now records stats on `/v1` but there is no per-request "why this model"
  answer for clients/operators.
- Adaptive weights subsystem (`app/services/adaptive.py`) still has no
  production call site (only diagnostics/tests import it). Wire it or
  explicitly document it as dormant (Problem G).
- Continuity reachability + client docs: continuity requires
  `CONTINUITY_ENABLED` AND a store-backed key; bootstrap/unauthenticated
  traffic gets none. Client guides do not document the
  `X-Relay-Conversation-Id` / `X-Relay-Project-Id` /
  `X-Relay-Resume-Token` contract end-to-end. Consider documenting (and/or
  extending to bootstrap keys) so hand-off is demonstrable (Problem H).
- Final release prep / README / release-candidate checklist as needed.

## Key decisions

- Privacy contract (Option C) is binding: no conversation content is ever
  persisted. Content-derived context is legal only when ephemeral + redacted
  (P9f approach).
- `/v1` keeps verbatim passthrough for literal upstream model ids; routing
  applies only to virtual/task/omitted models.
- Decision engine is observational by design; ordering is owned by
  CandidateBuilder/CandidateScorer.
