# P9 — Phase 4 (P9d) Implementation Plan: Recovery, Retention & Operator Surfaces

Date: 2026-08-06.
Status: **Implementation plan — no code yet.** Approval required before any
implementation. `PROJECT_LOG.md` is not modified. No commits.

Prerequisites (all approved / landed):
- `docs/platform-p9-research-plan.md` — approved decisions (Option C memory,
  `CONTINUITY_ENABLED` default `false`, opaque headers).
- `docs/platform-p9-architecture-design.md` — approved architecture, §6
  handoff protocol, §8 S1–S9 recovery, §9 loop prevention, §16 P10 boundary.
- `docs/platform-p9-implementation-plan.md` — P9 phase split + §12 P9d DoD.
- `docs/platform-p9-phase4-audit.md` — **approved** P9d audit; six focus
  areas (interrupted-task recovery, resume after model failure,
  duplicate-work prevention, checkpoint design, retention/cleanup,
  operator visibility) mapped onto OpenCode / Codex / Cline / Continue.dev /
  Aider / SWE-agent.
- **P9a landed** (`e8cb667`): schema v7, config, memory contract,
  `ConversationStore`, `ContinuityFlusher`, facade wiring, lifespan hooks.
- **P9b landed** (`26ada25`): `ContextManager`, `summarizer`,
  `summary_verifier`, continuity dataclasses, overflow retry helper.
- **P9c landed** (`18e94da`): `HandoffCoordinator`, `continuity_headers`,
  chat/OpenAI header plumbing, additive `relay:*` SSE lines, turn commit.
- Full suite green: **2182 passed / 22 skipped** (P9c commit).

Hard constraints (unchanged from P9):
- **Option C memory model**: metadata, project state, decisions, summaries,
  task state. **Never store raw prompts/responses by default.**
- **No agents, no autonomous execution, no filesystem/project editing, no
  parallel model collaboration, no P10 concepts** (architecture §16).
- Single-candidate / single-conversation architecture; no SQLite on request
  paths; single-writer flusher.
- **No new migrations** (v7 landed in P9a) and **no new config keys**
  (`MAX_RESUME_REPLAYS`, `CONTINUITY_RETENTION_DAYS`, and the budget knobs
  exist but are not yet wired to recovery/retention behavior).
- `PROJECT_LOG.md` is updated **only at the final release commit (P9e)**.

---

## 1. Grounding in the landed code

### 1.1 Already durable (P9a)

- Schema v7; `conversation_turns.resume_token` stores a **hash, never raw**
  (`app/services/platform_store.py:221`).
- `ConversationStore.append_turn(...)` accepts `resume_token_hash`
  (`app/services/conversation_store.py:360`), writes it (`:390`, `:402`),
  and returns turns with `resume_token_hash` (`:434`, `:453`).
- `ConversationStore` read/mutator surface already present: `find` (`:187`),
  `list` (`:210`), `archive` (`:248`), `prune_retention` (`:276`),
  `turns` (`:420`), `summaries` (`:523`), `counts` (`:731`).
- `ContinuityFlusher` (`app/services/continuity_flusher.py`): write-behind
  buffer (`:76-93`), drain + prune per flush (`:101-126`), idempotent create
  on `IntegrityError` (`:151-156`), `flush_stats()` diagnostics (`:189-203`),
  startup `prune_now()` (`:182-187`).
- Retention prune (`conversation_store.py:276-341`): prunes conversations
  where `(status = archived OR last_turn_ts IS NOT NULL)` and
  `COALESCE(last_turn_ts, updated_at) < cutoff`, cascade-deleting turns,
  summaries, compaction records, then stale `project_state`; emits a
  `continuity.prune` audit row; `days <= 0` disables (`:287-288`).
- Lifespan (`app/main.py:40-42`, `:56-61`): flusher start + startup prune,
  stop + final flush; flusher is `None` unless `CONTINUITY_ENABLED`
  (`app/core/relay.py:104-127`).

### 1.2 Already coordinated (P9c)

- `HandoffCoordinator.commit_turn` enqueues `turn.append`
  (`app/services/handoff.py:475-487`) and `project_state.update`
  (`:488-498`); `continuity_turns_committed` counter (`:500`).
- **P9c does not yet pass `resume_token_hash`** in the `turn.append`
  enqueue — the store accepts it, so P9d only has to generate a token, hash
  it, and include it. The envelope has a `resume_token` field
  (`handoff.py:530-539` envelope) currently unused.
- Header wire contract: `continuity_headers.resolve_scope`
  (`app/services/continuity_headers.py:97`) is the flag + key-scope gate;
  `new_conversation_id` exists (`handoff.py:37` import).
- Metrics continuity block: `app/services/metrics.py:550-568` (enabled,
  rows_queued, flushes, pruned, flush_failures) plus P9c counters
  (`handoff.py:408,500,573,613`: switches, turns_committed, compactions,
  denials).
- Event vocabulary already registers `continuity.create/resume/switch/
  compact/archive/prune/denied` (`app/services/event_log.py:61-68`).
- Config: `MAX_RESUME_REPLAYS=3` defined in spec (`config_spec.py:574-575`)
  and validated (`app/core/config.py:841-843`) but **unused by any code**;
  `CONTINUITY_RETENTION_DAYS=30` (`config_spec.py:523-526`).

### 1.3 Gaps this plan closes

| Gap (audit §1.5) | P9d closes with |
| --- | --- |
| No recovery/replay protocol | `ContinuityRecovery` + resume-token issuance/validation |
| No torn-turn / in-flight handling | flusher in-flight registry + startup reconciliation |
| No retention tuning beyond idle-days | `prune_preview` + `--dry-run` + prune guard |
| No flusher/lag observability | `pruned_total` in `flush_stats`, CLI `health`, TUI panel |
| TUI has no continuity view | continuity section in diagnostics tab 7 |
| `resume_token_hash` never consumed | hash-verified resume + `continuity.resume/denied` |

---

## 2. Recovery protocol

### 2.1 `ContinuityRecovery` (`app/services/continuity_recovery.py`)

New service, wired lazily through the facade (`app/core/relay.py`) exactly
like `HandoffCoordinator` (only when `CONTINUITY_ENABLED`). No SQLite on the
request path; the store is reached only through the flusher's single-writer
thread or read-only store helpers (same rule as `find`/`list`).

Public API:

- `validate_resume(conversation_id, key_id, token) -> dict`
  - Scope check: conversation exists and `key_id` matches (same rule as
    `ConversationStore.archive`, `:248`). Mismatch, unknown id, or archived
    status → generic `resume_denied` (no oracle; S7).
  - Token check: constant-time SHA-256 comparison of the presented token
    against the **last committed turn's** `resume_token_hash` (new read
    helper `ConversationStore.last_turn`, §2.3). Non-match → denied.
  - Replay cap: per-process counter keyed by
    `(conversation_id, resume_token)`; `count >= MAX_RESUME_REPLAYS` →
    denied (rate-limited).
  - Outcome: `continuity.resume` event (`outcome=ok|denied`), replay count
    incremented, and on success the resume context = summary + tail built by
    `ContextManager` from committed turns (rebuild the envelope through
    `HandoffCoordinator`, reuse the `(conversation_id, up_to_seq)` dedupe).
- `issue_resume_token() -> str`
  - Opaque `uuid4` hex; the SHA-256 hash is enqueued with `turn.append`
    (`resume_token_hash=`); the raw token is returned to the caller only
    (response header / SSE). Raw token is never persisted, echoed, or
    logged (privacy contract).
- `reconcile() -> dict`
  - Startup, best-effort, after flusher start (§2.2). Verifies per
    conversation: last committed turn is the durable resume point; no
    `UNIQUE(conversation_id, seq)` violations; seq gaps are **reported**
    (diagnostics + `continuity.reconcile` event) but never auto-repaired.
  - Returns counts (`conversations_checked`, `seq_gaps`); failures are
    logged, never raised (continuity never breaks startup).

### 2.2 Torn-turn / in-flight detection

Design decision (grounded in architecture §8 S1/S2): **in-flight turns are
ephemeral by design** — durable torn state is deliberately not created.
Two mechanisms:

1. **In-memory in-flight registry** in `ContinuityFlusher`:
   - `mark_in_flight(conversation_id)` when a turn starts;
   - `clear_in_flight(conversation_id)` when the `turn.append` drains
     (commit) or the turn aborts;
   - `in_flight(conversation_id) -> bool` consulted by prune (§3) and by
     `ContinuityRecovery` for the `in_flight` status bit.
   - A process crash loses the registry (S2): on restart there is nothing to
     "undo" — the durable resume point is the last committed turn; the
     client is told to resend the last item.
2. **Startup reconciliation** (`reconcile()`): confirms the durable resume
   point and surfaces seq-gap/dup anomalies as diagnostics. No partial-turn
   reconstruction ever (S2).

### 2.3 Store read helpers (`app/services/conversation_store.py`)

Additive read-only methods, no schema change:

- `last_turn(conversation_id, key_id) -> Optional[dict]` — the newest
  committed turn (seq, outcome, ts, `resume_token_hash`). Mirrors `turns()`
  (`:420`) ordered DESC with `LIMIT 1`, key-scoped. Used by
  `validate_resume`.
- `prune_preview(days) -> list[dict]` — read-only dry-run of the prune
  candidate query (§3.1); shares the candidate query with
  `prune_retention` (refactor the WHERE at `conversation_store.py:296-303`
  into a private `_prune_candidates(days)` helper used by both).

### 2.4 Resume token lifecycle

| Phase | Where | Behavior |
| --- | --- | --- |
| Issue | `ContinuityRecovery.issue_resume_token` | opaque uuid4 hex; hash stored via `turn.append` enqueue (`handoff.py:475`) |
| Present | request header `X-Relay-Resume-Token` (with `X-Relay-Conversation-Id`, flag on) | validated in `continuity_headers` (bounds ≤ 128 bytes, printable ASCII; no echo) |
| Validate | `validate_resume` | constant-time hash compare vs last committed turn; key-scope; replay cap |
| Consume | next committed turn | new token replaces the old hash → old token stops matching (durable single-use-per-turn, **no migration**) |
| Deny | any mismatch / cap / scope / archived | generic `resume_denied`; `continuity.denied`; proceed as new conversation (S7) |

Replay-cap design note: durable single-use is enforced by token
**replacement on commit** (the stored hash only matches the newest turn), so
the per-process counter is a flapping guard, not the only control. This
respects the "no new migrations" constraint while satisfying the
architecture's §9 resume-replay cap.

### 2.5 Recovery state machine (per conversation)

| State | Meaning | Entry | Exit |
| --- | --- | --- | --- |
| `none` | no conversation row | — | create on first request with continuity |
| `in_flight` | a turn started, not committed (in-memory marker) | turn start | commit (`clear_in_flight`) or abort / crash (S2 → registry gone) |
| `committed` | last turn durably committed | turn commit / startup reconcile | next turn |
| `resuming` | reconnect with valid token, envelope rebuilt | `validate_resume` ok | commit → `committed`; failure → `in_flight` stays, client resends |
| `resume_denied` | invalid token / scope / cap / archived | `validate_resume` denied | treated as `none` (new conversation) |
| `archived` | conversation archived (`archive`, store `:248`) | archive | resume denied → new conversation |

Transitions never reconstruct a partial turn (S2); all `denied` paths are
generic (no oracle, architecture §11). The machine is driven by
`ContinuityRecovery` + `HandoffCoordinator`; the flusher only holds the
`in_flight` bit and performs the durable commit.

### 2.6 Duplicate-work prevention

- **Resume-from-last-commit**: a replay rebuilds context from the last
  committed turn (summary + tail, envelope dedupe by `up_to_seq`) — the
  client re-sends only the last item (S1).
- **Seq monotonicity**: `UNIQUE(conversation_id, seq)` + single-writer
  flusher make double-commit of the same turn impossible.
- **In-flight guard**: concurrent requests on one conversation cannot both
  advance the same conversation (marker + single-writer commit).
- **No partial reconstruction** (S2): a torn turn is never "finished" by
  recovery, so no duplicated or fabricated work.

---

## 3. Retention and cleanup

### 3.1 Prune improvements

- Refactor the candidate query in `prune_retention`
  (`conversation_store.py:276-341`) into `_prune_candidates(days)`.
- Add `prune_preview(days)` — same candidate set, read-only, returning ids +
  per-conversation row counts (turns/summaries/compactions) for the dry-run.
- Add `pruned_total` to `ContinuityFlusher.flush_stats()`
  (`continuity_flusher.py:189-203`) so the operator can see cumulative prune
  activity (the per-pass value already flows to
  `relay_metrics.continuity_pruned`, `:125`).

### 3.2 Dry-run support

- CLI: `relay conversations prune --dry-run [--days N] [--json]`
  (`app/cli/continuity.py:184` `_prune`) — prints the candidate set from
  `prune_preview` **without deleting**. JSON mode emits
  `{"candidates": [...], "days": N, "removed": 0}`. Respects
  `CONTINUITY_RETENTION_DAYS` default and `days <= 0` = disabled
  (`conversation_store.py:287-288`).

### 3.3 Recovery-safe deletion rules

- **Never prune in-flight**: `prune_retention` consults
  `ContinuityFlusher.in_flight(conversation_id)` and skips any conversation
  with an uncommitted turn (guard under the flusher lock; the prune call
  already runs on the flusher thread, `continuity_flusher.py:113-118`).
- **Active never pruned** (S8): unchanged rule — a conversation active within
  the window is never removed.
- **Transactional per conversation**: candidate deletions stay inside one
  `with conn:` block (`conversation_store.py:295-323`), so a prune never
  leaves an orphaned turn/summary/compaction.
- **`CONTINUITY_RETENTION_DAYS=0` disables** pruning entirely (existing
  rule).
- Audit: `continuity.prune` rows already record `removed` + `days`
  (`:332-339`); `--dry-run` never emits a prune event.

### 3.4 Retention diagnostics

- CLI `health` (§4.1) shows retention window, last flush, queued/drained/
  dropped counts, flush errors (from `flush_stats()`).
- TUI continuity panel (§4.2) shows the same, plus prune totals from
  `relay_metrics.continuity_pruned`.

---

## 4. Operational visibility

### 4.1 CLI surfaces (`app/cli/continuity.py`)

All additions remain **metadata-only** and flag-safe (print
`continuity disabled`, exit 0, `continuity.py:93-95`):

- `relay conversations show <id>` — extend the existing record render
  (`_show`, `:140-167`) with: last committed turn (`seq`, `outcome`, `ts`),
  **resume-token presence** (boolean; the hash is never rendered),
  `in_flight` indicator, replay count. JSON mode adds the same fields.
- `relay conversations health` — new subcommand: `flush_stats()` surface
  (running, interval, retention_days, last_flush_at, queued,
  queued_total, drained_total, dropped_total, flush_errors) + `counts()`
  summary. No values from continuity content.
- `relay conversations prune --dry-run [--days N] [--json]` — §3.2.
- Parser wiring: extend `add_continuity_parser`
  (`app/cli/continuity.py:22`); dispatch unchanged
  (`app/cli/__init__.py:245-248`).

### 4.2 TUI continuity/recovery panel (`app/ui/screens/diagnostics.py`)

Extend diagnostics tab 7 (`diagnostics.py:45-67` compose) with a read-only
**Continuity** section after the provider-health table:

- Counts: conversations (active/archived), turns, summaries, compactions,
  project-state rows — from `ConversationStore.counts()`
  (`conversation_store.py:731`).
- Flusher health: running, queued, drained_total, dropped_total,
  flush_errors (bounded), last_flush_at — from `flush_stats()`.
- Counters: switches / compactions / denials / resumes / reconciliations —
  from `relay_metrics` via `ServiceFacade`.
- Recent `continuity.*` events (create/resume/switch/compact/archive/prune/
  denied) filtered from the existing ops tail
  (`diagnostics.py:114` `ops_tail`).
- Data plumbing: `ServiceFacade` (`app/ui/data.py:262`) gains
  `continuity_stats()` and `flusher_health()` read helpers; the panel is
  rendered only when continuity is enabled (otherwise a single
  `continuity disabled` line).
- The existing redacted snapshot export (`export_diagnostics`,
  `app/ui/data.py:1100`) must pass `contains_never_captured()` with the new
  section (privacy negative test).

### 4.3 Status and troubleshooting information

- **Status**: conversation `status` (active/archived), last committed turn,
  resume-token presence, `in_flight` bit — all available via `show` and the
  TUI panel.
- **Troubleshooting**: flush errors + dropped totals signal write-behind
  loss risk; `continuity.denied` events in the ops tail surface client replay
  problems; `continuity.reconcile` at startup confirms recovery ran; seq-gap
  diagnostics flag divergence. All surfaces are metadata-only and never echo
  tokens, prompts, or responses.

---

## 5. Metrics, events, logging

- **Metrics** (`app/services/metrics.py:550-568` block): add
  `continuity_resumes` (ok), `continuity_resume_denials`,
  `continuity_reconciliations` counters (extend the P9c block).
- **Events**: **one additive vocabulary entry** — `continuity.reconcile`
  (`event_log.py:61-68`). `continuity.resume` (ok/denied) and
  `continuity.denied` already exist and gain emitters. No other vocabulary
  change.
- **Logging**: `RequestLogger` JSON records already carry `conversation_id`
  (`log_service.py:104-112`); P9d adds nothing to `request_log` (table and
  privacy contract untouched). Recovery events carry redacted detail only.

---

## 6. Files

### 6.1 New files

| File | Purpose |
| --- | --- |
| `app/services/continuity_recovery.py` | `ContinuityRecovery`: `validate_resume`, `issue_resume_token`, `reconcile`, replay cap, `continuity.resume/denied` events |
| `tests/test_continuity_recovery.py` | recovery unit/integration suites (§7 scenarios) |

### 6.2 Modified files

| File | Change |
| --- | --- |
| `app/services/conversation_store.py` | read helpers `last_turn`, `prune_preview`; refactor `_prune_candidates`; prune skips in-flight conversations |
| `app/services/continuity_flusher.py` | in-memory in-flight registry (`mark/clear/in_flight`); `pruned_total` in `flush_stats()`; prune guard consults registry |
| `app/services/handoff.py` | `commit_turn` enqueues `resume_token_hash` (token from recovery); expose last committed turn's token hash for validation |
| `app/services/continuity_headers.py` | `validate_resume_token` (bounds/charset, no echo) — same contract as `validate_conversation_id` |
| `app/core/relay.py` | lazy-wire `ContinuityRecovery` (flag-gated); recovery hooks in `chat()`/`achat()`; startup `reconcile()` |
| `app/main.py` | lifespan: `reconcile()` after flusher start (best-effort, flag-gated) |
| `app/api/chat.py` | optional `X-Relay-Resume-Token` request header (validate), `X-Relay-Resume-Token` response header on committed turns |
| `app/api/openai.py` | same for `/v1/chat/completions`; `relay:resume_token` SSE line on streaming resumes |
| `app/cli/continuity.py` | `show` resume fields, `health` subcommand, `prune --dry-run` |
| `app/ui/screens/diagnostics.py` | continuity panel (tab 7) |
| `app/ui/data.py` | `ServiceFacade.continuity_stats()`, `flusher_health()` |
| `app/services/metrics.py` | resume/deny/reconcile counters |
| `tests/test_continuity_http.py` | resume-token header validation, response-header echo, `relay:resume_token` |
| `tests/test_continuity_parity.py` | flag-off parity incl. resume header ignored, CLI/TUI surfaces inert |

### 6.3 Explicitly unchanged

- **Migrations**: none (v7 landed in P9a).
- **Config / spec**: no new keys; `MAX_RESUME_REPLAYS`,
  `CONTINUITY_RETENTION_DAYS` are wired, not added.
- `summarizer.py`, `summary_verifier.py`, `context_manager.py` internals
  (P9b) — called into, not modified.
- Provider clients, `client_registry`, `failure_classifier`,
  `chat_policy`, routing/decision layers.
- `request_log` table and privacy contract; `ops_store`; `event_log` (one
  additive action only); `memory_contract.py`.

---

## 7. Testing strategy

`tests/test_continuity_recovery.py` (new) plus extensions to
`test_continuity_http.py` and `test_continuity_parity.py`. Every mandated
scenario is a distinct suite:

| Mandated scenario | Suite | Verifies |
| --- | --- | --- |
| Crash during turn (S2) | `test_continuity_recovery.py` | in-flight marker present → simulated crash (new process/flusher) → `reconcile()` → resume point is the last committed turn; no duplicate turn row (`UNIQUE(conversation_id, seq)`); client is told to resend |
| Incomplete handoff (S3/S4) | `test_continuity_recovery.py` | provider fails mid-turn after envelope assembly, turn not committed → next request resumes from last committed turn; envelope rebuilt from summary + tail; no partial-turn reconstruction |
| Repeated resume attempts | `test_continuity_recovery.py` | valid token honored; replay counter reaches `MAX_RESUME_REPLAYS` → `continuity.denied`; after the next turn commits, the old token no longer matches (durable single-use); constant-time compare exercised |
| Corrupted state (S6) | `test_continuity_recovery.py` | corrupt/incomplete continuity rows → backup-aside-and-reopen; conversation starts fresh; `events` audit row; chat path unaffected; seq-gap reported not repaired |
| Retention interaction (S8) | `test_continuity_recovery.py` + store suite | active never pruned; archived older than window pruned; **dry-run returns exactly the candidate set prune removes**; in-flight conversation never pruned; `--days 0` disables; delete is transactional (no orphans) |
| Flag-off parity | `test_continuity_parity.py` | `CONTINUITY_ENABLED=false`: resume header ignored, no rows, no new SSE lines, no CLI/TUI surfaces, responses byte-identical; `relay conversations` prints `continuity disabled` |

Supporting suites in `test_continuity_recovery.py`:
- unit: token issue/validate (hash compare, bounds, scope), replay counter,
  state-machine transitions, `prune_preview` candidate parity with
  `prune_retention`;
- integration (mocked providers, facade): resume → envelope → commit →
  new token; reconnect with stale token → denied → new conversation;
  `relay:resume_token` on streaming resume;
- concurrency: parallel resumes on one conversation respect the in-flight
  marker and single-writer commit; no DB access on request paths (asserted).

**Regression gate** (P9c parity): full suite green (**2182 passed / 22
skipped** baseline + new suites), `python -m compileall -q app` clean; no
ruff/mypy gate exists in this repo — pytest + compileall is the gate.

---

## 8. Risks

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Resume replay burns tokens / thrash | cost, provider hammering | `MAX_RESUME_REPLAYS` cap + durable token replacement; `continuity.denied` events; rate-limited |
| Torn-turn double-commit | inconsistent history | in-memory marker + `UNIQUE(conversation_id, seq)` + single-writer flusher; reconciliation reports gaps |
| Resume-token leakage | impersonation / exposure | hash-only storage (already enforced, `platform_store.py:221`); raw token only in response; never echoed in logs/metrics/CLI |
| Unflushed tail lost on hard crash | context regression | S2 design (in-flight ephemeral); shutdown flush (P9a); drain-lag surfaced |
| Prune deletes an in-flight conversation | data loss | prune consults the in-flight registry under the flusher lock; dry-run preview; active never pruned |
| Corrupt continuity rows after crash | stale resume | backup-aside-and-reopen (S6); reconcile reports; chat path unaffected |
| Flag-off regression | behavior drift | parity suite gates every P9d commit; additive-only; default off |
| Scope creep toward P10 | boundary violation | recovery/retention/surfaces only (§1 constraints; architecture §16) |

---

## 9. Acceptance criteria (P9d DoD)

Verified after the gate (§7):

- **Recovery**: resume at a prior committed turn works with hash-verified
  resume tokens; torn-turn (crash mid-turn) and incomplete-handoff scenarios
  leave no duplicate or partial rows; state-machine transitions S1–S7 tested;
- **Replay cap**: `MAX_RESUME_REPLAYS` enforced; over-cap denied with
  `continuity.denied`; durable single-use via token replacement on commit;
- **Retention**: `--dry-run` matches actual prune; active and in-flight
  conversations never pruned; transactional deletion (no orphans);
- **Surfaces**: CLI `show` resume status, `health`, and `prune --dry-run`
  render metadata only; TUI continuity panel present when enabled, single
  `continuity disabled` line otherwise;
- **Diagnostics**: resume/deny/reconcile counters + `pruned_total` +
  `flush_stats` surfaced; `continuity.reconcile` startup event;
- **Privacy**: no raw prompts/responses/tokens/paths in any new surface;
  exports pass `contains_never_captured()`;
- **Parity + gate**: flag-off byte-identical parity green; full suite green
  (2182/22 baseline + new suites); `compileall` clean;
- `PROJECT_LOG.md` untouched (updated only at the P9e release commit).

---

**Stop condition**: this plan is delivered for approval. Implementation
does not start until approved. No code, no commits, `PROJECT_LOG.md`
untouched.
