# P9 — Phase 2 Plan (P9b): Context Manager, Summarizer & Summary Verifier

Date: 2026-08-06.
Status: **Plan document — no code.** Approval required before any
implementation. `PROJECT_LOG.md` is not modified. No commits. Plan stays
uncommitted.

Prerequisites (all approved):
- `docs/platform-p9-research-plan.md` — research + approved decisions.
- `docs/platform-p9-architecture-design.md` — approved architecture (incl.
  §16 Non-Goals / P10 boundary).
- `docs/platform-p9-implementation-plan.md` — phase plan (P9a–P9e).
- **P9a complete and committed** (`e8cb667`): full suite **2060 passed /
  22 skipped** post-commit.

Workflow for every phase: **Audit → Plan → Approval → Implementation →
Tests → Commit**; the full regression gate runs at each phase end.

---

## 1. Current P9 implementation status

P9a (Foundation & schema) is **complete and committed** (`e8cb667`). The
post-commit regression gate is green: full suite **2060 passed /
22 skipped**.

Repo state verified during this audit:
- `app/services/` contains the P9a deliverables
  (`conversation_store.py`, `continuity_flusher.py`) and **none** of the
  P9b/P9c/P9d service modules. `context_manager.py`, `summarizer.py`,
  `summary_verifier.py`, `handoff.py`, `continuity_recovery.py`, and
  `continuity_headers.py` **do not exist**.
- `app/api/chat.py`, `app/api/openai.py`, `app/services/async_chat_service.py`,
  and `app/services/chat_service.py` contain **zero** continuity or
  conversation references (verified by search) — the handoff wiring of
  P9c has not started.
- `docs/platform-db-schema.md`, `docs/architecture.md`, and `README.md`
  contain **zero** continuity / conversation / v7 references (verified by
  search) — the P9d documentation pass has not started.
- `app/ui/` contains no continuity references — the P9d TUI diagnostics
  surface has not started.
- All 13 continuity config keys, the schema-v7 migration, the memory-
  contract surfaces, and the `.env.example` documentation are in place
  from P9a.

## 2. What P9a completed

From the approved P9a scope (implementation plan §1, §12):
- **Schema v7**: `MIGRATIONS[7]` in `platform_store.py` — 5 additive
  tables (`conversations`, `conversation_turns`, `summaries`,
  `compaction_records`, `project_state`) + 5 indexes; v6→v7 additive
  upgrade; v8 refused; `integrity_check` ok; reopen idempotent.
- **Continuity settings registry**: 13 keys in `app/core/config.py` +
  `app/core/config_spec.py` (all restart-required), golden test bumped
  (103→116), `.env.example` documents each with defaults.
- **Memory-contract surfaces**: 7 new `DURABLE` surfaces in
  `memory_contract.py`; `FORBIDDEN_KEYS` / `contains_never_captured()`
  unchanged and reused as the hard guard.
- **ConversationStore**: key-scoped create/get/list/archive/prune,
  append_turn, record_summary (redacted + bounded, dedupe on
  `(conversation_id, up_to_seq)`), record_compaction, update_project_state,
  counts/stats; single guarded connection; WAL + `busy_timeout 5000`;
  best-effort audit rows.
- **ContinuityFlusher**: write-behind daemon thread, `start/stop/flush/
  prune_now`, consecutive-failure tracking; lifespan start + startup prune
  + final shutdown flush.
- **CLI**: `relay conversations [list|show|archive|prune]` (metadata only;
  prints `continuity disabled` when the flag is off — P9a DoD).
- **Metrics + audit**: `relay_continuity_*` counters/gauges; 7
  `continuity.*` `EVENT_ACTIONS`.
- **Feature-flag boundary**: everything is inert when
  `CONTINUITY_ENABLED=false`; P0–P8 behavior unchanged (parity suite
  green).

## 3. Remaining P9 gaps

Audit-verified remainder of the approved roadmap:

| Phase | Remaining work |
| --- | --- |
| **P9b** | `ContextManager` (token estimation, budget split, tail serialization, compaction orchestration), `summarizer` (extractive default + optional `llm`), `summary_verifier` (structural invariants + redaction hard guard), overflow retry helper. |
| **P9c** | `handoff.py` (`HandoffCoordinator`: envelope assembly, switch caps); integration into `async_chat_service` / `chat_service` candidate walks; header plumbing in `api/chat.py` + `api/openai.py`; SSE `relay:model_switched`; `X-Relay-Conversation-Id` response header; `continuity_headers.py`. |
| **P9d** | `continuity_recovery.py` (turn resume, replay caps, degradation ladder), resume/SSE protocol, `relay conversations`+TUI diagnostics completion, docs (`architecture.md`, `platform-db-schema.md`, README). |
| **P9e** | Security/privacy adversarial pass, redaction sweep, full gate + RC suite, `PROJECT_LOG.md` update only at the final release commit. |

Also still open from the audit:
- No compaction/summarization metric increments yet (P9a added only
  flush/prune/enabled counters).
- No context-envelope dataclasses in `app/models/continuity.py`.

## 4. Recommended next phase

**P9b — Context manager (as approved in the roadmap).** The audit found no
reason to reorder. P9b is the correct next milestone; see §5 for why.
Two small scope clarifications, proposed for approval (no roadmap change):

1. **`continuity_headers.py` stays out of P9b.** It is wire-format
   validation and is only exercised once HTTP/SSE plumbing exists (P9c).
   P9b remains wire-agnostic, which keeps it purely unit-testable.
2. **No facade wiring in P9b.** `ContextManager` / `summarizer` /
   `summary_verifier` ship as standalone, importable modules instantiated
   by tests. `app/core/relay.py` is not modified until P9c, when
   `HandoffCoordinator` actually consumes them. This keeps the P9b change
   set reviewable and guarantees zero hot-path effect.
3. **Metric increments deferred to P9c.** Adding `relay_continuity_
   compactions_total` / `_summarization_failures_total` now would be dead
   code (nothing invokes P9b in production until P9c wires the
   coordinator). The counters are defined in P9c.

## 5. Why this phase should come next

1. **Dependency order.** P9c's `HandoffCoordinator` assembles its context
   envelope from a *compacted context* (summary + tail), which is exactly
   what P9b's `ContextManager` + `summarizer` produce. P9d's recovery reads
   turns and summaries to resume — the summaries are P9b output. P9b is the
   prerequisite for both.
2. **Lowest risk increment on the critical path.** P9b is pure logic and
   deterministic math: no new I/O, no SQLite access, no HTTP/SSE surface,
   no API contract change, no config changes (all budget keys already exist
   from P9a), no schema change. It is fully unit-testable in isolation and
   cannot regress the hot path.
3. **It is the core algorithmic risk of P9.** The estimation/budget/split/
   tail math and the extractive summarizer are where correctness bugs
   (over-budget contexts, malformed summaries, redaction gaps) would be
   most expensive to find later. Landing them now, green before any wiring,
   de-risks P9c/P9d.
4. **Approved roadmap order.** P9a→P9b→P9c→P9d→P9e is the approved plan
   (§1 of the implementation plan); the audit confirms the phases map
   cleanly to the remaining gaps in §3.

## 6. Detailed technical scope (P9b)

New services in `app/services/` (pure logic unless noted; **never invoked
from chat request paths in P9b**):

### 6.1 `app/services/context_manager.py` — `ContextManager`
- `estimate_tokens(text) -> int`: `max(1, len(text) // CHARS_PER_TOKEN)`
  (default 4), reading the existing `settings.continuity_chars_per_token`.
  Deterministic; no I/O.
- `budget_split(budget, reserve, summary_share) -> (summary_budget,
  tail_budget)`: `summary_budget = floor((budget - reserve) *
  summary_share)`, `tail_budget = budget - reserve - summary_budget`.
  All inputs from existing settings (`CONTINUITY_CONTEXT_TOKEN_BUDGET`,
  `CONTINUITY_OUTPUT_RESERVE_TOKENS`, `CONTINUITY_SUMMARY_SHARE`).
- `compact(turns, budget, params) -> CompactionResult`: pure split —
  newest items into the tail up to `tail_budget` tokens **and** the
  `CONTINUITY_TAIL_MAX_ITEMS` item cap; older items feed the summary.
  Returns `CompactionResult(summary_block, tail, up_to_seq, stats)`.
  Must never raise on any input; over-budget results degrade structurally.
- `serialize_tail(tail) -> str`: deterministic, bounded serialization of
  the tail for the envelope (P9c consumes it; P9b only produces it).
- `should_retry_compacted(error) -> bool`: the **overflow retry helper**
  — a pure decision mapping a context-overflow signal to
  "retry once with the compacted context" vs "degrade to current-request-
  only". The actual HTTP retry wiring is P9c.

### 6.2 `app/services/summarizer.py`
- `extractive_summarize(turns, budget) -> SummaryBlock`: deterministic,
  structured (goal/context, decisions, outcomes, unresolved items),
  stamped with `SUMMARY_VERSION`, provenance (`method="extractive"`,
  token counts), bounded by `CONTINUITY_SUMMARY_MAX_CHARS` and the
  summary budget. Operates on **turn metadata** — never raw prompts or
  responses.
- `llm_summarize(...)`: optional wrapper, used **only** when
  `CONTINUITY_SUMMARIZER_MODEL` is set. A single serial provider call
  through the existing client-registry abstraction. On **any** failure
  (unavailable provider/model, timeout, error, redaction-suspicious
  output) it **falls back to extractive** (provenance records the
  fallback). Default config (`""`) means the llm path is never entered.
- `summarize_and_persist(store, conversation_id, key_id, turns, budget)`
  -> `SummaryBlock`: off-hot-path orchestration that (1) runs the
  summarizer, (2) **verifies** via `summary_verifier.verify()`, (3) only
  on verification success calls the existing
  `ConversationStore.record_summary` / `record_compaction`. Refused
  summaries are not persisted and yield a failed outcome — never a
  partial write. All persistence goes through existing store methods; no
  new DB access.

### 6.3 `app/services/summary_verifier.py`
- `verify(summary, conversation, turns) -> bool` — pure structural checks:
  - `up_to_seq` references existing turns (`turns` seq set contains it);
  - `conversation_id` matches the supplied conversation;
  - `up_to_seq` strictly increases per conversation vs the latest stored
    summary (no reorder / duplicate ranges);
  - token/turn counts referenced are consistent with turn metadata;
  - `SUMMARY_VERSION` is known (unknown version → reject, treat as
    no-context, fail-safe);
  - **redaction hard guard**: `not contains_never_captured(summary)` —
    a summary carrying a forbidden key (e.g. `content`, `message`,
    `response`, `api_key`) is rejected.

### 6.4 `app/models/continuity.py` (additive)
- Add `CompactionResult` and `SummaryBlock` dataclasses with
  `to_dict()` exports that pass `contains_never_captured()` (safe key
  names, mirroring the existing `SummaryRecord` pattern).
- **No change** to existing records and **no `MODEL_VERSION` bump**
  (additive shapes only).

### 6.5 Explicitly out of scope for P9b
- No `platform_store.py` / schema change, no migration.
- No new config keys (all budget keys exist from P9a).
- No `handoff.py`, `continuity_headers.py`, `continuity_recovery.py`.
- No changes to `api/chat.py`, `api/openai.py`, `async_chat_service.py`,
  `chat_service.py`, `log_service.py`, `core/relay.py`, `main.py`, `cli/`,
  `ui/`, `README.md`, or the schema/architecture docs.
- No metrics counters (deferred to P9c, §4).

## 7. Files expected to change

### New files
| File | Purpose |
| --- | --- |
| `app/services/context_manager.py` | `ContextManager`: estimation, budget split, compaction, tail serialization, overflow retry decision |
| `app/services/summarizer.py` | extractive summarizer + optional `llm` wrapper with fallback + verify-then-persist orchestration |
| `app/services/summary_verifier.py` | structural invariants + redaction hard guard |
| `tests/test_continuity_context.py` | estimation/budget/split/tail math, overflow decision, determinism, no-raise guarantees |
| `tests/test_continuity_summary.py` | extractive output + provenance, `llm` path with a mock provider, fallback, bounds/redaction, verify-then-persist behavior |
| `tests/test_continuity_verifier.py` | accept/reject structural cases, redaction hard guard, unknown-version rejection, monotonic `up_to_seq` |

### Modified files
| File | Change |
| --- | --- |
| `app/models/continuity.py` | additive `CompactionResult` / `SummaryBlock` dataclasses (no `MODEL_VERSION` change) |
| `tests/test_memory_contract.py` | *optional* — additive negative assertions that the new `to_dict()` exports pass `contains_never_captured()` (default: keep inside the new P9b suites to avoid touching golden tests) |

No other existing production file is touched in P9b.

## 8. Files that must remain untouched

- `PROJECT_LOG.md` (workflow: updated only at the final release commit).
- `app/services/platform_store.py` (no schema change in P9b).
- `app/core/config.py`, `app/core/config_spec.py` (no new keys).
- `app/core/relay.py`, `app/main.py` (facade wiring deferred to P9c).
- `app/api/chat.py`, `app/api/openai.py`.
- `app/services/async_chat_service.py`, `app/services/chat_service.py`,
  `app/services/log_service.py`, `app/services/continuity_flusher.py`,
  `app/services/conversation_store.py`.
- `app/cli/*`, `app/ui/*`.
- `.env.example` (already documents all 13 keys).
- All `docs/*` plan/audit documents stay **uncommitted**.

## 9. Database/schema implications

- **None.** P9b adds no DDL, no migration, no index, no column. Schema v7
  and all five continuity tables already exist from P9a.
- The only persistence is through the existing `ConversationStore`
  methods (`record_summary`, `record_compaction`), invoked by the
  off-hot-path `summarize_and_persist` orchestration — no new connection,
  no new table, no single-writer rule change.
- Because there is no schema change, the constraint "no schema changes
  without a migration plan" is satisfied trivially: no migration is
  needed or written.

## 10. Security and privacy risks

| Risk | Mitigation (built into P9b) |
| --- | --- |
| Summary content leaks verbatim prompt/response material | Summarizer operates on **turn metadata only**; verifier redaction hard guard (`contains_never_captured`) rejects before any persistence; store's existing `redact_text` + length bound is the last line of defense |
| Over-budget context sent to a fallback model | Deterministic `budget_split` + summary truncation; `compact` never exceeds `CONTINUITY_CONTEXT_TOKEN_BUDGET - reserve`; math is unit-tested including edge cases |
| Unknown `SUMMARY_VERSION` misread as valid context | Verifier rejects unknown versions (fail-safe: no-context, never silent misread) |
| `llm` summarizer emits redaction-suspicious output or is unavailable | Fallback to extractive; provenance records the method; summarization never runs on the hot path and never fails a request |
| Compaction/summarization loop | At most one preflight compaction + one overflow retry per request decision (pure helper); summaries deduped by `(conversation_id, up_to_seq)` via existing store UNIQUE constraint |
| Audit / negative-test drift | New export shapes (`CompactionResult`, `SummaryBlock`) must pass `contains_never_captured()`; asserted in the new suites |

Threat model (§11 of the architecture design) is unaffected: P9b adds no
wire surface, no headers, no resume tokens, and no new persistence — the
P9b surfaces are internal pure-logic modules.

## 11. Testing strategy

New suites, wired into `[tool.pytest]`:

- `tests/test_continuity_context.py`
  - `estimate_tokens` math incl. empty/1-char/unicode boundaries;
  - budget split with the default share/reserve and edge shares (0, 1);
  - tail/summary split respects both the token budget and the
    `CONTINUITY_TAIL_MAX_ITEMS` cap; overflow degrades structurally;
  - `compact` never raises and returns bounded output on adversarial
    input; determinism (same input → same output);
  - `serialize_tail` deterministic and bounded;
  - `should_retry_compacted` decision matrix (overflow→retry, else
    degrade).
- `tests/test_continuity_summary.py`
  - extractive output structure + `SUMMARY_VERSION` + provenance;
  - bounds: output ≤ `CONTINUITY_SUMMARY_MAX_CHARS` and summary budget;
  - `llm` path with a mock provider: happy path, and fallback to
    extractive on failure/unavailable/redaction-suspicious output;
  - `summarize_and_persist`: verified summary persisted via store
    (dedupe no-op on same range), unverified summary refused with no
    partial write; exported dicts pass `contains_never_captured()`.
- `tests/test_continuity_verifier.py`
  - accept: structurally valid summary;
  - reject: unknown `up_to_seq`, mismatched conversation, non-monotonic
    `up_to_seq`, unknown `SUMMARY_VERSION`, inconsistent token counts,
    forbidden key present (`contains_never_captured` hard guard);
  - verifier is a pure function (no I/O).

Regression gate:
- Full suite green (2060 passed / 22 skipped + new P9b tests).
- RC suite (`tests/test_rc_validation.py`) green.
- Flag-off parity (P9a) unchanged — P9b adds no reachable behavior with
  `CONTINUITY_ENABLED=false`.

## 12. Rollback strategy

- **Zero-risk rollback**: P9b is additive, pure-logic modules plus tests
  and two additive dataclasses. No production path invokes them in P9b,
  so removing the commit reverts Relay to byte-identical P9a behavior
  with no data or schema consequences.
- **No schema rollback needed**: no migration is introduced.
- **Feature-flag rollback**: even after P9c wires the coordinator, the
  instant rollback remains `CONTINUITY_ENABLED=false` (unchanged).
- **Commit discipline**: P9b lands as one focused commit (or reviewed
  sub-steps) after approval; plan documents stay uncommitted.

## 13. Acceptance criteria (P9b DoD, from implementation plan §12)

1. Estimation / budget / split / tail unit tests green, **including
   overflow math** (context > budget - reserve compacts; a second overflow
   degrades, never loops).
2. Extractive summarizer tests green (structured, versioned, bounded,
   provenance recorded); `llm` path tested with a mock provider; fallback
   to extractive on any failure is green.
3. Verifier accept/reject tests green, including the **redaction hard
   guard** (`contains_never_captured` reject) and unknown-version
   rejection.
4. `summarize_and_persist` refuses unverified summaries with no partial
   write; persistence flows only through existing `ConversationStore`
   methods.
5. **No I/O on the hot path**: P9b introduces no code reachable from
   chat request paths; `compact` never fails a request.
6. No schema change, no config change, no `relay.py` / API / CLI / UI
   change in P9b.
7. Full suite + RC suite green; flag-off parity unchanged.

## 14. How P9b improves model-switching continuity without entering P10

- **Direct contribution to handoff**: P9b provides the *content* of model
  continuity — a budget-constrained compacted context (structured summary
  + verbatim tail) that P9c's `HandoffCoordinator` will attach to a
  fallback candidate's call. Without P9b, a fallback model would keep
  receiving only the current request; with P9b in place, the mechanism
  exists for "switch models without losing the thread."
- **Mechanism, not orchestration**: P9b produces deterministic, pure,
  per-conversation context blocks. It does not issue a second call,
  compare outputs, or route anything.
- **Single-conversation, single-candidate discipline held**: every P9b
  path operates on one conversation's turn metadata at a time. There is
  no agent, no spawn, no inter-agent channel, no parallel execution, no
  delegation, and no filesystem access. The optional `llm` summarizer is
  a single serial provider call (and off by default), not parallel model
  collaboration.
- **No new wire surface**: no headers, no SSE events, no endpoints in
  P9b — so nothing new is exposed to clients, and the P10 boundary
  (`§16` Non-Goals) is untouched.

---

**Stop condition**: this plan is delivered for approval. Implementation
does not start until approved. No code, no commits, `PROJECT_LOG.md`
untouched.
