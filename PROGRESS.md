# PROGRESS.md — Relay session state

High-level status for cross-tool handoffs. Updated at session boundaries.
Base all work on: current code + this file + `git log --oneline -10`.

## Current state

- **HEAD:** `64a660c` — Phase 10B complete (master, pushed).
- **Working tree:** clean.
- **CI** on all matrix jobs (ubuntu Python 3.11/3.12/3.13, windows
  Python 3.12, packaging) — run #10 (`31907969693`) fully passed after
  the `f93c112` packaging-test skip. Phase 10A CI confirmation ran green.
  Phase 10B CI confirmation not yet queried from this device (gh
  unauthenticated).
- **Recent history:** Phase 7 at `c41cd22` (actual decision records for
  `/v1`); Phase 8 at `64976cd` (decision execution telemetry); progress
  doc at `d7433b1`; Phase 9A at `27a0ca9` (cross-client continuity);
  CI/3.10–3.11 remediation at `9a8d9d5`; doc state updates at `a5703a2`
  and `3dbeec8`; packaging-test 3.11 skip at `f93c112`; SQLite
  concurrency/SIGBUS fix at `30b2078`; Phase 9B at `d65e802` (model
  handoff); Phase 10A at `fdd5af7`–`d5eaba8` (empty-model seq fix,
  project_state reader, CLI surface, diagnostics subsection, docs);
  Phase 10B at `64a660c` (overflow-retry wiring).
- **Phase 10B (overflow-retry wiring) is COMPLETED** — implemented and
  verified at `64a660c` (see What is done).
- **Phase 10A (empty-model continuity fix, conversation-states surface)
  is COMPLETED** — implemented and verified at `fdd5af7`–`d5eaba8` (see
  What is done).
- **Phase 9B (model handoff) is COMPLETED** — implemented and verified
  at `d65e802` (see What is done).
- **Resolved:** the SQLite platform-store concurrency/SIGBUS issue is now
  fixed at `30b2078` (see Known failures below for the investigation and
  verification).
- **Baseline suite:** 2633 passed, 8 skipped, 0 failed (Python 3.13,
  full suite in ~8 min), verified in the Phase 10B close-out run.
- **Remote:** `github.com/Mo-H101/relay` (private, branch `master`).
  Workflow: pull before starting, commit + push at natural checkpoints,
  only one tool edits the repo at a time.

## Known failures / flakes

- ~~`test_platform_store.py::TestConcurrency::test_concurrent_opens_of_same_file`
  fails under a full-suite run on this SD-card-backed device (sqlite
  `busy_timeout=5000` exceeded by 8 concurrent opens under load). Passes
  in isolation and on the other device. Intermittent: did not trigger on
  the Phase 8 verification run, but triggered on one review re-run
  (2544 passed + this flake, 0 real failures). Environment-dependent,
  **not** changed or papered over in Phase 8.~~ **Fixed at `30b2078`.**
- **CI manifestation / now resolved (`30b2078`):** on ubuntu Python 3.11
  the same test crashed the interpreter with `Fatal Python error: Bus
  error` (SIGBUS, exit 135) in CI run #9, in
  `platform_store.open_connection()` — `PRAGMA journal_mode = WAL` ran
  outside `_migration_lock`, so 8 concurrent first-time opens of the same
  fresh `platform.db` all raced the one-time WAL switch (db header write
  + `-wal`/`-shm` sidecar creation). Locally this surfaced as
  `database is locked` (and, via the corrupt-retry path, `disk I/O
  error`).
  **Root cause confirmed by instrumentation:** ~26/60 stress rounds
  failed, and 100% of failures were at the WAL pragma; the sanity SELECT
  and `migrate()` (already under the lock) never failed.
  **Fix:** run the WAL pragma under the existing `_migration_lock`
  (`app/services/platform_store.py` `open_connection()`), so the
  one-time sidecar/shm init is atomic across concurrent opens. No new
  lock; non-reentrant lock acquired sequentially (no deadlock).
  **Verification:** targeted suite 16 passed; 60 rounds of the 8-thread
  fresh-open stress pass (26 failed pre-fix); 80 rounds of 16-thread +
  reopen pass; consumer suites (state/key/event/request stores,
  continuity) pass; full suite 2558 passed, 8 skipped, 0 failed.
  CI confirmation pending for the commit.

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
  committed at `27a0ca9`):**
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
- **CI / Python 3.10–3.11 compatibility remediation (committed with this
  progress update):**
  - **f-string syntax (3.10/3.11):** `app/services/metrics.py` and
    `app/ui/screens/models.py` used PEP-701-only backslash escapes inside
    f-string expressions (invalid on 3.10/3.11, `compileall` failure).
    Rewrote to escape-free forms (`metrics.py` builds the `le="..."` label
    by plain concatenation outside the expression; `models.py` uses literal
    `✓`/`✗` glyphs). Generated Prometheus exposition and displayed glyphs
    are byte-identical (metrics tests assert the exact strings).
  - **Test isolation:** both `isolated_state` fixtures now also patch
    `platform_store.state_dir`, so `test_provider_flow_validates_key_before_persist`
    and `test_wizard_offers_keyring_key_as_existing` no longer touch the
    real `<repo>/.relay/platform.db`. Reproduced the CI failure with
    `RELAY_STATE_DIR` → nonexistent dir (pre-fix: 2 failed; post-fix: 2
    passed).
  - **CI hardening:** `actions/checkout@v4` now `fetch-depth: 0` +
    `fetch-tags: true` in both jobs so the packaging regression test
    (`git archive` of a historical tree) works on CI; Python 3.13 added to
    the Ubuntu test matrix (3.10 minimum unchanged).
  - **Verified:** `compileall -q app tests` green on Python 3.11.15 and
    3.14.6; full suite 2558 passed, 8 skipped, 0 failed (Python 3.14);
    packaging suite green incl. the wheel-upgrade regression test.
- **Packaging-test 3.11 skip (`f93c112`):** `test_wheel_upgrade_from_previous_release`
  builds the old 0.1.0 wheel from `_PREVIOUS_RELEASE_TREE` (`dbc2902`),
  whose `metrics.py` uses pre-PEP-701 f-string syntax — un-importable on
  Python 3.10/3.11 (deterministic 3.11 failure in CI runs #7/#8). Added
  `skipif(sys.version_info < (3, 12))` with a clear reason; test stays
  active on 3.12+. CI run #10 fully green afterwards.
- **SQLite concurrency/SIGBUS fix (`30b2078`):** serialized the one-time
  `PRAGMA journal_mode = WAL` switch (db header + `-wal`/`-shm` sidecar
  creation) under the existing `_migration_lock` in
  `app/services/platform_store.py::open_connection()`. Root cause of the
  CI run #9 SIGBUS (`test_concurrent_opens_of_same_file`) and the
  local `database is locked` flake; 100% of stress failures were at the
  WAL pragma (pre-fix 26/60 rounds, post-fix 0/60 fresh + 0/80 with
  16-thread/reopen). Full suite green (2558 passed, 8 skipped, 0
  failed). No new locking mechanism; sequential (non-nested) lock
  acquisition, no deadlock. Multi-process safety unchanged (SQLite file
  locking + `busy_timeout=5000`).
- **Phase 9B (model handoff, implemented + verified, committed at
  `d65e802`):**
  - **Durable conversation model anchor** (`app/core/relay.py`,
    `app/services/handoff.py`, `app/services/continuity_recovery.py`):
    `Relay.anchor_for()` returns the conversation's last committed
    logical `(provider, model)` — the in-memory committed view first,
    durable turn metadata (`ContinuityRecovery.last_provider_model`) as
    the restart/cross-process fallback. Both lookups are key-scoped, so a
    conversation id presented by another key never yields another key's
    anchor. The anchor is also seeded onto fresh `HandoffCoordinator`
    state for an existing conversation so it survives restarts.
  - **Anchor-first candidate tiering** (`app/services/candidate_builder.py`):
    `CandidateBuilder.build(..., anchor=...)` puts every candidate
    carrying the anchor model (anchor tier, deduplicated, spanning all
    providers that host it) first, the remaining routing output (fallback
    tier) second. Health/scoring may reorder within each tier but never
    across tiers; `ranked_candidates()`/`rankables()` thread the same
    anchor so the decision engine observes the identical plan (Phase 8
    single-plan invariant intact). An anchor model no provider can
    execute yields an empty anchor tier that falls through to the
    fallback tier (Case D). With no anchor the plan is byte-identical to
    the pre-Phase 9B output.
  - **Explicit cross-turn model-selection classification**
    (`app/services/handoff.py` `record_transition`, wired from
    `app/api/openai.py` and `Relay.annotate_transition`): after candidate
    resolution and before execution, the anchor's model is compared with
    the plan's first candidate. A different model emits exactly one
    `relay:model_switched` event (`switch_count=0`) —
    `reason="selection"` for an explicit literal model request,
    `reason="failover"` for Relay-initiated routing; an identical model
    emits nothing (provider movement within the same logical model stays
    with the execution-time `on_switch`, `reason="failover"`). The
    annotation event is never duplicated by a within-turn switch; a
    literal selection followed by an execution-time failover emits both
    events in order.
  - **Model-lineage reconstruction after restart**: durable per-turn
    `(provider, model, seq)` metadata seeds both the fresh state's seq
    counter (no `UNIQUE (conversation_id, seq)` collision) and its model
    chain, so a resumed conversation reconstructs the anchor and lineage
    across processes.
  - **/v1 and /chat continuity behavior**: virtual/omitted models
    (`auto`/`default`/`relay`, no model) are anchored on resume across
    `/v1/chat/completions` (stream + non-stream) and `/chat`; explicit
    literal models are never anchored (verbatim passthrough preserved).
  - **Tests** (`tests/test_continuity_phase9b.py`, 31 tests): anchor
    resolution/precedence/key-scoping/durability; transition
    classification (selection vs failover); anchor tiering (first,
    health-aware within-tier ordering, cross-provider expansion, no
    duplicates, unhosted-anchor fall-through, ranked parity); HTTP /v1
    streaming + non-streaming and /chat resume staying on the anchor;
    selection-then-failover event ordering; anchor-unavailable
    fall-through with `reason="failover"`; same-model cross-provider
    within-turn switch not duplicated by the annotation; restart
    reconstructing anchor + lineage.
  - **Verification:** Phase 9B suite 31 passed; full suite 2589 passed,
    8 skipped, 0 failed in the pre-commit run; `compileall -q app tests`
    green.
- **Phase 10A (empty-model continuity fix + conversation-states
  surface, implemented + verified, committed at `fdd5af7`–`d5eaba8`):**
  - **Slice 0 — empty-model seq fix** (`app/services/continuity_recovery.py`,
    `app/services/handoff.py`): `durable_last_seq()` now reads the last
    turn's seq independently of `last_provider_model()`; a last turn
    committed without a model still continues the sequence (seq N+1) so
    the unique `(conversation_id, seq)` constraint is never violated. The
    fresh-state seed in `HandoffCoordinator.start()` now decouples the
    durable seq source (`durable_last_seq`) from the anchor/model lineage
    source (`last_provider_model`); the anchor stays `None` until a real
    model turn commits.
  - **Slice 1 — project_state reader** (`app/services/conversation_store.py`):
    `ConversationStore.project_states(key_id=None, limit=50)` is a
    read-only bounded projection of the durable `project_state` checkpoint
    table. Each row contains `project_key`, `key_id`, `last_models`
    (parsed JSON list), `counters` (parsed JSON dict), and `last_seen`.
    Ordered newest `last_seen` first with stable `project_key` tie-break.
    `ContinuityRecovery.project_states()` passthrough was removed (not in
    approved spec; callers should use `ConversationStore` directly).
  - **Slice 2 (removed):** `HandoffCoordinator.build_conversation_snapshot()`
    was not in the approved spec and has been removed.
  - **CLI surface** (`app/cli/continuity.py`): `relay conversations projects
    [--limit N] [--json]` lists project-state checkpoints. Text output
    shows project_key, key_id, turns, and models. JSON output returns
    `{"projects": [...]}`. Disabled continuity prints "continuity
    disabled"; unavailable store prints "continuity unavailable" and
    exits 1.
  - **Diagnostics subsection** (`app/services/diagnostics.py`):
    `_continuity()` returns bounded counts in the `/diagnostics` snapshot:
    `{enabled, conversations, active, archived, turns, summaries,
    compactions, projects, replays}`. Disabled returns
    `{"enabled": false}`; unavailable store returns
    `{"enabled": true, "available": false}`.
  - **Tests:** 26 tests total — 9 `project_states` table tests, 4
    no-authority guardrail tests (regex-based, tolerating enqueue
    strings), 8 CLI tests (text/JSON/disabled/unavailable/limit), 5
    diagnostics tests (enabled/disabled/unavailable/zero/keys). All pass.
  - **Verification:** compileall green; 417 continuity+diagnostics tests
    pass (Python 3.13). CI push confirmation pending.
- **Phase 10B (overflow-retry wiring, implemented + verified, committed
  at `64a660c`):**
  - **Overflow detection** (`app/services/chat_policy.py`): `Attempt` data
    class gains `_exc: Any` (private, `repr=False`, `compare=False`), set
    to the original exception on every failed attempt. Not serialized in
    `to_dict()`.
  - **Rebuild infrastructure** (`app/services/handoff.py`):
    `TurnContext.rebuild_for_overflow()` delegates to
    `HandoffCoordinator._rebuild_envelope_for_overflow()`, which
    atomically (single `threading.Lock` acquisition) clears stale
    coordinator state (`state.envelope=None`, `state.envelope_seq=0`),
    rebuilds the envelope via `_build_envelope(state,
    _overflow_params=_OVERFLOW_PARAMS)` with aggressive compaction
    parameters (`tail_max_items=5`, `summary_share=0.7`), updates both
    coordinator state and turn, and clears `turn._injected_payload`.
    Delegates to existing `ContextManager.compact()` — no parallel
    summarization system. `_OVERFLOW_PARAMS` is the single authoritative
    definition (`handoff.py:41`).
  - **Overflow retry logic** (`app/services/chat_service.py`,
    `app/services/async_chat_service.py`): all four non-streaming
    `chat_across*` variants (`chat_across`, `achat_across`,
    `chat_across_messages`, `achat_across_messages`) check overflow
    conditions after each failed attempt: `turn is not None`,
    `attempt._exc is not None`, `turn.context_manager is not None`,
    `should_retry_compacted(exc)` returns `True`. On overflow:
    `overflow_retried=True` (per-candidate flag), `turn.rebuild_for_overflow()`,
    re-inject message/payload, `relay_metrics.continuity_overflow_retries.inc()`,
    `continue` (retries within existing loop). Exactly one overflow retry
    per candidate; subsequent overflows follow normal retry/failover.
  - **Metrics** (`app/services/metrics.py`): `relay_continuity_overflow_retries_total`
    counter incremented on each overflow retry.
  - **Streaming paths:** untouched — no lines containing "stream" in the
    diff for `chat_service.py` or `async_chat_service.py`.
  - **Tests** (`tests/test_continuity_overflow.py`, 16 tests):
    Cases A–G covering sync/async/messages overflow retry, retry
    exhaustion, non-overflow passthrough, no-turn failover, metrics,
    `_exc` on Attempt, envelope changes after rebuild (real coordinator
    with committed turns), empty-envelope recovery, payload-cache
    invalidation, and one-retry invariant.
  - **Verification:** compileall clean; 16/16 overflow tests pass; 2633
    passed, 8 skipped, 0 failures full local suite (Python 3.13). CI
    confirmation not yet queried from this device (gh unauthenticated).
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

- **Model handoff — COMPLETED in Phase 9B (`d65e802`).**
- **Empty-model continuity fix + conversation-states surface — COMPLETED
  in Phase 10A (`fdd5af7`–`d5eaba8`).**
- **Overflow-retry wiring — COMPLETED in Phase 10B (`64a660c`).**
  Remaining Phase 9+ candidates: context compaction, project persistence,
  and cross-client continuity follow-ups (explicitly out of scope for Phase 8).
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
