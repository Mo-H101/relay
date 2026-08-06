# P9 — Implementation Plan (P9a): Project Continuity & Model Handoff Layer

Date: 2026-08-06.
Status: **Implementation plan — no code yet.** Approval required before any
implementation. `PROJECT_LOG.md` is not modified. No commits.

Prerequisites (all approved):
- `docs/platform-p9-research-plan.md` — research + approved decisions.
- `docs/platform-p9-architecture-design.md` — approved architecture (incl.
  §16 Non-Goals / P10 boundary).

Workflow for every phase: **Audit → Plan → Approval → Implementation →
Tests → Commit**; the full regression gate runs at each phase end.

Hard constraints (from approval):
- **Option C memory model**: store metadata, project state, decisions,
  summaries, task state. **Never store raw prompts/responses by default.**
- **Never store API keys or secrets.**
- **No filesystem paths exposed externally** (`X-Relay-Project-Id` is
  opaque; `project_key` is a key-scoped one-way hash).
- **Maintain single-candidate / single-conversation architecture.**
- **No P10 concepts**: no agent framework, no multi-agent execution, no
  parallel model execution, no autonomous file editing, no task delegation,
  no AI developer teams.

---

## 1. Exact implementation phases

Phases P9a–P9e (per research plan §14), refined to file-level scope.
Acceptance criteria per phase: Section 12.

### P9a — Foundation & schema
- **P9a.1 Config**: add continuity settings + spec entries (Section 4).
- **P9a.2 Schema**: `platform_store.py` v7 migration (additive DDL).
- **P9a.3 Memory contract**: amend `memory_contract.py` surfaces.
- **P9a.4 Services**: `ConversationStore` + `ContinuityFlusher`; audit
  events; facade wiring in `core.relay.py`; `app/main.py` lifespan
  start/stop + startup retention prune.
- **P9a.5 Parity**: flag-off behavior tests green (nothing new reachable).

### P9b — Context manager
- `ContextManager` (token estimation, budget split, tail serialization),
  `summarizer` (extractive default + optional `llm`), `summary_verifier`
  (structural invariants + redaction hard guard), overflow retry helper.
- Pure logic; no I/O; no persistence beyond `summaries` /
  `compaction_records` rows written by the store.

### P9c — Handoff coordinator
- `handoff.py` (context envelope, switch caps), integration into
  `async_chat_service` (`achat_across`, `achat_across_messages`,
  `achat_across_stream`, `achat_across_stream_messages`) and
  `chat_service` (sync `chat_across`), plus `app/api/chat.py` and
  `app/api/openai.py` header plumbing and SSE handoff events.

### P9d — Recovery, retention & surfaces
- `continuity_recovery.py` (turn resume, replay cap), resume/SSE event
  protocol, retention pruning, `relay conversations` CLI, TUI diagnostics
  section, docs (`architecture.md`, `platform-db-schema.md`, README,
  `.env.example`).

### P9e — Security & privacy review + full gate
- `security-best-practices` gate, adversarial security pass, redaction
  sweep, privacy negative tests, full suite (**2338/22**) + RC suite (28) +
  CI, `PROJECT_LOG.md` updated **only at the final release commit** (per
  workflow).

## 2. Files expected to change

### Modified files
| File | Change |
| --- | --- |
| `app/services/platform_store.py` | `SCHEMA_VERSION = 7`; `MIGRATIONS[7]` DDL (Section 4); docstring update |
| `app/services/memory_contract.py` | add durable surfaces: `conversation_store`, `continuity_flusher`, `conversations`, `conversation_turns`, `summaries`, `compaction_records`, `project_state`; `FORBIDDEN_KEYS` unchanged |
| `app/core/config.py` | continuity settings attributes + validation (Section 4 list) |
| `app/core/config_spec.py` | `_restart`/`_simple` spec entries for each `CONTINUITY_*` / `MAX_*` key |
| `app/core/relay.py` | wire `ConversationStore`, `ContinuityFlusher`, `ContextManager`, `HandoffCoordinator`, `ContinuityRecovery`; continuity hooks in `chat()`/`achat()`; echo conversation id |
| `app/api/chat.py` | read/validate headers, pass continuity context, response `X-Relay-Conversation-Id`, handoff-aware error mapping |
| `app/api/openai.py` | same for `/v1/chat/completions` (non-streaming + streaming); additive `relay:*` SSE lines in the stream generator |
| `app/main.py` | lifespan: start/stop `ContinuityFlusher`; startup retention prune; final flush on shutdown |
| `app/services/async_chat_service.py` | optional continuity context threading through `achat_across*`; handoff envelope before post-first attempts; switch accounting |
| `app/services/chat_service.py` | sync `chat_across` parity changes |
| `app/services/log_service.py` | `RequestLogger` JSON records carry `conversation_id` (metadata only); `request_log` table untouched |
| `app/services/event_log.py` | `EVENT_ACTIONS` additions (Section 6) |
| `app/cli/__init__.py` | dispatch `relay conversations` subcommand |
| `app/ui/screens/diagnostics.py` | read-only continuity diagnostics section |
| `.env.example` | document all new keys with defaults |
| `README.md` | continuity feature docs (enabled/disabled behavior) |
| `docs/platform-db-schema.md` | v7 schema + privacy contract additions |
| `docs/architecture.md` | continuity services in the layer diagram + request flow |

### New files
| File | Purpose |
| --- | --- |
| `app/services/conversation_store.py` | `ConversationStore`: durable create/append/archive/prune; single guarded connection; scoped queries |
| `app/services/continuity_flusher.py` | `ContinuityFlusher`: write-behind flusher (mirrors `StateFlusher`), final flush on shutdown |
| `app/services/context_manager.py` | `ContextManager`: estimate, budget split, tail serialization, compaction orchestration |
| `app/services/summarizer.py` | extractive summarizer + optional `llm` wrapper (`CONTINUITY_SUMMARIZER_MODEL`) with fallback |
| `app/services/summary_verifier.py` | structural invariant checks + redaction hard guard |
| `app/services/handoff.py` | `HandoffCoordinator`: envelope assembly, switch caps |
| `app/services/continuity_recovery.py` | `ContinuityRecovery`: turn resume, replay caps, degradation ladder |
| `app/services/continuity_headers.py` | header validation, `project_key` derivation, conversation id generation |
| `app/models/continuity.py` | envelope/summary/turn dataclasses (versioned) |
| `app/cli/continuity.py` | `relay conversations [list|show|archive|prune]` (metadata only) |
| `tests/test_continuity_store.py` | store + migration + retention tests |
| `tests/test_continuity_context.py` | estimation/budget/tail math |
| `tests/test_continuity_summary.py` | extractive + llm summarizer + fallback |
| `tests/test_continuity_verifier.py` | summary verifier accept/reject |
| `tests/test_continuity_handoff.py` | envelope + switch caps + failover-with-context |
| `tests/test_continuity_recovery.py` | S1–S9 recovery behaviors + replay caps |
| `tests/test_continuity_http.py` | header validation, response headers, SSE events |
| `tests/test_continuity_privacy.py` | privacy leak detection + isolation |
| `tests/test_continuity_parity.py` | flag-off byte-identical parity |
| `tests/test_continuity_stability.py` | long-running stability + concurrency |

## 3. New services / modules required

All new services live in `app/services/` (or `app/models/`,
`app/cli/`), wired once through the facade in `app/core/relay.py`. They
follow the existing patterns: guarded singleton stores, injectable for
tests, best-effort on hot paths, write-behind persistence.

- **`ConversationStore`** (durable): owns schema-v7 tables. API:
  `create(scope, bucket, project_key)`, `append_turn(...)`,
  `get(conversation_id, key_id)`, `list(key_id, ...)`, `archive(...)`,
  `prune_retention(days)`, `record_summary(...)`,
  `record_compaction(...)`, `update_project_state(...)`. Single guarded
  connection + `threading.Lock`; **no access from chat request paths**;
  every mutator validates the key binding. Audit events emitted via
  `event_log()` (best-effort).
- **`ContinuityFlusher`** (background): mirrors `StateFlusher` — one
  daemon thread, periodic flush of queued continuity rows to
  `ConversationStore`, final flush on shutdown, consecutive-failure
  tracking. SQLite writes only on this thread.
- **`ContextManager`** (pure logic): `estimate_tokens(text)`,
  `compact(turns, budget, params) -> (summary, tail, stats)`,
  `serialize_tail(...)`. No I/O; fully unit-testable. Applied on the hot
  path only for *estimation* (cheap); compaction runs off the hot path.
- **`summarizer`** (module): `extractive_summarize(turns, budget)` —
  deterministic, structured (goal/context, decisions, outcomes, unresolved
  items, versioned); `llm_summarize(...)` — optional, called only when
  `CONTINUITY_SUMMARIZER_MODEL` is set and a provider can serve it;
  falls back to extractive on any failure. Never emits raw messages.
- **`summary_verifier`** (module): `verify(summary, store_ctx) -> bool` —
  structural invariants (references exist, `up_to_seq` monotonic, bounds)
  plus `not contains_never_captured(summary)` (hard guard). Persistence
  refuses unverified summaries.
- **`HandoffCoordinator`**: `envelope(conversation, tail, summary,
  budget_remaining, model_chain, resume_token)`; `on_switch(...)` applies
  switch caps (`MAX_SWITCHES_PER_TURN`, `MAX_SWITCHES_PER_WINDOW`) and
  returns the degradation action; records handoff metadata.
- **`ContinuityRecovery`**: resume protocol (validate token + key binding,
  replay cap), SSE resume-token issuance, S1–S9 behaviors, degradation
  ladder (full → compacted → current-request-only).
- **`continuity_headers`** (module): `validate_conversation_id(value)`,
  `validate_project_id(value)`, `derive_project_key(key_id, project_id)`
  (`sha256(key_id || ":" || project_id)[:32]`), `new_conversation_id()`
  (uuid4 hex). Bounds + printable-ASCII validation; values never echoed in
  errors/logs/metrics.

**Boundary enforcement**: none of these introduce an agent, a parallel
execution primitive, an inter-agent channel, or any filesystem access.
`project_key` is derived from opaque header input only.

## 4. Database migration plan (v6 → v7)

Mechanics (in `app/services/platform_store.py`):
- `SCHEMA_VERSION = 6 → 7`.
- Add `MIGRATIONS[7]` = list of `CREATE TABLE IF NOT EXISTS` / `CREATE
  INDEX IF NOT EXISTS` statements from architecture design §10
  (`conversations`, `conversation_turns`, `summaries`,
  `compaction_records`, `project_state` + 5 indexes). All statements run
  inside the existing migration transaction; guarded by `PRAGMA
  user_version`; idempotent on re-run; newer-version files refused.
- **Additive only**: no `ALTER`/`DROP` on existing tables; v6 rows and
  semantics untouched. Existing `relay migrate` backup/rollback copies
  `platform.db` whole, so continuity tables ride along automatically.
- **Flag-off behavior**: migration DDL runs (tables exist) but no rows are
  ever written when `CONTINUITY_ENABLED=false`; all continuity services
  are inert.
- Migration test matrix (`tests/test_continuity_store.py`):
  - open a real v6 fixture (or migrate to v6 then reopen) → version 7;
  - re-open an already-migrated v7 file → no-op, version stays 7;
  - file declaring version 8 → `PlatformStoreError` (upgrade error);
  - `PRAGMA integrity_check = ok` after migration; per-table row counts
    preserved for pre-existing tables;
  - corrupt-file backup-aside-and-reopen still works with continuity
    tables present.

## 5. Rollback strategy

- **Instant behavior rollback**: `CONTINUITY_ENABLED=false` — no code
  change, no data migration; continuity paths are inert (parity test).
- **Pre-release schema rollback**: existing `relay migrate --rollback`
  restores the backup (which includes `platform.db` whole) and removes
  continuity tables; unchanged code path.
- **Post-release downgrade**: additive downgrade migration v7 → v6 drops
  the five continuity tables; core tables (`api_keys`, state tables,
  `model_status`, `events`, `request_log`) preserved. If the Option C
  contract must be withdrawn, drop `summaries` and
  `compaction_records.content` first, retain metadata-only tables or
  remove all continuity tables; negative tests re-assert the `never` class.
- **Deploy rollback**: package revert is safe; continuity tables are
  prunable and non-fatal; a version mismatch never breaks the chat path.
- **Protocol compatibility**: older Relay versions ignore
  `X-Relay-Conversation-Id` / `X-Relay-Project-Id` and unknown SSE
  `relay:*` events; newer versions treat missing continuity state as a
  fresh conversation.

## 6. Security review

Gate: `security-best-practices` review **before** P9a implementation and a
closing adversarial pass in P9e (repo workflow).

- **Header hardening**: length ≤ 128 bytes, printable ASCII excluding
  control chars; invalid → `400` with generic body; values never echoed in
  errors, logs, or metrics; no path semantics (`project_key` is a
  key-scoped one-way hash).
- **Scope binding**: every `ConversationStore`/recovery operation
  re-validates `key_id == authenticated key_id` (source:
  `request.scope["relay_key_id"]`, same pattern as `event_log` actors);
  unknown/mismatched ids proceed as a new conversation — no oracle.
- **Replay protection**: resume tokens single-use-per-turn, validated
  against stored hashes (constant-time), replay cap `MAX_RESUME_REPLAYS`.
- **Switch caps**: `MAX_SWITCHES_PER_TURN` / `MAX_SWITCHES_PER_WINDOW`
  stop failover thrash; exhaustion records an audit row with outcome.
- **No new secrets**: provider keys stay in the keyring; `api_keys`
  unchanged; continuity tables hold no key material (privacy tests).
- **Rate limiting**: reuse existing per-key rate-limit seams for
  conversation create/resume and summarizer calls.
- **Audit** — add to `EVENT_ACTIONS`:
  `continuity.create`, `continuity.resume`, `continuity.switch`,
  `continuity.compact`, `continuity.archive`, `continuity.prune`,
  `continuity.denied`. Emitted via existing `event_log().emit(...)`
  (best-effort on hot paths, `raise_on_error` on admin paths), `detail`
  through `redact_dict`.
- **Threat model**: per architecture §11 (enumeration, spoofing, replay,
  content leakage via summaries, DoS, cross-key access) — each mapped to a
  test in `tests/test_continuity_security_*` / `test_continuity_privacy.py`.

**Tool-comparison conclusions (P9e §3.7):** the adversarial study of the
six tools (OpenCode, Cline, Continue, Codex, Aider, SWE-agent) shows every
reported failure mode is structurally answered without copying any tool's
design: read-only reconcile + non-persisted denials (OpenCode bricking),
structured rows + self-healing open (Cline corruption), reject-not-truncate
verification (Continue over-budget compaction), startup re-derivation with
no persisted in-progress flag (Codex wedging), detect-only reconcile plus
WAL/UNIQUE/flusher safety net (Aider), and fail-closed anomaly detection
(SWE-agent replay loops). P9e pins each answer with adversarial + fuzz +
restart tests rather than new production logic.

## 7. Privacy review

- **Memory contract**: amend `MEMORY_SURFACES` with the five durable
  continuity surfaces + `continuity_flusher`; `FORBIDDEN_KEYS` and
  `contains_never_captured()` unchanged and reused as the hard guard.
- **Never stored**: raw prompts, raw responses, verbatim generated
  content, API keys, proxy credentials, user identity, filesystem paths,
  and correlation ids (privacy contract unchanged).
- **Summaries**: derived only; versioned (`SUMMARY_VERSION`); provenance
  (method, model, token counts) recorded; redaction guard at write time;
  never presented as verbatim (SSE `relay:compacted` carries provenance).
- **request_log**: table and privacy contract unchanged; conversation id
  appears only in `RequestLogger` JSON log records (metadata).
- **Retention**: `CONTINUITY_RETENTION_DAYS` (default 30) prunes
  archived/inactive data; active conversations never pruned; deletion is
  key-scoped.
- **Transparency**: `relay conversations` and TUI surfaces expose
  metadata only; exports must pass `contains_never_captured()` (test).

## 8. Testing strategy

New suites (Section 2 list) wired into `[tool.pytest]`. Coverage includes
every mandated behavior:

| Required behavior | Test |
| --- | --- |
| Continuity disabled behavior | `test_continuity_parity.py` — flag off: headers ignored, no rows written, byte-identical responses to baseline suite |
| Enabled behavior | `test_continuity_http.py`, `test_continuity_store.py` — create/append/resume round-trip, response header echo |
| Provider switch continuity | `test_continuity_handoff.py` — failover with envelope, mid-stream `relay:model_switched`, switch caps |
| Context overflow | `test_continuity_context.py` — overflow → compact + retry-once → current-request-only; request never fails on compaction |
| Compaction correctness | `test_continuity_context.py` — budget split, summary+tail assembly, dedupe by `(conversation_id, up_to_seq)` |
| Summary verification failure | `test_continuity_verifier.py` — structural rejects + `contains_never_captured` reject; persistence refuses |
| Rollback/recovery | `test_continuity_recovery.py` — S1–S9, replay caps, `relay migrate --rollback`, downgrade migration |
| Privacy leak detection | `test_continuity_privacy.py` — exports/events/logs pass `contains_never_captured()`; no raw prompts/responses/keys/paths anywhere |
| Long-running stability | `test_continuity_stability.py` — soak loop (compaction + handoff + resume + prune), leak checks (memory bounded, rows pruned), concurrency (parallel resumes, single-writer lock, no DB on request path) |

Regression gate: full suite green (**2055/20**), RC suite green (**28**),
CI green; flag-off parity test is part of the gate. `security-best-practices`
gate before P9a and closing adversarial pass in P9e.

## 9. Performance considerations

- **No SQLite on the hot path**: chat request paths queue continuity rows
  in memory; `ContinuityFlusher` writes periodically (default 5 s) with a
  final flush on shutdown. Matches `request_log` / `StateFlusher`.
- **Cheap preflight**: `estimate_tokens` = JSON-serialize + char math
  (`// CHARS_PER_TOKEN`). Budget the added latency on `/chat` /
  `/v1/chat/completions`; target < 1 ms when no compaction needed (assert
  in stability suite).
- **Off-hot-path compaction**: compaction + optional summarizer run off
  the request thread; extractive is O(n) over bounded tail; `llm`
  summarizer is a single additional provider call only when configured.
- **Bounded memory**: tail capped (`CONTINUITY_TAIL_MAX_ITEMS`), summary
  capped (`CONTINUITY_SUMMARY_MAX_CHARS`), model chain capped; envelopes
  are bounded by compaction budgets.
- **Bounded latency**: switch caps and replay caps bound worst-case
  failover; single retry-once on overflow.
- **Concurrency**: WAL + `busy_timeout 5000`; single-writer rule preserved;
  per-store `threading.Lock`; no hot-path DB access (asserted).
- **Metrics**: continuity counters/gauges via `relay_metrics`
  (rows queued, flushes, compactions, switches, replay denials, failed
  summarizations); overhead budget measured in `test_continuity_stability.py`.

## 10. Failure scenarios and recovery handling

Implementation-level mapping of architecture §8 (S1–S9) + loop prevention:

| # | Scenario | Code path / handling |
| --- | --- | --- |
| S1 | Client disconnect mid-stream | stream generator marks turn interrupted (in-memory); on reconnect (conversation id + token) `ContinuityRecovery` replies `relay:resume_token`; client resends last item; partial token counts recorded |
| S2 | Relay restart mid-turn | durable record holds last completed turn; in-flight turn lost (ephemeral by design); resume from last completed turn; client told to resend |
| S3 | Provider fails mid-conversation | existing classifier/failover; `HandoffCoordinator` builds envelope; next candidate; `relay:model_switched`; caps enforced |
| S4 | Compaction fails / over budget after retry | degrade to current-request-only; record `compaction_records`; never fail the request |
| S5 | Summarizer (`llm`) unavailable | fall back to extractive; provenance records it; no hot-path dependency |
| S6 | Corrupt `platform.db` continuity data | inherited backup-aside-and-reopen; conversation starts fresh; `events` row written; request path unaffected |
| S7 | Scope mismatch / unknown id | deny resume (generic outcome), proceed as new conversation; audit `continuity.denied` |
| S8 | Retention prunes active conversation | prune only archived/inactive older than window; active never pruned |
| S9 | Invalid header values | `400` generic; no state created; nothing echoed |

Degradation ladder (uniform): **full context → compacted context →
current-request-only**. Every step logs/audits and preserves the existing
error contract (correlation id; no prompts/responses in bodies).

## 11. Integration with existing subsystems

- **request_log**: table, privacy contract, and `apps_projection` unchanged.
  Continuity adds `conversation_id` to `RequestLogger` JSON records
  (metadata only); per-request outcome recording stays as-is.
- **events**: reuse `event_log()` singleton + `EVENT_ACTIONS` additions
  (Section 6); continuity lifecycle is fully auditable; `relay events`
  CLI reads them unchanged.
- **auth / key scopes**: `key_id` from `request.scope["relay_key_id"]`
  (same source as event-log actors); continuity operations require the
  same auth as chat; revoked keys denied; bootstrap key (`"bootstrap"`)
  and unauthenticated traffic simply get no continuity (headers ignored
  unless a key is present and the flag is on).
- **provider routing**: routing/decision engine, `candidate_builder`,
  `provider_manager`, and `failure_classifier` unchanged. `HandoffCoordinator`
  wraps the existing `chat_across` / `achat_across*` candidate walk and
  adds the context envelope for post-first attempts; the decision engine's
  selection semantics are untouched.
- **CLI**: new `relay conversations [list|show|archive|prune]`
  (`app/cli/continuity.py`, dispatched from `app/cli/__init__.py`).
  Metadata only; respects key scope; reuses `relay` facade read APIs.
  No personality/mascot code (P9 seams preserved).
- **TUI**: read-only continuity section in `app/ui/screens/diagnostics.py`
  (counts, recent conversations, compaction/switch stats); reuses existing
  data patterns (`app/ui/data.py`); no agent or parallel concepts.

## 12. Acceptance criteria for each implementation phase

**P9a (Foundation & schema)** — DoD:
- Schema v7 migrates v6 → v7 cleanly (all v7 tests green); v8 refused;
  integrity ok; additive-only confirmed.
- `CONTINUITY_ENABLED=false` ⇒ all continuity paths inert; **parity suite
  green** (2055/20 baseline unchanged); `relay conversations` shows
  "continuity disabled".
- `ConversationStore` + `ContinuityFlusher` unit tests green; audit rows
  `continuity.create`/`continuity.prune` present with redacted detail.
- Config keys validated with documented defaults; `.env.example` updated.

**P9b (Context manager)** — DoD:
- Estimation/budget/split/tail unit tests green (incl. overflow math);
- extractive summarizer + fallback tests green; `llm` path tested with a
  mock provider; verifier accept/reject + redaction hard-guard tests green;
- no I/O on the hot path; compaction never fails a request.

**P9c (Handoff coordinator)** — DoD:
- Envelope assembly + switch caps unit tests green;
- async + sync failover-with-context integration tests green (non-stream
  and stream, messages and string payloads);
- `relay:model_switched` emitted on switch; response header
  `X-Relay-Conversation-Id` present; mid-stream switch does not restart the
  turn.

**P9d (Recovery, retention & surfaces)** — DoD:
- S1–S9 recovery tests green; replay cap enforced;
- retention prunes archived/inactive after `CONTINUITY_RETENTION_DAYS`,
  active never pruned;
- `relay conversations` and TUI diagnostics render metadata only;
- docs updated (`architecture.md`, `platform-db-schema.md`, README,
  `.env.example`).

**P9e (Security & privacy review + gate)** — DoD:
- `security-best-practices` gate green; adversarial pass closed;
- privacy negative tests green (exports/events/logs pass
  `contains_never_captured()`; no keys, prompts, responses, or paths);
- full suite **2338/22** + RC suite 28 + CI green; stability suite green
  with overhead budget met;
- `PROJECT_LOG.md` updated **only at the final release commit**.

---

**Stop condition**: this plan is delivered for approval. Implementation
does not start until approved. No code, no commits, `PROJECT_LOG.md`
untouched.
