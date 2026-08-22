# PROGRESS.md — Relay session state

High-level status for cross-tool handoffs. Updated at session boundaries.
Base all work on: current code + this file + `git log --oneline -10`.

## Current state

- **Branch:** `master`.
- **HEAD:** `c1f8840125087ec87d00976bf4b6f8d9db8a9557`
  (`fix: bump python-dotenv to patched 1.2.2 release`).
- **origin/master:** `619dbac952ef1aa9d95819dcf7024c777366d258` —
  master is **20 commits ahead, 0 behind**.
- **Push status:** **NOT PUSHED.** The repository is at a verified
  **pre-push remediation checkpoint** (see next section).
- **Working tree:** tracked files clean. The intentionally preserved,
  untracked context documents `OVERNIGHT_REPORT.md` and
  `PHASE_15_PROPOSAL.md` remain present.
- **CI:** the current workflow tests Ubuntu Python 3.10/3.11/3.12/3.13,
  Windows Python 3.12, and packaging on Ubuntu Python 3.12. CI run
  `32571822641` is green across all six jobs for `57af339`
  (historical). The remediation history ending at `c1f8840` has **not
  run CI yet** — verify CI after the controlled push.
- **Recent history:** Codex/OpenCode adversarial security/reliability
  remediation gate (20 commits, `619dbac..c1f8840`) completed,
  independently verified PASS WITH NOTES; earlier Phase 16 Stage 2A
  remediation through `57af339`; Phase 16 Stage 1 remediation at
  `68921c1`; Phase 14 streaming turn accounting at `8e53fd7`;
  Phase 15 Stages A+B design system at `3ab1de9`; Stage C core screens
  at `6ba1dc3`; Stage C hotfix at `608ee0f`; Stage D chat screen &
  streaming redesign at `d520a11`; Stage E diagnostics sub-tabs at
  `1d05f9a`; Stage F config collapsibles + wizard at `726f09c`; Stage G
  polish & final cleanup at `e1661ec`; post-Stage-G wizard CI-regression
  hotfix at `66cb775`.
- **Phase 15 Stage G (polish & final cleanup) is COMPLETED** —
  CSS dead-class cleanup, .chat-assistant fix, status transitions, tab
  description subtitles. Follow-up hotfix `66cb775` resolved a Stage G
  CI regression (see What is done).
- **Phase 15 Stage F (config collapsibles + wizard) is COMPLETED** —
  collapsible sections and guided wizard implemented, committed at
  `726f09c`, 36 config tests pass (was 20).
- **Phase 15 Stage E (diagnostics sub-tabs) is COMPLETED**
  — implemented and verified at `1d05f9a` (see What is done).
- **Phase 15 Stage D (chat screen & streaming redesign) is COMPLETED**
  — implemented and verified at `d520a11` (see What is done).
- **Phase 15 Stage C (core screens redesign) is COMPLETED** —
  implemented and verified at `6ba1dc3`/`608ee0f` (see What is done).
- **Phase 15 Stages A+B (design system foundations + dead code cleanup)
  is COMPLETED** — implemented and verified at `3ab1de9` (see What is
  done).
- **Phase 14 (streaming turn accounting) is COMPLETED** — implemented
  and verified at `8e53fd7` (see What is done).
- **Baseline suite:** pre-remediation baseline was 2777 passed, 8 skipped,
  0 failed. Stage 1 added seven authentication regression tests; local
  full-suite verification passed on Python 3.10 (2771 passed, 21 skipped)
  and Python 3.12 (2772 passed, 20 skipped), with no failures.
- **Release state:** version `1.0.0rc1`; Phase 15 Stages A–G complete;
  Phase 16 Stage 1 and Stage 2A remediation complete; `v1.0.0` is not
  released. Phase 17 has not started.
- **Remote:** `github.com/Mo-H101/relay` (private, branch `master`).
  Workflow: pull before starting, commit + push at natural checkpoints,
  only one tool edits the repo at a time.

### Adversarial remediation gate — completed, pre-push checkpoint

- **Baseline:** `619dbac952ef1aa9d95819dcf7024c777366d258`
  (`docs: reconcile Stage 2A bridge state`).
- **Verified remediation HEAD:** `c1f8840125087ec87d00976bf4b6f8d9db8a9557`
  via a linear 20-commit history (`619dbac..c1f8840`, no merges).
  The gate is COMPLETE; the branch has NOT been pushed.
- **What was fixed (Codex/OpenCode remediation, independently
  re-verified):** continuity history/state growth; flusher overflow/
  bookkeeping loss; cross-project coalescing collision; recovery
  pending-token growth/cleanup; bounded upstream response/stream
  lifetime (byte/chunk/seconds budgets on sync and async wire paths);
  provider error-body leakage; Ollama exception leakage; TUI
  provider-error leakage; authentication CPU amplification (throttle
  before store scan); non-finite key expiration; model-catalog
  amplification; retry/attempt amplification; request structure bounds;
  diagnostics/persistence information leakage; Gemini API-key URL
  exposure (header auth); provider `base_url` validation incl. lazy
  port parse; HTTP 408 classification as timeout; `httpcore>=1.0.9`
  CVE floor (CVE-2025-43859); `python-dotenv` 1.2.2 bump. A late wiring
  regression from `fddf9eb` (event hooks breaking sync provider calls)
  was caught and fixed via dedicated `bounded_get/post/stream`
  transport shims with real-socket regression tests. Redirect/SSRF was
  verified a non-issue (httpx redirect following is never enabled).
- **Final independent verification:** full relevant suite
  **2826 passed / 15 skipped / 0 failed**; targeted security suite
  **526 passed / 6 skipped**; adversarial attack passes (auth-throttle
  7/7, continuity combinations 5/5, transport budgets 4/4); no tracked
  changes; no suspicious artifacts/secrets. Verdict:
  **PASS WITH NOTES — safe to push.**
- **Non-blocking follow-ups (do NOT address during this checkpoint):**
  1. `ConversationStore.close()` auto-reopens — minor operational/
     reliability defect, not security relevant, not release blocking.
  2. Starlette 0.47.3 advisory family — Relay's Host/routing-path
     vector is mitigated by reading routing-scope path instead of
     `request.url.path`; no reachable exploitable path identified;
     FastAPI/Starlette upgrade remains a prioritized follow-up.
  3. `test_setup_adapter_masks_key_input` timing-sensitive UI flake
     under load (passes standalone/file-level and on rerun).
  4. Remaining pip-audit findings are environment/tooling-only,
     unreachable from Relay's API surface, or already mitigated in app
     code; no dependency upgrades now.
- **Next transition (in order, not started yet):** preserve this
  checkpoint → push the verified remediation history → run/verify CI
  on pushed HEAD → if green, proceed to the next pre-release phase →
  continue planned benchmark/security/reliability and installer/
  distribution work.

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
- **`test_ui_providers.py::test_setup_adapter_masks_key_input`
  timing-sensitive flake under full-suite load** (observed once in
  three full-suite runs on the remediation tree; textual headless
  driver error). Passes standalone, passes file-level (12/12), and
  passed on the immediate full-suite rerun. Not a deterministic
  regression; left unmodified. Revisit pilot timeouts only if it
  recurs.

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
- **Phase 16 Stage 1 remediation (`68921c1`):** R-C1 Host-header path
  confusion was fixed by using the raw ASGI scope path for all
  authentication/public-path and scoped-key decisions. Regression coverage
  confirms malformed Host and forwarded-header variants cannot bypass
  protected routes while public routes and valid credentials continue to
  work. R-C2 Python 3.10 compatibility was restored by replacing the six
  `datetime.UTC` uses with `timezone.utc`; the application starts and the
  targeted/full verification passes on supported Python versions.
  Stage 2A subsequently addressed R-C3, R-C4, R-C6, and R-C11; the
  remaining findings are recorded below.
- **Phase 16 Stage 2A remediation (completed at `57af339`):**
  - **R-C3 — authentication CPU amplification: COMPLETE** at `9196c53`.
    Authentication now performs one KDF verification/classification scan
    while preserving the existing key format, parameters, constant-time
    comparison, bootstrap behavior, and revoked/expired handling.
  - **R-C4 — request/resource admission controls: COMPLETE** at `c77ae16`.
    Expensive chat execution paths use bounded, process-local, non-queuing
    admission with release on success, error, cancellation, and streaming
    cleanup. Health, liveness, and admin behavior remain unchanged.
  - **R-C6 — provider error redaction boundary: COMPLETE** at `fe9256b`
    and corrected at `57af339`. External and persisted surfaces receive
    stable safe classifications rather than provider exception strings,
    response bodies, credential-bearing URLs, prompts, or headers. The
    fixed `No candidates to try.` Relay outcome remains safe and compatible.
  - **R-C11 — provider registration visibility: COMPLETE** at `f302c85`.
    Registration, disabled, initialization-failed, and discovery-failed
    states are visible through safe structured health, diagnostics, provider,
    and TUI surfaces while optional-provider failures remain non-fatal.
  - **CI correction:** `f302c85` exposed seven stale R-C6 expectations in
    the full suite. `57af339` corrected those expectations and the safe
    `No candidates to try.` boundary behavior. CI run `32571822641` is
    green on Ubuntu Python 3.10/3.11/3.12/3.13, Windows Python 3.12, and
    packaging.
  - **Verification:** local non-packaging suite `2759 passed, 20 skipped`;
    corrected seven-test regression `7 passed`; provider/API regression
    `45 passed`.
- **Phase 16 findings intentionally not addressed in Stage 2A:** R-C5,
  R-C7, R-C8, R-C9, and R-C10 remain deferred. No Phase 17 work has
  started.
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
    passed, 8 skipped, 0 failures full local suite (Python 3.13).
- **Phase 11 (provider transport error redaction, implemented + verified,
  committed at `0c12049`):**
  - **Transport error redaction** (`app/providers/openai_compat_client.py`,
    `app/providers/anthropic_client.py`, `app/api/openai.py`):
    all `str(exc)` sites in both provider clients (14 each, 28 total)
    wrapped with `redact_text()` to prevent credential leakage through
    transport error messages. The non-streaming exception handler in
    `openai.py` (line 611) also updated to use `redact_text(str(exc))`.
    Raw exception strings (containing hostnames, ports, IPs, API keys)
    never reach API responses or logs at WARNING+ level.
  - **Tests** (`tests/test_transport_error_redaction.py`, 20 regression
    tests): covers both clients and the redaction layer.
  - **Verification:** all 20 transport redaction tests pass; no regression
    on existing provider error tests.
- **Phase 12 (provider error hardening + streaming overflow, implemented
  + verified, committed at `35f3823`/`c34d856`):**
  - **Provider error hardening (`35f3823`):** Gemini client
    (`app/providers/gemini_client.py`) — wrapped all 14 `httpx.HTTPError`
    sites in `safe_error_body` to redact API keys from exception messages.
    SSE stream error handler — added `redact_text` defense-in-depth.
    `chat_service.py` — fixed sync `_try_once_messages` to copy payload
    and override model (matching async behavior). `continuity_recovery.py`
    — wrapped `reconcile()` `_states` writes in `self._lock`. Ollama
    client (`app/providers/ollama_client.py`) — surfaced in-stream errors
    in `chat_stream`/`achat_stream`. 14 regression tests added across 4
    test files.
  - **Streaming context-length overflow (`c34d856`):**
    `app/services/async_chat_service.py` and `app/services/chat_service.py`:
    all four streaming entry points (`chat_across_stream`,
    `chat_across_stream_messages`, and their async counterparts) now retry
    once on context-length overflow with a compacted prompt. The overflow
    rebuild happens before any data is sent to the client, so the retry
    is invisible to callers. 50 tests in `test_continuity_overflow.py`.
  - **Verification:** all Phase 12 tests pass; no regressions.
- **Phase 13 (CI pipeline stabilization, implemented + verified,
  committed at `04890d8`):**
  - **UI test executor hang fix** (`tests/test_ui_providers.py`,
    `.github/workflows/ci.yml`): wrapped the assertion in
    `test_setup_adapter_masks_key_input` with try/finally so that
    `pilot.press('escape')` always fires — even if the PromptScreen DOM
    composition race causes the password-mode assertion to fail before
    the escape path is reached. Without this, the ThreadPoolExecutor
    worker blocks forever on `threading.Event.wait()` (observed as a
    1h52m hang on Windows CI). Also added `timeout-minutes: 15` to the
    CI test job as a defensive backstop.
  - **Verification:** CI jobs complete within timeout; no hang observed
    in subsequent runs.
- **Phase 14 (streaming turn accounting, implemented + verified,
  committed at `8e53fd7`):**
  - **Streaming provisional turn lifecycle** (`app/services/handoff.py`):
    during streaming responses, a provisional turn is created with status
    `pending` before the first chunk. On successful completion (all chunks
    received), the turn is committed with status `completed`. On error or
    client disconnect, the turn is rolled back (status `failed`, seq not
    advanced). This prevents partial-stream turns from corrupting the
    conversation sequence.
  - **Turn sequencing in streaming:** `TurnContext.start_streaming_turn()`
    atomically claims the next `seq` number. `commit_streaming_turn()` or
    `rollback_streaming_turn()` finalize or discard. No double-commit:
    if the turn is already committed, commit is a no-op.
  - **Model-lineage tracking in streaming:** the streaming turn captures
    `(provider, model)` from the first successful chunk, so the conversation
    anchor is updated even when the stream encounters mid-stream errors.
  - **Tests** (`tests/test_continuity_phase14.py`, 39 tests):
    provisional lifecycle (pending → completed, pending → failed rollback);
    double-commit idempotency; seq advancement correctness; model
    anchor updates; client-disconnect rollback; mid-stream error rollback;
    streaming + non-streaming parity; concurrency safety (simultaneous
    streaming and non-streaming requests on the same conversation).
  - **Verification:** 39/39 Phase 14 tests pass; full suite 2754 passed,
    8 skipped, 0 failed (Python 3.13, ~8m34s).
- **Phase 15 Stage D (chat screen & streaming redesign, implemented +
  verified at `d520a11`):**
  - **Streaming O(n²) fix** (`app/ui/widgets/chat_view.py`): replaced
    `self._stream_parts: list[str]` with `self._stream_body: str` and
    `"".join()` concatenation with `+=` for O(n) chunk appending. Removes
    quadratic cost on long streaming responses.
  - **Streaming indicator** (`chat_view.py`): replaced single static
    `STREAM_MARKER` with cycling three-dot animation
    `_STREAM_FRAMES = ("●○○", "○●○", "○○●")` that advances on each
    `_render_stream()` call.
  - **Empty state** (`chat_view.py`): replaced bare "No messages yet"
    with rich `Text` showing "Relay Chat" heading + keyboard hints
    (r/m/Ctrl+T). Removed on first message via `.chat-empty-state` query.
  - **Copy last response** (`chat.py`): `_copy_to_clipboard()` tries
    xclip/xsel/wl-copy/pbcopy; `action_copy_last()` + `Binding("C",
    priority=True)` with status feedback.
  - **Mode switcher labels** (`chat.py`): buttons show `● Random`/`○ Model`
    with variant toggling (primary=default).
  - **Markdown rendering** (`chat_view.py`): finalized assistant responses
    mount a Textual `Markdown(body)` widget below the badge-only
    `Static`, replacing the previous monolithic `Text` block.
  - **Removed "Back to Dashboard" button** from `chat.py` (Escape still
    works via existing binding).
  - **CSS** (`base.tcss`): new classes `.chat-empty-state`,
    `.chat-assistant-header`, `.chat-user`, `.chat-error`, `.chat-system`;
    `#mode-random`/`#mode-model` min-width for consistent toggle sizing.
  - **Tests** (`tests/test_ui_chat.py`): 15 tests (was 14). Removed
    `test_back_to_dashboard_button`. Added tests for copy unavailable,
    copy nothing, empty state guidance, mode switcher labels, markdown
    widget in finalized response. Updated `_transcript()` to also query
    `Markdown` widgets via `.source`.
  - **Verification:** 15/15 chat tests pass; full suite 2763 passed,
    8 skipped, 0 failed (Python 3.13, ~12m13s). Multi-size
    verification: 80×24, 100×30, 120×40 all green.
- **Phase 15 Stage E (diagnostics sub-tabs, implemented + verified):**
  - **TabbedContent sub-tabs** (`app/ui/screens/diagnostics.py`):
    replaced monolithic diagnostics scroll with four `TabPane` children
    (System Info, Provider Health, Continuity, Decisions) inside a
    shared `TabbedContent(id="diag-tabs")`. Each pane is a dedicated
    `_system_info_pane()`, `_provider_health_pane()`,
    `_continuity_pane()`, `_decisions_pane()` generator method.
  - **`action_tab_sub(name)`** for keyboard navigation via number keys
    `1`–`4` (priority bindings, show=False).
  - **`decision_records()`** added to `ServiceFacade`
    (`app/ui/data.py`): wraps `DecisionRecordStore.snapshot(limit=...)`
    for the decisions sub-tab.
  - **CSS** (`app/ui/styles/base.tcss`): new `#diag-tabs`,
    `TabPane`, and `TabbedContent DataTable` height rules for consistent
    sub-tab content sizing.
  - **Tests** (`tests/test_ui_diagnostics.py`): 18 tests (was 14).
    Added `test_diagnostics_has_four_sub_tabs`,
    `test_diagnostics_tab_switching`, `test_diagnostics_decisions_table_renders`,
    `test_diagnostics_provider_health_tab_visible`.
  - **Verification:** 18/18 diagnostics tests pass; full suite 2767
    passed, 8 skipped, 0 failed (Python 3.13, ~12m03s). Multi-size
    verification: 80×24, 100×30, 120×40 all green.
- **Phase 15 Stage G (polish & final cleanup, implemented + verified):**
  - **CSS dead-class cleanup** (`app/ui/styles/base.tcss`): removed
    four confirmed dead CSS classes: `.chat-assistant-header` (never
    applied — `chat-assistant` was the actual class used), `.config-group`
    (class selector never applied; `#config-group-*` ID selectors
    unchanged), `.placeholder-title`, `.placeholder-note` (leftover from
    pre-redesign placeholder screens).
  - **Added `.chat-assistant` class** (`base.tcss`): the missing CSS class
    referenced by `chat_view.py` lines 112 and 134 for assistant message
    bubbles. Matching layout pattern with `.chat-user`, `.chat-error`,
    `.chat-system`.
  - **CSS transitions** (`base.tcss`): added `transition: color 200ms` to
    `.status-line` and `.config-note` for smooth status-color updates.
    Scoped to elements that benefit; no interference with readability.
  - **Tab description subtitles** (`app/ui/app.py`): added
    `_TAB_DESCRIPTIONS` dict with concise per-tab descriptions derived
    from the proposal's NOTES concept and current UI semantics. Subtitle
    updates on tab switch (`action_tab`), dashboard push (`on_mount`),
    and escape-to-dashboard (`action_go_dashboard`). Format:
    `AI gateway terminal · v{version} · {description}`.
  - **Tests:** full UI suite 137/137 passed; boundary tests including
    multi-size (100×30, 80×24, 60×20, 40×15) all passed; 120×40
    verified via headless pilot; compileall clean.
  - **Verification (at `e1661ec`, superseded):** 2773 passed, 11
    skipped, 0 failed locally, but CI runs failed on 2 wizard tests —
    see the post-Stage-G regression below.
- **Post-Stage-G CI regression fix (`66cb775`):** Stage G's first CI
  appearance (`726f09c` Stage F introduced the wizard; `e1661ec` Stage G)
  failed `test_wizard_navigation` and `test_wizard_finish_with_no_changes`
  on ubuntu (both) and windows (finish only), while passing locally.
  - **Root cause:** Textual's `Button._on_click` deliberately ignores
    clicks while the `-active` press effect is active
    (`active_effect_duration = 0.2s`). CI machines execute the tests'
    click+pause cycles in ~5–33ms, so every click after the first landed
    inside the debounce window and was silently dropped: navigation froze
    at step 1 and the wizard never reached its summary/Finish step.
    Reproduced deterministically by simulating CI-speed click gaps.
  - **Fix (production code only):**
    `app/ui/screens/config_wizard.py` sets
    `active_effect_duration = 0.0` on the wizard's Cancel/Back/Next
    buttons. Navigation controls must register every click — losing a
    rapid repeat press is a real UX defect, not just a test artifact.
  - **No test files were modified**, weakened, skipped, or given sleeps.
  - **Verification:** both tests pass individually; module 30 passed;
    full suite 2777 passed, 8 skipped, 0 failed; CI run `32524662538`
    fully green (5/5 jobs).
- **Phase 15 Stage F (config collapsibles + wizard, implemented +
  verified):**
  - **Collapsible sections** (`app/ui/screens/configuration.py`):
    replaced flat scrolling list of 7 display-group headers with
    `Collapsible` widgets. All 7 groups collapsed by default for
    scannability; user expands via click on title or keyboard Tab+Enter.
  - **Expand/collapse all** (`configuration.py`): `action_expand_all()`
    (`e` key) and `action_collapse_all()` (`E` key) toggle all 7
    collapsible sections at once.
  - **Configuration wizard** (`app/ui/screens/config_wizard.py`, new):
    `ConfigWizardScreen(ModalScreen)` with 5 guided steps: Welcome,
    Server Basics, Provider Setup, Task Routing, Review & Save. Uses
    `ContentSwitcher` for step navigation. Each step shows a focused
    subset of fields. Changes accumulated across steps and saved
    atomically via `ServiceFacade.save_config()`. Escape to cancel,
    Back/Next/Finish buttons. No domain logic duplicated — reads from
    the same `ConfigField` model.
  - **`config_wizard_fields(step_id)`** added to `ServiceFacade`
    (`app/ui/data.py`): returns `ConfigField` rows for a given wizard
    step, keeping the wizard screen free of `app.core` imports
    (boundary rule).
  - **Wizard button** on config screen controls bar (`#config-wizard`).
    Keyboard: `w` launches the wizard.
  - **CSS** (`app/ui/styles/base.tcss`): enhanced Collapsible title
    styling for config groups; new wizard modal styles (`#wizard-container`,
    `#wizard-body`, `#wizard-nav`, `.wizard-field`, `.wizard-label`, etc.).
  - **Tests** (`tests/test_ui_configuration.py`): 30 tests (was 20).
    Added: 7 collapsible groups present, all collapsed by default,
    expand-all, collapse-all, wizard button, fields present when
    collapsed, wizard smoke, wizard navigation, wizard finish with no
    changes, wizard step indicator.
  - **Verification:** 36/36 config+accessibility tests pass; full suite
    2777 passed, 8 skipped, 0 failed (Python 3.13, ~12m49s). Multi-size
    verification: 80×24, 100×30, 120×40 all green.
- **Fixes:** CI workflow trigger `main`→`master`; EmbeddedServer readiness
  poll (`app/core/server.py`); health-store freshness determinism;
  CI UI-test executor hang (`04890d8`); provider transport error
  redaction (`0c12049`); provider error categorization (`35f3823`);
  streaming context-length overflow detection (`c34d856`).
- **Docs:** `docs/implementation-audit.md` (request-path audit + phase
  status), `docs/capability-matrix.md`, configuration/known-limitations
  updated for the new flags; `OVERNIGHT_REPORT.md` (validation artifact,
  untracked).

## What is next (candidate backlog)

- **Phase 15 Stage E — Diagnostics sub-tabs:** COMPLETED (see What is
  done).
- **Phase 15 Stage F — Config collapsibles + wizard:** COMPLETED (see
  What is done).
- **Phase 15 Stage G — Polish & final cleanup:** COMPLETED (see What is
  done).
- **Phase 16 Stage 1 — R-C1/R-C2 remediation:** COMPLETED at `68921c1`.
- **Phase 16 Stage 2A — R-C3/R-C4/R-C6/R-C11 remediation:** COMPLETED at
  `57af339`; CI run `32571822641` is green. R-C5, R-C7, R-C8, R-C9, and
  R-C10 were intentionally not addressed in Stage 2A.
- **Phase 17:** NOT STARTED.
- **Live smoke testing:** restore valid API keys (NVIDIA key expired,
  OpenAI key quota-exhausted) and run end-to-end provider tests against
  real endpoints. This is the final validation gate before v1.0.0.
- **Release tagging:** create `v1.0.0` git tag after live smoke tests
  pass. Current version is `1.0.0rc1` (from `app/__version__.py`).
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
- Actual-decision records remain bounded in-memory; a durable schema is
  a later decision only if justified.

## Next architectural work (Phase 9+ candidates, completed)

- **Model handoff — COMPLETED in Phase 9B (`d65e802`).**
- **Empty-model continuity fix + conversation-states surface — COMPLETED
  in Phase 10A (`fdd5af7`–`d5eaba8`).**
- **Overflow-retry wiring — COMPLETED in Phase 10B (`64a660c`).**
- **Provider transport error redaction — COMPLETED in Phase 11
  (`0c12049`).**
- **Provider error categorization + streaming overflow — COMPLETED in
  Phase 12 (`35f3823`/`c34d856`).**
- **CI pipeline stabilization — COMPLETED in Phase 13 (`04890d8`).**
- **Streaming turn accounting — COMPLETED in Phase 14 (`8e53fd7`).**
- **Phase 15 Stages A+B (design system + dead code cleanup) — COMPLETED
  at `3ab1de9`.**
- **Phase 15 Stage C (core screens redesign) — COMPLETED at
  `6ba1dc3`/`608ee0f`.**
- **Phase 15 Stage D (chat screen & streaming redesign) — COMPLETED
  at `d520a11`.**
- **Phase 15 Stage E (diagnostics sub-tabs) — COMPLETED at
  `1d05f9a`.**
- **Phase 15 Stage F (config collapsibles + wizard) — COMPLETED at
  `726f09c`.**
- **Phase 15 Stage G (polish & final cleanup) — COMPLETED at
  `e1661ec`; CI regression hotfixed at `66cb775`.**
- Remaining deferred items: context compaction, project persistence,
  cross-client continuity follow-ups, AdaptiveWeights removal, durable
  decision-record schema (all explicitly out of scope for v1.0.0).
- **Remaining release gate (v1.0.0):** live smoke testing — restore
  valid API keys and run end-to-end provider tests against real
  endpoints; then, if validation succeeds, version bump + release
  tagging to `v1.0.0` (current version `1.0.0rc1`; `v1.0.0` NOT yet
  tagged).

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
- **Streaming turn lifecycle (Phase 14):** provisional turns are created
  atomically at stream start and committed only on successful completion.
  Failed/disconnected streams roll back the turn without advancing seq.
  The turn lifecycle is entirely within the continuity subsystem and
  never blocks the streaming response path — commit/rollback are
  post-response bookkeeping.
