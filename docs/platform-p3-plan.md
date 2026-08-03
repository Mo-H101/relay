# P3 Plan — Async Chat Path, Streaming, Cancellation

Status: **draft — awaiting approval. No code written.**

Scope: migrate the chat hot path to asyncio (async provider clients, an async
`ChatService`, streaming, async failover/routing, cancellation, timeout,
retry) **without rewriting the sync stack, breaking `relay serve` API
compatibility, or failing the existing suite** (1052 passed / 5 skipped at
P2e, `444c70c`).

---

## 1. Scope

### In scope

- **Async provider interface** on all six clients: `achat()`, `achat_stream()`,
  `alist_models()`, `aprobe_model()` (+ `achat_messages()`,
  `achat_stream_messages()`, `akey_check()`).
  - NVIDIA, OpenAI, LM Studio: async methods on the shared
    `OpenAICompatibleClient` (they inherit).
  - Anthropic, Gemini, Ollama: async list/probe/key-check **and the first real
    `achat()` / `achat_stream()` implementations** (their sync chat methods
    currently raise `NotImplementedError`).
- **Async `ChatService`** (`AsyncChatService`) mirroring the sync
  `ChatService` algorithm exactly: failover, retry, Retry-After/backoff,
  request-timeout budget, per-attempt records, fallback reason.
- **Async streaming** via async generators for `/v1/chat/completions` and the
  native `/chat` streaming surface.
- **Cancellation**: cooperative cancellation for non-streaming awaits and
  mid-stream client disconnects; httpx async clients closed via context
  managers on exit/cancellation.
- **Timeout/retry parity**: same `settings.request_timeout` per request,
  same `request_timeout_budget_seconds` wall-clock budget, same
  `RETRY_HONOR_RETRY_AFTER` / `RETRY_BACKOFF_*` semantics, `asyncio.sleep`
  instead of `time.sleep` so waits are cancellable.
- **API handlers** for the two chat endpoints become `async def`
  (`/chat`, `/v1/chat/completions`); HTTP contract unchanged.
- **Reconciliation**: update the stale "P4" docstrings in
  `anthropic_client.py`, `gemini_client.py`, `ollama_client.py`,
  `setup/scan.py` — the async-first provider client work is **P3**, per the
  approved roadmap.
- **Docs**: this plan, `docs/platform-p3-risks.md`, architecture/PROJECT_LOG
  updates, sub-phase statuses.

### Non-goals (explicitly deferred)

- **No rewrite of the sync path.** `ChatService`, sync provider methods,
  `Relay.chat`, the TUI facade, and their callers stay untouched and keep
  working. Sync is the compatibility adapter for everything not on the API hot
  path.
- **No async conversion of `HealthChecker` / `HealthRefresher`** (they run on
  a background thread; sync is correct for them). `aprobe_model()` is defined
  and tested in P3; its executor consumer (`ScanEngine`) stays sync — that
  swap is already documented as a seam (`setup/scan.py` docstring).
- **No async TUI facade.** The 13 `asyncio.to_thread` sites are UI-side and
  remain (see §7.2).
- **No new HTTP endpoints, no new env vars, no DB/migrations, no dependency
  changes** (httpx 0.28.1 already ships `httpx.AsyncClient`).
- **No security/redaction changes** — async reuses the existing pure helpers
  verbatim (§9).
- P4+ scope (keyring, `relay.db`, async scanning consumers, CLI config
  commands) untouched.

---

## 2. Current state (sync everywhere, verified at planning time)

| Layer | Today | Blocking characteristic |
|---|---|---|
| `app/api/chat.py` `/chat` | sync `def` handler → `relay.chat()` | FastAPI threadpool; one OS thread per request |
| `app/api/openai.py` `/v1/chat/completions` | sync `def` handler; module-level `chat_svc = ChatService()`; sync `StreamingResponse` generator | threadpool + blocking httpx; no cancellable stream |
| `app/services/chat_service.py` | sync `ChatService` (`chat_across`, `chat_across_messages`, `chat_across_stream`, `chat_across_stream_messages`), `Attempt`, `_retry_wait_seconds`, `_budget_exhausted`, `_fallback_reason`, `time.sleep` backoff | blocking sleep, no cancellation |
| `app/providers/openai_compat_client.py` | sync `httpx.post/get/stream` for chat/chat_messages/chat_stream/chat_stream_messages/list_models/probe_model/key_check; `_safe_provider_body`, `_retry_after_seconds`, `_stream_error_text`, `proxy_request_kwargs` | sync httpx I/O |
| `app/providers/anthropic|gemini|ollama_client.py` | sync list/probe/key_check only; `chat*` raise `NotImplementedError` ("until P4") | no chat at all |
| `app/services/health_checker.py` | sync; `ThreadPoolExecutor(max_workers=12)` model probes; sync `httpx.get` connectivity | thread pool per check (not hot path) |
| `app/services/health_refresher.py` | daemon thread loop calling sync `check()` | background only |
| `app/setup/scan.py` | sync `ScanEngine` with `ThreadPoolExecutor`; docstring notes the async `aprobe_model` seam | setup wizard only |
| `app/core/relay.py` | sync facade: `chat`, `health`, `choose_provider`; owns all stores/services | sync orchestration |
| `app/api/*` other routes | sync `def` (FastAPI threadpool) | not on chat hot path; unchanged |

Key enablers already present and reused as-is:

- `app/services/failure_classifier.py` — pure `classify()`, `RETRYABLE`,
  `PROVIDER_LEVEL`, `FailureKind`. Loop-independent.
- `app/services/client_registry.py` — name → client instance; async service
  uses the same registry (clients expose both sync and async methods).
- `app/core/config.py` — `request_timeout`, `max_retries`,
  `retry_honor_retry_after`, `retry_after_max_seconds`, `retry_backoff_*`,
  `request_timeout_budget_seconds` (no new knobs needed).
- `pytest-asyncio` already configured (`asyncio_default_fixture_loop_scope =
  "function"`).

---

## 3. Design: async layer alongside sync

### 3.1 Principle

Additive dual-stack. The sync stack stays the source of behavior truth; the
async stack is a **structural mirror** that shares every pure policy helper so
it cannot drift, plus a **parity test suite** that proves both stacks produce
identical attempt sequences and results for the same inputs.

### 3.2 What moves / what stays / compat adapters

| Component | Decision | Reason |
|---|---|---|
| Provider HTTP methods (`chat*`, `list_models`, `probe_model`, `key_check`) | **Move to async** (new `a*` methods added; sync methods retained) | async httpx is the hot path |
| `ChatService` orchestration | **New `AsyncChatService`** (additive) | TUI + sync callers keep using sync service |
| `/chat`, `/v1/chat/completions` handlers | **Move to `async def`** | removes threadpool occupancy; enables cancellation + async streaming |
| Retry backoff / Retry-After wait | **Move to `asyncio.sleep`** in async path | cancellable; budget-aware like today |
| `HealthChecker`, `HealthRefresher`, `ScanEngine` | **Stay sync** | background threads/executors; not the hot path; `ScanEngine` swap already queued as a later seam |
| TUI facade + 13 `asyncio.to_thread` sites | **Stay** | UI-side; sync facade by design |
| All stores, config, classification, metrics, `ops_store`, redaction | **Stay** | pure/in-memory; shared by both stacks |
| **Compat adapter** | Sync `ChatService` + sync provider methods + `Relay.chat` remain as the sync entry point | "preserve the sync chat path" verbatim |
| **Compat adapter** | `Relay.achat` async facade mirrors `Relay.chat` (same correlation/log/telemetry/feedback) | `/chat` handler becomes async without forking Relay logic |

---

## 4. Async provider interface

### 4.1 Interface (on every client)

```python
async def achat(provider, model, message, **gen_kwargs) -> str
async def achat_messages(provider, payload: dict) -> dict
async def achat_stream(provider, model, message, **gen_kwargs) -> AsyncIterator[str]
async def achat_stream_messages(provider, payload: dict) -> AsyncIterator[dict]
async def alist_models(provider) -> list
async def aprobe_model(provider, model) -> ModelProbe
async def akey_check(provider) -> tuple
```

`ModelProbe`, `Provider`, exception types, and metrics labels are identical to
the sync surface.

### 4.2 NVIDIA / OpenAI / LM Studio (`OpenAICompatibleClient`)

One async implementation set on the base class, inherited by all three. Each
async method:

- Uses `async with httpx.AsyncClient(**proxy_request_kwargs(provider, url))` or
  a module-level shared `AsyncClient` (decide in P3a; per-call matches current
  sync `httpx.post/get` semantics and keeps proxy trust_env behavior exact).
- Reuses `proxy_request_kwargs`, `_safe_provider_body`, `_retry_after_seconds`,
  `_stream_error_text` unchanged (all pure).
- Maps `httpx.ReadTimeout`/`httpx.TimeoutException` → `ProviderTimeout` and
  `httpx.HTTPError` → `ProviderHTTPError` exactly like the sync methods.
- Records the same `relay_metrics.record_provider` /
  `record_provider_timeout` calls with the same labels/units.

Streaming: `async with client.stream("POST", ...)` + `aiter_lines()`, same SSE
`data: ` / `[DONE]` parsing, same malformed-chunk tolerance, same
`ProviderHTTPError` on `status_code >= 400` with the body bounded and redacted
via `_safe_provider_body` (async variant of `_stream_error_text` reads the
body with `await response.aread()`).

### 4.3 Anthropic / Gemini / Ollama (first real async chat)

- Async `alist_models` / `aprobe_model` / `akey_check` are direct ports of the
  sync setup methods (same endpoints, headers, URL-key embedding for Gemini,
  redaction via `app/providers/availability.safe_error_body`).
- `achat` / `achat_stream` are **net-new** (currently `NotImplementedError`):
  - Anthropic: `POST {base_url}/messages` (`x-api-key` +
    `anthropic-version: 2023-06-01`), Messages API shape.
  - Gemini: `POST {base_url}/models/{model}:generateContent?key=...`,
    `generateContent` shape.
  - Ollama: `POST {base_url}/api/chat`, chat shape (`stream: False` for
    `achat`, SSE for `achat_stream`).
  - `achat_stream` yields content deltas with the same failure contract.
- Docstrings updated from "P4" → "P3" (§1 reconciliation).

### 4.4 Error / redaction / timeout reuse

| Concern | Reused verbatim |
|---|---|
| Error-body redaction | `_safe_provider_body` (openai_compat), `safe_error_body` (availability) |
| Retry-After parsing | `_retry_after_seconds` |
| Proxy/trust_env behavior | `proxy_request_kwargs` |
| Failure classification | `app/services/failure_classifier.py::classify` |
| Metrics | `relay_metrics.record_provider` / `record_provider_timeout` |

---

## 5. AsyncChatService

New module `app/services/async_chat_service.py` (or an async branch shared
with sync — see decision in §10 note). Mirrors `ChatService`:

| Sync method | Async mirror |
|---|---|
| `chat_across` | `achat_across` |
| `chat_across_messages` | `achat_across_messages` |
| `chat_across_stream` | `achat_across_stream` |
| `chat_across_stream_messages` | `achat_across_stream_messages` |

### 5.1 Failover / retry / budget

- Same loop: candidates in order → per-candidate retry loop (≤ `max_retries`) →
  `PROVIDER_LEVEL` skips provider → `RETRYABLE` gates retries →
  `_retry_wait_seconds` wait → `_budget_exhausted` break.
- Same result dict shape: `success, provider, model, response|stream_gen,
  latency_ms, fallback_reason, error, attempts` (attempts are
  `Attempt.to_dict()`), so the API layer and telemetry/ops recording are
  unchanged.
- Waits are `await asyncio.sleep(wait)`; the budget check uses
  `asyncio.get_running_loop().time()` (same wall-clock semantics as
  `time.perf_counter`).
- `CancelledError` is not swallowed: it propagates out of the service; httpx
  context managers close upstream connections; the API layer records the
  outcome (or omits recording) deterministically per policy in §6.

### 5.2 Streaming

- `achat_across_stream_messages` / `achat_across_stream` are async generators
  returning a success dict with an async `stream_gen`. First-chunk pull to
  verify the stream started (mirrors sync `next(stream_gen)`; empty stream →
  `failure_type: "empty_stream"` attempt + next candidate).
- Mid-stream exceptions propagate to the API generator's error handler
  (classified via `classify`, stream-error chunk + `[DONE]` — same as today).

### 5.3 Cancellation

- Non-streaming: task cancellation propagates through awaits; per-attempt
  recording happens before the next await so cancelled requests leave an
  accurate attempts list (or none if cancelled before first attempt).
- Streaming: Starlette cancels the async generator on client disconnect;
  `finally` in the generator closes the httpx stream and records
  telemetry/ops. No leaked connections; no record after `CancelledError`
  (guarded by `except BaseException` + `finally` ordering).

---

## 6. API layer

### 6.1 `/chat` (`app/api/chat.py`)

- `async def chat(...)`; replaces `relay.chat(...)` with `await relay.achat(...)`.
- Identical: response model, `X-Relay-Correlation-Id`, 502/503 `HTTPException`
  mapping, `_record_chat` (metrics + ops), `_resolve_task` logic.
- The sync `Relay.chat` remains for the TUI and any sync caller.

### 6.2 `/v1/chat/completions` (`app/api/openai.py`)

- `async def openai_chat_completion(...)`; module-level service becomes an
  `AsyncChatService` (same instance naming so existing patches/tests targeting
  `chat_across_messages` / `chat_across_stream_messages` keep working).
- Streaming: `StreamingResponse` wraps an `async def stream_generator()` that
  `async for`-consumes the async `stream_gen` and emits `data: {...}\n\n`
  chunks + `data: [DONE]\n\n`; mid-stream error → `data: {"error": {...}}\n\n`
  + `[DONE]` (unchanged wire format).
- Non-streaming: `await chat_svc.chat_across_messages(...)`; identical
  `_openai_error_response`, telemetry/health recording, `_record_chat`.
- `/v1/models`, `/health`, `/diagnostics`, `/providers`, `/admin/reload`,
  `/feedback`, `/metrics`, `/decision/*` **stay sync `def`** (not the chat hot
  path).

### 6.3 Relay facade (`app/core/relay.py`)

- Extract the post-chat finishing (correlation id, `request_logger.chat`,
  telemetry, health feedback) into a shared method used by both `chat` (sync)
  and the new `async def achat(...)`.
- `achat` builds candidates exactly like `chat` (routing/decision/health
  aware) then calls `AsyncChatService.achat_across`.

---

## 7. Performance analysis

### 7.1 Blocking points today

1. **API chat handlers are sync `def`** → FastAPI threadpool (default 40
   threads). Each `/chat` or `/v1` request occupies an OS thread for the full
   provider round-trip; high concurrency exhausts the pool.
2. **Sync `httpx.post/get/stream`** inside provider clients — blocking socket
   I/O on those threads.
3. **`time.sleep`** in retry backoff — blocks a thread and is not cancellable.
4. Health checker's `ThreadPoolExecutor(max_workers=12)` and `ScanEngine`'s
   `ThreadPoolExecutor` — bounded, background/UI only, not on the hot path.
5. Streaming today uses a **sync generator** — Starlette iterates it on the
   event loop, so each `next()` blocks the loop for the full provider read.

### 7.2 `asyncio.to_thread` inventory (all UI-side, all retained)

| Site | Call |
|---|---|
| `app/ui/app.py:255` | embedded server start |
| `app/ui/screens/providers.py:186,217,259` | wizard / rescan / toggle |
| `app/ui/screens/models.py:265,298` | priority apply / provider toggle |
| `app/ui/screens/diagnostics.py:200,228` | test connection / export |
| `app/ui/screens/configuration.py:176` | save config |
| `app/ui/screens/chat.py:168,238,258,320` | probe / random chat / specific chat / stream consume |

**Where removable:** none of these are on the API hot path; they are the TUI's
correct bridge to the blocking facade. Converting them to async would require
an async `ServiceFacade` — out of P3 scope. The threadpool removal happens on
the **API side** (sync `def` → `async def`), not the TUI side.

### 7.3 What async removes

- One thread per chat request (async handlers run on the event loop).
- Blocking socket I/O in the hot path (async httpx).
- Non-cancellable retry sleep and non-cancellable streaming reads.
- The sync-generator-on-event-loop stall for streaming responses.

Residual sync consumers (health refresher thread, state flusher, TUI) remain
on threads/executors by design and are not regressed.

---

## 8. Testing strategy

### 8.1 New tests

| File | Coverage |
|---|---|
| `tests/test_async_provider_clients.py` | `achat`/`achat_messages`/`achat_stream`/`achat_stream_messages`/`alist_models`/`aprobe_model`/`akey_check` on the OpenAI-compatible clients **and** Anthropic/Gemini/Ollama via `httpx.MockTransport` / fake `AsyncClient`: auth headers, payloads, SSE parsing, `[DONE]`, malformed chunks, status≥400 redacted error, ReadTimeout→`ProviderTimeout`, retry-after parsing, Gemini `key=` URL embedding, proxy kwargs |
| `tests/test_async_chat_service.py` | `AsyncChatService` unit tests mirroring `test_chat_service.py`: first-candidate success, attempt history, fallback reason, retryable retry, max_retries, non-retryable no-retry, provider-level skip, unknown-is-retryable, rate-limit/server-error retry, budget exhaustion, Retry-After/backoff timing with fake clocks |
| `tests/test_async_parity.py` | **Regression gate**: identical candidate lists + fake outcome queues → identical `attempts`, `fallback_reason`, `success`, `provider`, `model`, `error` from sync `ChatService` and `AsyncChatService` (non-stream + stream-start) |
| `tests/test_async_cancellation.py` | `task.cancel()` mid-await and mid-stream: no hang, no leaked connection (fake transport records close), deterministic telemetry/ops recording, `CancelledError` not swallowed |
| `tests/test_async_streaming.py` | async generator streaming over the API: SSE wire shape, stable `id`/`created`, `[DONE]`, mid-stream error chunk, usage chunk passthrough, empty-stream failover to next candidate |

### 8.2 Regression gates (must stay green, unchanged)

- Full `pytest tests -q` — baseline **1052 passed / 5 skipped** at P2e.
- Existing suites that exercise the sync path and/or the API surface:
  `test_chat_service.py`, `test_retry_hardening.py`, `test_api_integration.py`,
  `test_openai_api.py`, `test_openai_sdk_compat.py`, `test_openai_conformance.py`,
  `test_rc_validation.py`, `test_lmstudio_integration.py`, `test_health.py`,
  `test_health_refresher.py`, `test_scan.py`, `test_proxy.py`,
  `test_redaction.py`, all `test_ui_*.py`.
- Boundary rule (`test_ui_boundary.py`): screens still import only
  `ServiceFacade`/theme/setup_adapter — no new core/provider imports.
- Secret scan clean (`git grep` for key patterns; no `sk-`/`Bearer` in new
  code, logs, or errors).

### 8.3 Sync/async parity harness

Async tests reuse the existing deterministic fake-client pattern
(`test_chat_service.py` / `test_retry_hardening.py`) with an added async
`FakeAsyncClient` exposing `achat`/`achat_messages`/`achat_stream_messages`
driven by the same per-model outcome queues, plus the sync fake for the parity
side.

---

## 9. Security invariants (unchanged)

1. **No API keys exposed.** Async clients send the same auth (Bearer /
   `x-api-key` / Gemini `key=` query param) and never log headers, URLs, or
   payloads.
2. **No redaction change.** Error bodies flow through `_safe_provider_body` /
   `safe_error_body` (bounded to 200 chars, control chars stripped, key
   replaced with `[REDACTED]`) — verified by async-path redaction tests.
3. **No new request-body logging.** Async methods forward the payload to the
   provider only; `MetricsMiddleware`, `RequestLogger`, `ops_store`,
   diagnostics, and the async streaming path never persist prompts or bodies.
4. Proxy credentials remain configuration-only (never logged) via the reused
   `proxy_request_kwargs`.

---

## 10. Sub-phase commit plan

Every sub-phase ends with the full suite green and a clean secret scan.

### P3a — Async provider clients
New async methods on `OpenAICompatibleClient` + Anthropic/Gemini/Ollama
(including first `achat`/`achat_stream`); update "P4" docstrings → P3;
`tests/test_async_provider_clients.py`. Sync methods untouched.
**Commit `P3a: async provider client layer`.**

### P3b — AsyncChatService + parity
Extract shared pure policy helpers (attempt dict, retry-wait, budget, fallback
reason) into a module both services import (behavior-neutral, guarded by the
existing suite); implement `AsyncChatService`; add
`tests/test_async_chat_service.py` + `tests/test_async_parity.py` +
`tests/test_async_cancellation.py`.
**Commit `P3b: async chat service with sync parity`.**

### P3c — Async API hot path
`async def` for `/chat` and `/v1/chat/completions`; `Relay.achat` +
shared finishing; `AsyncChatService` wired into `api/openai.py`; async
`StreamingResponse` generator; `tests/test_async_streaming.py`. Existing API /
TestClient / SDK-compat / rc_validation suites stay green.
**Commit `P3c: async API chat endpoints and streaming`.**

### P3d — Docs & final gates
Statuses in this plan + `docs/platform-p3-risks.md`; `docs/architecture.md`
and `PROJECT_LOG.md` async-layer notes; full `pytest tests -q`; secret scan;
packaging/wizard checks.
**Commit `P3d: P3 documentation and gates`.**

---

## 11. File inventory

### New
| File | Purpose |
|---|---|
| `app/services/async_chat_service.py` | `AsyncChatService`: `achat_across*` mirrors of sync `ChatService`. |
| `app/services/chat_policy.py` | Shared pure helpers (attempt dict, retry-wait, budget, fallback reason) used by sync + async services. |
| `tests/test_async_provider_clients.py` | Async client method tests (all six providers). |
| `tests/test_async_chat_service.py` | `AsyncChatService` unit tests. |
| `tests/test_async_parity.py` | Sync-vs-async behavior parity gate. |
| `tests/test_async_cancellation.py` | Cancellation / connection-close / recording tests. |
| `tests/test_async_streaming.py` | Async SSE streaming + failover + mid-stream error tests. |
| `docs/platform-p3-plan.md` | This document. |
| `docs/platform-p3-risks.md` | Risk register (separate deliverable). |

### Modified
| File | Change |
|---|---|
| `app/providers/openai_compat_client.py` | Add async methods + async stream error-body read; reuse all pure helpers. |
| `app/providers/anthropic_client.py` | Async list/probe/key-check + `achat`/`achat_stream`; docstring P4→P3. |
| `app/providers/gemini_client.py` | Same (Gemini `generateContent`). |
| `app/providers/ollama_client.py` | Same (Ollama `/api/chat`). |
| `app/services/chat_service.py` | Import shared policy helpers (behavior-neutral); sync methods unchanged. |
| `app/core/relay.py` | Extract shared finishing; add `async def achat`. |
| `app/api/chat.py` | `async def chat` → `await relay.achat`. |
| `app/api/openai.py` | `async def` handler; `AsyncChatService`; async stream generator. |
| `docs/architecture.md`, `PROJECT_LOG.md`, `README.md` | Async-layer notes, milestone update. |

No changes to config, stores, security/auth, or the setup wizard.

---

## 12. Gate criteria

1. Full `pytest tests -q` green at every sub-phase (≥ 1052 passed; no sync
   tests regressed).
2. Parity: for identical fake outcome queues, sync and async services produce
   identical `attempts`, `fallback_reason`, `success`, and result fields.
3. API compatibility: `/chat` and `/v1/chat/completions` wire format
   byte-identical (status codes, error shapes, headers, SSE `data:`/`[DONE]`
   framing); `relay serve` unchanged.
4. Cancellation: disconnect / `task.cancel()` mid-request and mid-stream
   completes without hang or leaked httpx connections.
5. Timeout/retry: ReadTimeout → `ProviderTimeout`; budget exhaustion stops the
   loop; Retry-After/backoff honored with `asyncio.sleep`; `CancelledError`
   never swallowed.
6. Security: redaction tests pass on the async path; no keys/URLs/bodies in
   logs, metrics, ops events, or errors; secret scan clean.
7. Boundary rule green: screens import only facade/theme/setup_adapter.
8. `docs/architecture.md` and `PROJECT_LOG.md` describe the async layer and
   sub-phase commits are attributed.
