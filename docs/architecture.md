# Relay Architecture

## Layers

Relay is a layered application with a strict dependency direction. Each
layer may only depend on the layers beneath it; no layer may import a
layer above it.

```
api routers  ->  Relay facade  ->  services  ->  providers (clients)
     \            (core.relay)            \          (providers/*)
      \             |                     \          |
       \            | core.config (root)  \         httpx
        \           |                      \         |
         \-- app.main (FastAPI, lifespan,   \-- schemas/models
             middleware, global auth)          (pydantic)
```

- `app/core/config.py` is the root of the dependency graph. It loads the
  `.env`, validates every value, and exposes the process-wide `settings`
  singleton. Nothing imports it from below except `app/main.py` and
  `app/core/relay.py`'s construction path.
- `app/core/relay.py` is the application facade. `Relay` owns every
  service (provider manager, health store/checker/refresher, telemetry,
  quality store, candidate builder, decision engine, chat service, state
  store/flusher) and wires them together once at construction.
- `app/api/*` routers are thin HTTP adapters. They parse requests, call
  the facade, and map results to responses. Error bodies carry a
  correlation id but never prompts, responses, or provider internals.
- `app/services/*` (29 services) hold all business logic: routing,
  scoring, health, telemetry, quality, decisions, persistence, reload,
  ops, metrics, failure classification. The P9 project-continuity services
  live here too and sit under the facade: `ConversationStore`,
  `ContinuityFlusher`, `ContextManager`, `HandoffCoordinator`,
  `ContinuityRecovery`, and the summary/verifier pair
  (`summarizer.py`, `summary_verifier.py`).
- `app/providers/*` model backends. `Provider` is a plain data holder;
  clients (NVIDIA, OpenAI, LM Studio) share `OpenAICompatibleClient`
  which speaks the OpenAI REST protocol over `httpx`. Exceptions are
  classified (`app/services/failure_classifier.py`) into
  `FailureKind` values that drive retry and failover.

There are no circular imports. Importing `app.core.config` is cheap;
importing `app.core.relay` builds the singleton `relay` (which only does
network I/O when a provider is enabled).

## Async Architecture (P3)

Relay provides a **dual-path** architecture for chat requests:

### Sync Path (Legacy/Fallback)
- `Relay.chat()` → `ChatService` → sync provider clients (`chat`, `chat_stream`, `chat_messages`, `chat_stream_messages`)
- Used by the terminal interface (`relay` TUI) via `asyncio.to_thread`
- Kept as a fallback; no sync behavior was removed

### Async Path (Primary API Hot Path)
- `Relay.achat()` → `AsyncChatService` → async provider clients (`achat`, `achat_stream`, `achat_messages`, `achat_stream_messages`)
- Used by `/chat` and `/v1/chat/completions` FastAPI endpoints (now `async def`)
- Non-blocking I/O via `httpx.AsyncClient` throughout the provider layer
- Cancellation-safe: `asyncio.CancelledError` propagates cleanly through all layers

### Shared Policy Layer
Both paths share pure decision helpers in `app/services/chat_policy.py`:
- `budget_exhausted`, `retry_wait_seconds`, `classify`, `fallback_reason`
- `RETRYABLE` / `PROVIDER_LEVEL` failure kind constants
- No I/O, no state, trivially testable in isolation

### Provider Contract
All provider clients (OpenAICompatibleClient, AnthropicClient, GeminiClient, OllamaClient) implement:
- `achat(provider, model, message, **gen_kwargs) -> str`
- `achat_stream(...) -> AsyncIterator[str]`
- `achat_messages(provider, payload) -> dict`
- `achat_stream_messages(provider, payload) -> AsyncIterator[dict]`
- `alist_models(provider) -> List[str]`
- `aprobe_model(provider, model) -> ModelProbe`

Error mapping, timeout handling, metrics recording, and retry-after logic mirror the sync implementations exactly.

## Request Flow

`POST /chat` (or `/v1/chat/completions`):

1. `app/main.py` runs the global `require_api_key` dependency (no-op when
   `RELAY_API_KEY` is unset) and the `MetricsMiddleware`, which records
   the HTTP call into the ops store.
2. The router resolves a routing task (explicit `task` field, or free-text
   classification when `TASK_CLASSIFICATION_ENABLED`).
3. `Relay.achat()` (async) or `Relay.chat()` (sync):
   - `provider_manager.ranked()` returns providers ordered by priority.
   - `candidate_builder.build()` produces an ordered candidate list
     `(provider, model)` using routing preference, health state, telemetry,
     and quality signals (see [routing-decisions.md](routing-decisions.md)).
   - If `DECISION_ENGINE_ENABLED`, the decision engine scores the same
     candidates and records decision statistics.
   - `async_chat_service.achat_across()` or `chat_service.chat_across()`
     walks the candidates, retrying and failing over per the failure
     classifier, until one succeeds.
   - Outcomes are recorded: request logger, telemetry (when enabled),
     health feedback (when enabled).
4. The router records metrics + ops, sets the `X-Relay-Correlation-Id`
   header, and returns the response (or a `502`/`503` error with the same
   correlation header).

Streaming (`stream: true`) opens the winning candidate's stream, emits
SSE chunks, and records telemetry/health once the stream finishes.

## Concurrency and lifecycle

- The FastAPI app runs the server's threadpool/event loop; providers are
  called with synchronous `httpx` calls from worker threads.
- Shared stores are internally locked: `HealthStore`, `TelemetryStore`,
  `QualityStore`, `DecisionEngine`, `ops_store`, and the metrics registry
  guard their state with `threading.Lock`. Concurrent reads and writes
  are covered by tests (`tests/test_concurrency.py`,
  `tests/test_hardening.py`, and per-store concurrency tests).
- Background work is driven by the lifespan handler in `app/main.py`:
  - `HealthRefresher` periodically re-probes providers when
    `HEALTH_REFRESH_ENABLED`.
  - `StateFlusher` write-behind flushes learned state to SQLite when
    `PERSISTENCE_ENABLED`, and performs a final flush on shutdown so no
    learned intelligence is lost.
  - `ContinuityFlusher` (when `CONTINUITY_ENABLED`) write-behind flushes
    conversation metadata/turns/summaries to the same SQLite file on its
    own thread and prunes by retention.
- The ops window and metrics are in-memory only and never touch SQLite.

## Persistence

Learned intelligence (health state, telemetry EWMA aggregates, quality
aggregates, decision statistics) is persisted to a single SQLite database
when `PERSISTENCE_ENABLED=true`. It is write-behind (flushed on an
interval and on shutdown) and versioned with additive migrations. The
database never contains prompts, responses, API keys, proxy credentials,
or correlation ids; `app/services/memory_contract.py` encodes these
rules and tests enforce them. A corrupted database is backed up and
persistence is disabled gracefully rather than failing startup.

The project-continuity tables (schema v7/v8) share the same SQLite file
and are written **only when `CONTINUITY_ENABLED=true`**: `ConversationStore`
owns the schema (conversations, turns, summaries, compactions,
`project_state`, and the v8 `resume_replays` replay tracker), the
`ContinuityFlusher` thread is the only writer, and rows are metadata and
derived state only — raw prompts, raw responses, and generated content are
never stored. See [platform-db-schema.md](platform-db-schema.md).

## Project continuity (P9)

Continuity gives opt-in clients ("no progress lost = committed turns"): a
conversation that is interrupted by a provider switch or a Relay restart
can be resumed without re-executing acknowledged work.

- **Gate:** the whole layer is inert when `CONTINUITY_ENABLED=false`
  (default). Enabling it is additive — clients opt a conversation in by
  sending `X-Relay-Conversation-Id` / `X-Relay-Project-Id`.
- **Two provider-facing flows** on the chat hot path:
  1. **Envelope injection** — before the request is sent, the
     `HandoffCoordinator` hydrates a context envelope (bounded recent turns
     plus a derived summary when the budget requires it) from
     `ConversationStore` and injects it into the provider payload
     (`handoff.py:167-188`). This is the live path; preflight compaction is
     done here, so the request never fails on context overflow.
  2. **Optional LLM summarizer** — when `CONTINUITY_SUMMARIZER_MODEL` is
     set, `summarizer.py:191-209` derives a redacted summary of compacted
     turns; otherwise summaries are extractive and local. The summarizer
     calls a provider through the same relay surface and any failure
     degrades to the extractive path.
- **Crash recovery:** `ContinuityRecovery` + `validate_resume` turn a
  crash-window interruption into a resumable state: the server replays the
  durable per-conversation `seq` and rejects any token whose replay cap is
  exhausted. Resume tokens are stored one-way hashed; replay attempts are
  tracked in `resume_replays` (v8) so the cap survives restarts, and the
  path fails closed if that table cannot be persisted.
- **Switch caps:** `MAX_SWITCHES_PER_TURN` / `MAX_SWITCHES_PER_WINDOW`
  stop a provider-switch storm; exhaustion records `continuity.denied` and
  emits `relay:model_switched` SSE for operators.
- **SQLite boundary:** continuity writes happen only on the
  `ContinuityFlusher` thread; hot-path reads are bounded (single-row
  `last_turn` + `resume_envelope` hydration), so the single-writer process
  model is unchanged.

## Metrics and observability

- `GET /metrics` — Prometheus text exposition (counters, gauges,
  histograms). No prompts, responses, or secrets.
- `GET /diagnostics` — a read-only operational snapshot: provider states,
  learned health, telemetry summaries, scoring/ranking details, decision
  statistics, and persistence status. Like metrics, it never exposes
  prompts, responses, or secrets.
- The ops store keeps a bounded rolling window of request metadata
  (timestamps, routes, statuses, latencies, provider/model) sized by
  `OPS_WINDOW_SECONDS` / `OPS_MAX_EVENTS`.

## Configuration reload

`POST /admin/reload` re-reads `.env`, validates a fresh `Settings()`, and
applies only a fixed allowlist of reloadable fields in place (including
provider `enabled`/`api_key`/`model_priority` and the persistence
retention window). It snapshots before applying and rolls back on
mid-apply failure. API keys are reported by field name only; secrets are
never echoed.
