# PROGRESS.md — Relay session state

High-level status for cross-tool handoffs. Updated at session boundaries.
Base all work on: current code + this file + `git log --oneline -10`.

## Current state

- **HEAD:** `d7433b1` — `docs: update project progress after phase 8`
  (committed and pushed to `origin/master`).
- **Working tree:** Phase 9A work-in-progress, **not committed**:
  `app/services/handoff.py`, `tests/test_continuity_handoff.py`,
  `tests/test_continuity_phase9a.py` (new), `docs/clients/continuity.md`
  (new), plus doc link updates (README, docs/clients/*, docs/configuration.md).
- **Recent history:** Phase 7 landed at `c41cd22` (actual decision
  records for `/v1`); Phase 8 at `64976cd` (decision execution
  telemetry); progress doc at `d7433b1`.
- **Baseline suite:** 2558 passed, 8 skipped, 0 failed (Python 3.13.5,
  full suite in ~9.5 min), re-verified after the Phase 9A additions.
  The SD-card sqlite concurrency flake is intermittent — see Known
  failures below.
- **Remote:** `github.com/Mo-H101/relay` (private, branch `master`).
  Workflow: pull before starting, commit + push at natural checkpoints,
  only one tool edits the repo at a time.

## Known failures / flakes

- `test_platform_store.py::TestConcurrency::test_concurrent_opens_of_same_file`
  fails under a full-suite run on this SD-card-backed device (sqlite
  `busy_timeout=5000` exceeded by 8 concurrent opens under load). Passes
  in isolation and on the other device. Intermittent: did not trigger on
  the Phase 8 verification run, but triggered on one review re-run
  (2544 passed + this flake, 0 real failures). Environment-dependent,
  **not** changed or papered over in Phase 8.

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
- **Phase 8 — Decision truth integration (8B–8D, implemented + verified):**
  - **8B (completed/verified)** — legacy `/chat` actual-decision recording.
    The Phase 7 `_record_actual_decision` was extracted into the shared
    `record_actual_decision(store, ...)` in `app/services/decision_record.py`;
    `/v1` now delegates to it and `relay.chat()`/`relay.achat()` record the
    executed candidate for the legacy path (capturing the previously
    discarded `decide()` result). Routing, ordering, retry/failover, and
    explicit-passthrough behavior unchanged.
  - **8C (completed/verified)** — read-only diagnostics surface: the
    `/diagnostics` snapshot now includes a bounded `actual_decisions`
    section (most recent 50 records, metadata only) served from the same
    `DecisionRecordStore` the `/decision/explain/actual` endpoint uses.
  - **8D (investigation complete — Outcome B)** — `AdaptiveWeights`
    (`app/services/adaptive.py`) is **dormant/redundant for production
    ordering**: the production `CandidateScorer` already consumes the same
    authoritative EWMA estimates (`TelemetryStats.ewma_success` /
    `ewma_latency_ms`) directly from the telemetry store, gated by
    `adaptive_min_samples`. `AdaptiveWeights` is used only by diagnostics/
    tests and adds no unique production functionality; it stays an
    observability-only derivation layer (documented in its module docstring).
    No production call site was added. Eventual removal proposed separately.
  - **8E (verified)** — full suite green (2545 passed, 8 skipped, 0
    failed). Routing equivalence (phase0), candidate-builder health,
    adaptive routing, sync/async parity, retry/failover, passthrough,
    virtual routing, decision engine/record, continuity, and API suites
    all pass unchanged.
- **Phase 9A (cross-client continuity verification, implemented + verified,
  not yet committed):**
  - **Fix — key-scoped in-memory state** (`app/services/handoff.py`):
    `HandoffCoordinator._states` is now keyed by the composite
    `(key_id, conversation_id)` tuple (new `_state_key()` helper
    normalizing `str(key_id or "")`), so the same conversation id under
    two different store-backed keys can never share or collide on this
    process's continuity state. Same-key conversations still reuse state;
    no-conversation-id keys never collide. Verified by the new
    `TestKeyScopedState` in `tests/test_continuity_handoff.py`.
  - **Cross-client HTTP verification** (`tests/test_continuity_phase9a.py`,
    10 tests): a `cline` client is served only after an A→B failover, then
    an `opencode` client resumes the same conversation with the wire
    resume token — one conversation row (`client_bucket "cline"`), one
    contiguous durable `seq` run [1,2], the data-marked continuity envelope
    injected on the resumed request, and derived project state
    (turn/switch counters, model chain). Stream variant asserts the
    `relay:conversation` / `relay:model_switched` SSE events on the wire.
  - **Restart-safe resume**: a fresh `Relay` over the same `platform.db`
    resumes from the wire token at `last_seq + 1` with no re-execution or
    duplicate rows; a stale/wrong token fails closed (200, resume denied
    `token_mismatch`, sequence neither reset nor advanced by the attempt,
    no durable replay recorded), and the correct token resumes cleanly
    afterwards.
  - **Privacy / memory contract**: raw prompt/response content and raw
    resume tokens never appear anywhere in `platform.db` (only SHA-256
    hashes), all continuity exports are free of content-shaped keys
    (`contains_never_captured` false), and the opt-in content digest
    (`CONTINUITY_CONTENT_CONTEXT_ENABLED`) is ephemeral — present only in
    the forwarded payload, absent from the store.
  - **Regressions on this path**: bootstrap keys get no continuity,
    header-less requests unchanged, flag-off parity, literal-model
    passthrough verbatim, and the Phase 8 actual-decision record still
    fires on the continuity path.
  - **Docs:** new `docs/clients/continuity.md` (wire contract: headers,
    one-time resume token, cross-client handoff, staleness/replay limits,
    privacy), linked from `docs/clients/index.md`, all client guides,
    README, and `docs/configuration.md` §Project continuity.
  - **Full suite green** after the additions: 2558 passed, 8 skipped,
    0 failed. Phase 9 is **not** marked complete — later continuity work
    (see What is next) is still open.
- **Fixes:** CI workflow trigger `main`→`master`; EmbeddedServer readiness
  poll (`app/core/server.py`); health-store freshness determinism.
- **Docs:** `docs/implementation-audit.md` (request-path audit + phase
  status), `docs/capability-matrix.md`, configuration/known-limitations
  updated for the new flag.

## What is next (candidate backlog)

- **Problem F (mostly done in Phase 8):** `/v1` and `/chat` both record
  actual decisions and `/diagnostics` surfaces them. Remaining: nothing
  in this phase — handoff/context-compaction/project-persistence and
  cross-client continuity changes are explicitly later phases.
- **AdaptiveWeights (Problem G):** resolved as **dormant/redundant**
  (Outcome B) in Phase 8D. Propose eventual removal as a separate,
  independent change; do not wire it into production ordering.
- Continuity reachability + client docs: continuity requires
  `CONTINUITY_ENABLED` AND a store-backed key; bootstrap/unauthenticated
  traffic gets none. **Phase 9A documented the
  `X-Relay-Conversation-Id` / `X-Relay-Project-Id` /
  `X-Relay-Resume-Token` contract end-to-end** in
  `docs/clients/continuity.md` and verified cross-client + restart-safe
  resume over HTTP. Remaining: decided-scope follow-ups beyond 9A
  (e.g. extending to bootstrap keys, if ever justified) and the
  later-phase items below.
- Final release prep / README / release-candidate checklist as needed.

## Next architectural work (Phase 9+ candidates, unverified)

- Model handoff, context compaction, project persistence, and
  cross-client continuity (explicitly out of scope for Phase 8).
- Actual-decision records remain bounded in-memory; a durable schema is
  a later decision only if justified.

## Key decisions

- Privacy contract (Option C) is binding: no conversation content is ever
  persisted. Content-derived context is legal only when ephemeral + redacted
  (P9f approach).
- `/v1` keeps verbatim passthrough for literal upstream model ids; routing
  applies only to virtual/task/omitted models.
- **One authoritative routing/execution plan (Phase 8 invariant):** task
  classification → CandidateBuilder/RoutingEngine → CandidateScorer /
  health/adaptive signals → ordered execution plan → ChatService
  execution/failover. The DecisionEngine is an analysis/observability
  layer only — it must NOT become an independent authoritative router, and
  no second ordering authority is introduced.
- Decision engine is observational by design; ordering is owned by
  CandidateBuilder/CandidateScorer.
- Actual-decision records are metadata only and describe the *executed*
  candidate (post-failover), never a predicted one; they are bounded
  in-memory (no persistence) until a later phase justifies a schema.
