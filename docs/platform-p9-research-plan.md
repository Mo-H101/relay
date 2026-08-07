# P9 — Project Continuity & Model Handoff Layer (Research & Architecture Audit)

Date: 2026-08-06.
Status: **Research and architecture audit only — no code, no commits, no
`PROJECT_LOG.md` changes.** Pending approval before any implementation.

Scope: P9 is **required before `v1.0.0`** (user decision, supersedes the
"reserved / must not be initiated during release preparation" boundary in
`roadmap-verification-audit.md` §P9). The P0–P8 audit stands; release
preparation stays gated on this phase. This document follows the standard
workflow: Audit → Plan → Approval → Implementation → Tests → Commit.

Design sources: study of OpenCode, OpenAI Codex, Cline, Continue.dev,
Aider, and SWE-agent (Section 4). Relay grounding: `docs/architecture.md`,
`docs/platform-db-schema.md`, `app/services/state_store.py`,
`app/services/memory_contract.py`, `app/services/platform_store.py`.

---

## 1. Current Relay capabilities relevant to P9

- **Layered architecture** (`docs/architecture.md`): `api routers → Relay
  facade (`app/core/relay.py`) → services → providers`. `app/core/config.py`
  is the dependency-graph root. No circular imports; no layer imports above
  itself. Any P9 surface must sit in the services layer or below and be
  wired through the facade.
- **Dual-path chat**: sync (`ChatService`, used by the TUI via
  `asyncio.to_thread`) and async (`AsyncChatService`, hot path for `/chat`
  and `/v1/chat/completions`). Provider contract: `achat`, `achat_stream`,
  `achat_messages`, `achat_stream_messages`, `alist_models`,
  `aprobe_model`. Failover walks an ordered candidate list
  (`chat_across` / `achat_across`) using the failure classifier.
- **Stateless request handling today**: every request is independent. There
  is **no conversation identity, no server-side turn state, and no resume**.
  Continuity across requests is limited to provider/model-level learning
  (health, telemetry, quality, decisions) — never conversation-level.
- **Durable surface** (`platform.db`, WAL, `busy_timeout 5000`, `0600`
  file+sidecar perms, in-process migration lock, corrupt-file
  backup-aside-and-reopen): `api_keys` (v1), `learned_state` /
  `telemetry` / `telemetry_failures` / `quality_aggregates` /
  `decision_stats` (v2/v3), `model_status` (v4), `events` audit log (v5),
  `request_log` (v6, metadata only). Migrations guarded by `PRAGMA
  user_version`; a newer-version file is refused; `relay migrate` supports
  backup + rollback.
- **Memory contract** (`app/services/memory_contract.py`): every state
  surface is classified durable / ephemeral / **never**. Never = prompts,
  responses, generated content, API keys, proxy credentials, user identity.
  `FORBIDDEN_KEYS` + `contains_never_captured()` back this with negative
  tests. **P9's design must reconcile with this contract — it is the central
  constraint.**
- **Client awareness**: `request_log` records a client bucket
  (`cline | opencode | continue | other`), trimmed User-Agent, opaque key
  id, and per-key correlation; `X-Relay-Correlation-Id` per request; an
  apps projection over `api_keys` × `request_log`.
- **Background machinery** (lifespan handler): `HealthRefresher`,
  `StateFlusher` (write-behind durable flush + final flush on shutdown),
  `request_log` bounded-buffer flush. Single-writer SQLite assumption is
  explicit (`state_store.py` docstring): no DB access from chat request
  paths, no concurrent writers.
- **Observability**: `events` audit log, `diagnostics`, `telemetry`,
  metrics registry, `event_log` — P9 continuity operations must flow into
  the same surfaces.

## 2. Why P9 is needed

1. **Mid-turn failure is a real, observed gap.** A client disconnect, a
   Relay restart, or an upstream failure mid-stream loses the turn. Clients
   re-send or the conversation restarts; Relay cannot resume because it
   holds no turn/conversation state.
2. **Model handoff is context-free today.** When failover picks a
   different provider/model, the fallback receives only the current request
   payload. It has no idea what came before, so multi-turn work degrades
   sharply after a provider outage.
3. **It is the product's core value proposition.** Relay is a local smart
   gateway in front of many providers. "Continue the conversation across
   models without losing the thread" is the reason a proxy like this exists;
   today only per-request routing survives.
4. **Required before `v1.0.0`** per user decision. Release preparation is
   gated on P9; the P0–P8 audit (`docs/roadmap-verification-audit.md`)
   remains valid.

## 3. Problems P9 must solve

1. **Mid-turn disconnect / restart recovery** — no server-side turn state to
   resume from.
2. **Context-free model/provider handoff** — a fallback provider/model
   cannot continue a conversation.
3. **No conversation identity model** — multi-client (Cline/OpenCode/
   Continue), multi-project, multi-key usage needs a durable, isolated
   conversation identity.
4. **Context-window management** — long sessions must compact without
   losing the ability to continue (bounded token budget, tail + summary).
5. **Memory-contract conflict (the central constraint)** — Relay is
   *forbidden* from persisting prompts, responses, or generated content.
   Any continuity layer that stores contexts or summaries must reconcile
   with this contract or amend it with explicit user consent.
6. **Single-writer SQLite** — continuity writes must stay off chat request
   paths and remain single-writer (existing `StateStore` rule).
7. **Streaming resume semantics** — SSE streams need a resume/disconnect
   protocol, not just request/response.
8. **Durability format and backward compatibility** — resumed sessions must
   survive schema upgrades and future versions.
9. **Multi-tenant privacy** — one Relay serves multiple app keys; key A must
   never resume key B's conversation.
10. **Auditability** — continuity lifecycle must land in the `events` log.
11. **Bounded resource use** — compaction cost, history size, retention, and
    token budgets must be capped.
12. **Zero behavior change when disabled** — a feature flag must make Relay
    behave exactly as today (protects the 2055/20 regression gate).

## 4. Research findings from existing systems

Research performed this session (web + source studies). Findings are
synthesized as *principles*, not copied code.

### OpenCode — checkpoint compaction
- Compaction replaces old context with a **checkpoint**: a structured
  summary plus a **serialized tail** of recent messages kept verbatim.
- Durable session messages are **never deleted**; compaction is lossy for
  *context* only, and the durable record remains complete.
- Preflight size estimate: JSON-serialize the conversation and estimate
  tokens (~**4 chars/token** heuristic).
- **Auto-compact before the model call** when the estimate exceeds the
  limit; on a **context-overflow error, compact and retry once**, even when
  auto-compact is disabled.
- Manual compact coalesces pending requests.
- Lesson for Relay: compaction = summary + verbatim tail; never destroy the
  durable record; retry-once on overflow; cheap preflight estimation.

### OpenAI Codex — thread lifecycle, rollout persistence, index
(Codex research retried successfully this session after an earlier 429.)
- **Threads**: create / resume / fork / archive / unarchive / delete.
  Persisted event history lets clients reconnect and render a consistent
  timeline; `sessionId` roots forked threads to their origin.
- **Rollout persistence**: append-only JSONL per session under
  `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`; cold files
  compressed with **Zstandard** (transparently materialized back on resume);
  a **background recorder** writes asynchronously via a command channel
  (`AddItems` batching / `Persist` ack / `Flush` / `Shutdown`).
- **SQLite index** (`state_5.sqlite`): extracted `ThreadMetadata` (title,
  model, token usage, git context, goals) + **`thread_spawn_edges`**
  parent/child lineage (forks used by sub-agents). Index is **backfilled**
  from the filesystem on init.
- **Compaction**: auto-compaction check **before sampling** when the token
  limit is reached; manual `thread/compact/start`; compaction summaries
  stored as `Compacted` items in the rollout; `contextCompaction` lifecycle
  events streamed to the UI. `thread/rollback` is deprecated (drop last N
  in-memory turns + persist a marker).
- **Resume restores** model/provider, cwd/environment, sandbox policy,
  dynamic tools, and token budget. Goals are persisted
  (`thread/goal/set/get/clear`).
- **WorldState** aggregates context sections, with **diff-rendering
  sections** to minimize redundant context injection.
- **History cap**: `history.jsonl` with `max_bytes` drops oldest entries and
  compacts while keeping the newest.
- Lesson for Relay: append-only per-conversation log + separate metadata
  index (mirrors Relay's `platform.db` split); async background writes;
  additive, never-destructive persistence; explicit resume payload
  (identity + model + environment + token budget).

### Cline — state architecture
- **Three-tier storage**: disk persistence / in-memory cache / **debounced
  async writes**; a `StateManager` singleton coordinates them.
- **Scoping**: global vs workspace-scoped vs per-task storage; per-task
  storage holds conversation history and checkpoints (resumable).
- Lesson for Relay: write-behind with debounce (already the Relay pattern in
  `StateFlusher`); explicit scopes map to Relay's key_id × project.

### Continue.dev — layered message-passing
- Architecture is layered (IDE extensions / Core orchestrator / GUI) with
  **typed message-passing protocols** between layers; pass-through message
  lists and a `configHandler.onConfigUpdate` pattern for dynamic config.
- Lesson for Relay: keep the continuity surface an internal, typed service
  contract behind the facade; expose small HTTP/SSE hooks to clients.

### Aider — repo map and token budget
- Concise **repo map** of classes/functions/types with call signatures;
  a **graph-ranking** algorithm (files = nodes, dependency edges) selects
  the most relevant subset within a `--map-tokens` budget (default ~1k);
  the map expands dynamically when few files are in chat.
- Lesson for Relay: budget-constrained, relevance-ranked context is
  tractable and testable; Relay's equivalent is a **budget-constrained
  conversation tail** rather than a repo map.

### SWE-agent — Agent-Computer Interface and history compression
- **ACI**: a small set of custom tools exposed inside an isolated shell
  session; environment managed by `SWEEnv` wrapping a SWE-ReX deployment
  (Docker local/remote).
- A **HistoryProcessor compresses history before prompting**; model output
  is parsed back into actions.
- Lesson for Relay: decouple the *environment/execution* layer from the
  *conversation* layer; compress history as an explicit preprocessing step
  (Relay: compression before handoff, never in the hot path).

### Synthesis — design principles for Relay P9

| Principle | Source |
| --- | --- |
| Compaction = structured summary + verbatim tail; never destroy the durable record | OpenCode, Codex |
| Preflight token estimate (~4 chars/token); compact before call; retry once on overflow | OpenCode |
| Append-only conversation log + separate metadata index; async background writes | Codex |
| Resume payload restores identity, model chain, token budget, project | Codex |
| Write-behind, debounced durable writes; scopes = key_id × project | Cline, Relay `StateFlusher` |
| Internal typed service contract behind the facade; small client hooks | Continue.dev |
| Budget-constrained, relevance-ranked context | Aider |
| History compression as an explicit pre-step, off the hot path | SWE-agent |
| History caps drop oldest, keep newest | Codex, OpenCode |
| Feature flag: disabled ⇒ byte-identical behavior to today | Relay regression gate |

## 5. Proposed Relay P9 architecture

Layers (respecting `docs/architecture.md`):

```
api routers → Relay facade (core.relay) → services → providers
                    │
        continuity services (new)      ── platform.db schema v7
```

- **Conversation identity**: `(key_id, client_bucket, project_key,
  conversation_id)`. `conversation_id` comes from an optional client header
  (`X-Relay-Conversation-Id`); `project_key` is derived one-way from an
  opaque `X-Relay-Project-Id` header, scoped to `key_id` (never a
  filesystem path); `key_id` is the opaque app key id. Conversation
  records are scoped and isolated by `key_id`.
- **New services** (in `app/services/`, wired once in the facade):
  - `ConversationStore` — durable create / append / archive / prune on
    `platform.db` schema v7 (see Section 11). Mirrors `StateStore`'s single
    guarded connection + WAL; **never touched from chat request paths**.
  - `ContextManager` — in-flight token estimation, compaction
    (summary + tail), tail serialization. Pure logic where possible;
    in-memory/ephemeral only (Section 7).
  - `HandoffCoordinator` — wraps the existing candidate walk; assembles the
    **context envelope** on model/provider switch and emits streaming
    resume/handoff events (Section 8).
  - `ContinuityRecovery` — turn resume after disconnect/restart, archive,
    retention, audit-event writing (Section 9).
- **API/CLI surface** (small, additive):
  - Optional request headers (`X-Relay-Conversation-Id`,
    `X-Relay-Project-Id`) honored only when the feature is enabled; both
    are opaque ids, never filesystem paths.
  - Additive SSE notifications (`relay:conversation`, `relay:model_switched`,
    `relay:resume_token`) — clients that ignore unknown events are
    unaffected.
  - Read-only CLI/TUI surfaces (`relay conversations`, diagnostics screen
    section) with metadata only.
- **Feature flag** (e.g. `CONTINUITY_ENABLED`): when off, all new paths are
  inert and behavior is byte-identical to today.
- **Audit integration**: every continuity lifecycle event (create, resume,
  compact, handoff, archive, prune) writes a bounded-vocabulary row to the
  `events` table.

Design invariants:
- No import of `app.core.config` below the facade construction path.
- No SQLite access from chat request paths (existing single-writer rule).
- No content persistence without explicit opt-in (Section 6).

## 6. Memory model

**Approved 2026-08-06: Option C.** Relay stores metadata, project state,
decisions, summaries, and task state. Raw prompts/responses are **not**
persisted by default. This is a memory-contract amendment: new durable
classes are added for continuity, while the `never` class is unchanged.

| Surface | Class | Content |
| --- | --- | --- |
| `conversations` | durable | id, key_id, client_bucket, project_key, status, model chain, token budgets, timestamps |
| `conversation_turns` | durable | per-turn provider/model, outcome, task, token counts, latency, resume token |
| `summaries` | durable | derived, versioned, redacted compaction summaries (never raw prompts/responses) |
| `compaction_records` | durable | method, reason, from→to tokens, summary reference |
| `project_state` | durable | per-project derived state (last models, counters) |
| in-flight context envelope | ephemeral | tail + token estimates for the current turn |
| prompts / responses / generated content | never | unchanged — forbidden everywhere |

Identity handling: `key_id` is opaque; **user identity is never stored**;
project identity is an opaque, key-scoped id (one-way derived key, never a
filesystem path). Client bucket stays one of
`cline | opencode | continue | other`.

## 7. Context compaction strategy

- **Tail model**: during a continuity turn, keep the last N messages
  verbatim **in memory (ephemeral)**; bound N by a token estimate using the
  OpenCode-style heuristic (JSON-serialize, ~4 chars/token) and a
  configurable budget (default: model context window minus reserve).
- **Preflight**: before each upstream call in a continuity session, estimate
  context size; **auto-compact when over budget**.
- **Compaction output**: structured summary + serialized tail. Sent to the
  next provider/model on handoff. The summary is **derived and persisted**
  (approved Option C); the raw tail and messages are never persisted.
- **Overflow recovery**: on a context-overflow error from upstream, compact
  and retry **once**; if still failing, degrade to no-context handoff
  (current request only) — never fail the request on compaction.
- **Durable record unaffected**: compaction is lossy for context only; the
  durable conversation record (metadata, lineage, outcomes) remains
  complete. Mirrors OpenCode/Codex ("never delete the durable log").
- **Bounded resource use**: per-conversation history cap (drop oldest, keep
  newest — Codex `history.max_bytes` pattern); compaction runs off the hot
  path; estimate cost is bounded and cheap.

## 8. Model handoff protocol

- **Context envelope**: when the failover/candidate walk selects a fallback
  provider/model mid-conversation, `HandoffCoordinator` assembles
  `{compacted summary, serialized tail, current request}` from the
  **ephemeral** in-flight context (Option A) and sends it to the fallback.
  The envelope is structurally identical to a normal payload, so existing
  provider clients are unchanged.
- **Durable handoff metadata**: after a switch, write
  `{from_model, to_model, from_provider, to_provider, ts, outcome}` to
  `conversation_turns` / `compaction_records` (metadata only) for
  diagnostics and audit.
- **Streaming handoff**: on a mid-stream switch, emit an additive SSE
  notification (`relay:model_switched` with new provider/model + resume
  token) and continue the stream from the new provider — the turn does not
  restart.
- **Relationship to existing failover**: P9 wraps the existing
  `chat_across` / `achat_across` candidate walk. Failover behavior,
  retry/backoff, and failure classification are unchanged; P9 only adds the
  context envelope and the continuity record.
- **Budget transparency**: token budget consumed so far is part of the
  envelope so the fallback model sees remaining headroom.

## 9. Failure recovery strategy

- **Turn resume**: client supplies `X-Relay-Conversation-Id` (+ optional
  resume token) on reconnect. `ContinuityRecovery` validates scope (key_id
  match), reads the durable record (last completed turn, model chain, token
  budget), and either replays the unfinished turn or returns a
  `relay:resume_token` prompting the client to resend the last item.
- **Mid-stream**: SSE resume-token protocol; server can signal "replay from
  turn k" or "resend last item" without rebuilding client state.
- **Crash consistency**: `platform.db` WAL + atomic single-row commits
  (existing `StateStore` pattern); write-behind flush with final flush on
  shutdown (mirror `StateFlusher`) so continuity metadata is not lost.
- **Corruption**: reuse the existing backup-aside-and-reopen policy;
  continuity tables are prunable and non-fatal to the rest of Relay.
- **Degradation ladder**: full context → compacted context → current
  request only. Compaction/handoff failures never escalate to a request
  failure.

## 10. Security / privacy considerations

- **Contract compliance** (approved Option C): `FORBIDDEN_KEYS` negative
  tests extended to every new surface. Metadata, project state, decisions,
  summaries, and task state are stored; raw prompts/responses are never
  persisted and summaries are always derived, versioned, and redacted.
- **Multi-tenant isolation**: conversations are scoped and bound to
  `key_id` server-side; key A cannot create/resume/read key B's
  conversation; conversation-id binding is validated on every continuity
  operation.
- **Audit**: continuity operations write bounded-vocabulary rows to the
  `events` table with outcome (`ok`/`failed`/`denied`) and redacted detail —
  consistent with the existing audit contract.
- **At-rest posture**: reuse existing platform.db protections — WAL mode,
  `0600` on the database and sidecars (POSIX), no raw key material, no
  plaintext provider credentials; continuity tables add no new secret
  surface.
- **Diagnostics/redaction**: continuity surfaces, metrics, and logs expose
  metadata only; `contains_never_captured()` negative tests must pass on
  conversation exports, event payloads, and log output.
- **Opt-out**: `CONTINUITY_ENABLED=false` (default false) leaves behavior
  byte-identical to today; no continuity headers are honored, no tables
  written, no resume endpoints active.
- **Consent/contract**: Option C (approved) means summaries and task/project
  state are durable by default; the `never` class (raw prompts/responses,
  keys, identity) is unchanged and enforced by negative tests.

## 11. Storage requirements

Extend `platform.db` with schema **v7** (proposal; additive, idempotent,
guarded by `PRAGMA user_version`, refusing newer-version files):

```
conversations (
  id TEXT PRIMARY KEY,            -- conversation_id (uuid)
  key_id TEXT NOT NULL,           -- opaque app key id (scope)
  client_bucket TEXT NOT NULL,    -- cline | opencode | continue | other
  project_key TEXT,               -- key-scoped opaque project id (never a path)
  status TEXT NOT NULL,           -- active | archived
  model_chain TEXT NOT NULL,      -- JSON array of model ids used
  token_budget INTEGER,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  last_turn_ts REAL
)

conversation_turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  seq INTEGER NOT NULL,
  provider TEXT, model TEXT,
  outcome TEXT NOT NULL,          -- ok | failed | denied
  task TEXT,                      -- routing task classification
  tokens_in INTEGER, tokens_out INTEGER,
  latency_ms INTEGER,
  resume_token TEXT,
  ts REAL NOT NULL
)

summaries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  up_to_seq INTEGER NOT NULL,     -- turns covered by this summary
  version INTEGER NOT NULL,       -- SUMMARY_VERSION
  method TEXT NOT NULL,           -- extractive | llm
  content TEXT NOT NULL,          -- derived, redacted summary (bounded)
  tokens_in INTEGER, tokens_out INTEGER,
  created_at REAL NOT NULL,
  UNIQUE (conversation_id, up_to_seq)
)

compaction_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  at REAL NOT NULL,
  reason TEXT NOT NULL,           -- preflight | overflow | manual
  method TEXT NOT NULL,           -- summary+tail | tail-only
  from_tokens INTEGER, to_tokens INTEGER,
  summary_id INTEGER REFERENCES summaries(id)
)

project_state (
  project_key TEXT NOT NULL,      -- key-scoped opaque project id
  key_id TEXT NOT NULL,
  last_models TEXT NOT NULL,      -- bounded JSON array
  counters TEXT NOT NULL,         -- bounded JSON counters
  last_seen REAL NOT NULL,
  PRIMARY KEY (project_key, key_id)
)
```

Constraints and rules:
- Indexes: `idx_conversations_key` on `(key_id, status)`,
  `idx_conversation_turns_cid` on `(conversation_id, seq)`,
  `idx_compaction_cid` on `(conversation_id)`.
- WAL mode, `busy_timeout 5000`, `0600` file + `-wal`/`-shm` sidecars,
  in-process migration lock, corrupt-file backup-aside-and-reopen — all
  inherited from `platform_store.py`.
- **Single-writer**: continuity writes occur only from background /
  continuity paths, never chat request paths (existing `StateStore` rule).
  One guarded connection with `threading.Lock`.
- **No new DB files**: reuse the single-file `platform.db` model.
- **Retention**: `conversations` archived-not-deleted by default; prune
  policy mirroring `request_log` (default 30 days, configurable); turn and
  compaction rows pruned with their conversation.
- **Bounded rows**: token counts are integers; `model_chain` JSON is
  bounded (cap chain length); resume tokens are opaque and short.

## 12. Migration risks

- **Schema v7** is additive; v6 → v7 must be idempotent and guarded by
  `user_version`. Existing state dirs and clients unaffected.
- **Single-writer violation** is the top risk: any accidental SQLite access
  from chat request paths could corrupt `platform.db`. Mitigated by the
  existing rule, explicit service boundaries, and concurrency tests.
- **Contract amendment**: the Option C amendment is approved; it touches
  `MEMORY_SURFACES`, `FORBIDDEN_KEYS` documentation, negative tests, and
  security docs, and must land together with the schema migration.
- **Streaming protocol compatibility**: new SSE notifications are additive;
  clients ignoring unknown events are unaffected (matches today's
  SSE contract).
- **Performance**: compaction and estimation run off the hot path; token
  estimation is bounded; no blocking DB writes in request handling.
- **Rollback**: schema migration is reversible through the existing
  backup-aside + `relay migrate --rollback` path; continuity tables are
  prunable and non-fatal if removed.
- **Feature-flag rollout** makes the whole phase zero-risk to the existing
  2055/20 gate: with the flag off, no code path changes.

## 13. Testing strategy

- **Unit**: memory-contract negative tests for all new surfaces
  (`contains_never_captured()` passes on conversation exports and event
  payloads); token-estimation and compaction math; tail serialization;
  envelope assembly; retention pruning.
- **Store**: schema v7 up/down, idempotent re-run, newer-version refusal,
  integrity after simulated crash; single-writer lock behavior.
- **Integration**: create → append → resume → compact → handoff through the
  Relay facade with mocked providers; failover with context envelope;
  mid-stream model switch emits `relay:model_switched`.
- **Streaming**: disconnect/reconnect with resume tokens; replay-unfinished-
  turn; SSE resume notification ordering.
- **Concurrency**: parallel resumes; key-scope isolation (key A cannot touch
  key B); no DB access from request-path tests.
- **Security**: isolation across key ids; audit rows present with correct
  outcome; redaction on logs/metrics/exports; negative tests on all new
  surfaces.
- **Regression gate**: full suite stays green (2055/20), RC suite 28 green,
  CI green; new continuity suites added; **flag-off parity test** asserting
  byte-identical behavior to today.

## 14. Implementation phases

Each phase follows Audit → Plan → Approval → Implementation → Tests →
Commit; the full gate runs at the end of each phase.

- **P9a — Foundation & schema**: approve this plan; add `platform.db`
  schema v7 migrations; `ConversationStore` (metadata-only) + feature flag
  + audit rows; CLI/TUI read surface skeleton. DoD: schema up/down tests,
  flag-off parity, `events` rows present.
- **P9b — Context manager**: token estimation, compaction
  (summary + tail), tail serialization, overflow retry-once; pure/off-hot-
  path logic. DoD: unit tests for estimates and compaction; no persistence.
- **P9c — Handoff coordinator**: wrap the candidate walk with the context
  envelope; streaming `relay:model_switched`; durable handoff metadata.
  DoD: failover-with-context integration tests; stream-switch tests.
- **P9d — Recovery & retention**: turn resume protocol (header + resume
  token), archive/prune, `relay conversations` CLI + TUI diagnostics
  section, docs (`architecture.md`, `platform-db-schema.md`, README). DoD:
  disconnect/reconnect tests; retention tests; docs updated.
- **P9e — Security review & gate**: memory-contract review (Option A/B
  decision), adversarial security pass, redaction sweep, full gate, RC
  suite, `PROJECT_LOG.md` updated only at the final commit (per workflow).

Open decisions **resolved 2026-08-06**: (1) memory model = Option C
(metadata, project state, decisions, summaries, task state stored; raw
prompts/responses never persisted), (2) `CONTINUITY_ENABLED` default
`false`, (3) `CONTINUITY_RETENTION_DAYS` default `30`, (4) headers =
`X-Relay-Conversation-Id` / `X-Relay-Project-Id` (opaque; no filesystem
paths in headers). The detailed design is
`docs/platform-p9-architecture-design.md`; implementation does not start
until that document is approved.
