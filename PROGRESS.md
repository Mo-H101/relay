# PROGRESS.md — Relay session state

High-level status for cross-tool handoffs. Updated at session boundaries.
Base all work on: current code + this file + `git log --oneline -10`.

## Current state

- **HEAD:** `3a6ed85` + local work (actual-decision record / Phase 7).
- **Suite:** 2497 passed, 8 skipped, 0 failed on the other device
  (Python 3.14). On the SD-card-backed device (Python 3.13.5) the suite
  shows **1 environment-dependent flake** — see Known failures below.
- **Remote:** `github.com/Mo-H101/relay` (private, branch `master`).
  Workflow: pull before starting, commit + push at natural checkpoints,
  only one tool edits the repo at a time.

## Known failures / flakes

- `test_platform_store.py::TestConcurrency::test_concurrent_opens_of_same_file`
  fails under a full-suite run on this SD-card-backed device (sqlite
  `busy_timeout=5000` exceeded by 8 concurrent opens under load). Passes
  in isolation and on the other device. Environment-dependent, **not**
  changed or papered over in Phase 7.

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
- **Phase 7 — Orchestration truth layer (implemented + verified on /v1):**
  `app/services/decision_record.py` adds an explicit, metadata-only
  `DecisionRecord` for what an actual `/v1/chat/completions` request did:
  executed provider/model (post-failover), ordered candidate pool, ranks,
  per-attempt metadata, classified task, correlation id, and (when
  `DECISION_ENGINE_ENABLED`) the engine's reason/confidence/signals for
  the *executed* candidate. `Relay.decision_record_store` is a bounded
  in-memory `DecisionRecordStore` (never persisted; classified
  `decision_records`/EPHEMERAL in the memory contract). The `/v1` handler
  now captures the previously discarded `decide()` result and records
  stream + non-stream outcomes (stream final outcome attached in place).
  `GET /decision/explain/actual` serves the most recent or
  correlation-id-looked-up record (404 when absent); `GET /decision/explain`
  stays predictive. Routing behavior unchanged. Status: **implemented and
  verified** by focused + full-suite tests.
- **Fixes:** CI workflow trigger `main`→`master`; EmbeddedServer readiness
  poll (`app/core/server.py`); health-store freshness determinism.
- **Docs:** `docs/implementation-audit.md` (request-path audit + phase
  status), `docs/capability-matrix.md`, configuration/known-limitations
  updated for the new flag.

## What is next (candidate backlog)

- **Per-request decision explanation (Problem F):** *partially
  implemented* — actual-decision records + `/decision/explain/actual`
  exist for `/v1`; the legacy `/chat` path still discards `decide()` and
  records no actual decision, and the records are not yet surfaced in
  `/diagnostics`.
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
- Actual-decision records are metadata only and describe the *executed*
  candidate (post-failover), never a predicted one; they are bounded
  in-memory (no persistence) until a later phase justifies a schema.
