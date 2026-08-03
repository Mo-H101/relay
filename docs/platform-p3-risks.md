# P3 Risks — Async Chat Path, Streaming, Cancellation

Status: **draft — companion to `docs/platform-p3-plan.md` (awaiting approval).**

Risk register for migrating the chat hot path to asyncio. Each entry lists the
risk, likelihood, severity if realized, and the concrete mitigation. Risks are
grouped by theme. "Green" means the mitigation is embedded in the plan's
sub-phases; "carry-forward" means the risk is accepted for P3 and tracked.

---

## 1. Behavior drift between sync and async stacks

| # | Risk | Likelihood | Severity |
|---|---|---|---|
| 1.1 | `AsyncChatService` produces a different attempt sequence, `fallback_reason`, or budget behavior than `ChatService` (e.g., retry counter off-by-one, budget check timing, Retry-After cap vs `request_timeout_budget_seconds`) | Medium | High |
| 1.2 | `chat_across_stream*` async first-chunk semantics diverge (empty-stream handling, first-chunk pull ordering) so a stream starts on a different candidate than today | Medium | High |
| 1.3 | Async streaming emits a different wire shape (chunk boundaries, `id`/`created` stability, `[DONE]`, usage passthrough) breaking OpenAI SDK clients | Low | High |

**Mitigations (green):** shared pure policy helpers (`chat_policy.py`) imported
by both stacks so the numeric/decision logic cannot diverge (§3.1, P3b);
dedicated parity test suite comparing sync vs async `attempts`/`fallback_reason`/
`success`/`provider`/`model`/`error` on identical fake outcome queues
(`tests/test_async_parity.py`); existing API/TestClient/SDK-compat/rc_validation
suites as the wire-format gate. All default knobs (immediate retry, no budget)
preserve current behavior exactly.

---

## 2. Async httpx correctness (timeouts, streaming, proxy)

| # | Risk | Likelihood | Severity |
|---|---|---|---|
| 2.1 | `httpx.AsyncClient` timeout mapping diverges from sync (`ReadTimeout` vs generic `TimeoutException`, per-attempt vs per-read for streams), changing failure classification | Low | Medium |
| 2.2 | Async streaming error-body read differs (sync `_stream_error_text` calls `response.read()`; async must `await response.aread()`), producing a worse error message or a `ResponseNotRead` bug | Medium | Medium |
| 2.3 | Proxy / `trust_env` behavior differs between `httpx.post` (sync convenience) and `httpx.AsyncClient(**proxy_request_kwargs(...))`, changing proxy selection for env-configured proxies | Low | Medium |
| 2.4 | Per-call `AsyncClient` construction adds measurable overhead on the hot path vs sync one-shot `httpx.post` | Low | Low |

**Mitigations (green):** async methods reuse `proxy_request_kwargs` verbatim
and an explicit `await response.aread()` for stream error bodies; timeout
mapping is copied from the sync handlers and covered by MockTransport tests
(`test_async_provider_clients.py`); async-path tests assert the same auth
headers, payloads, and proxy kwargs as sync. P3a decides per-call vs shared
`AsyncClient`; if per-call, a carry-forward optimization note tracks a
connection-pooled client as a later enhancement.

---

## 3. Cancellation and resource cleanup

| # | Risk | Likelihood | Severity |
|---|---|---|---|
| 3.1 | Client disconnect mid-stream leaks the upstream httpx connection (async generator not awaited to completion) | Medium | High |
| 3.2 | `CancelledError` is swallowed or caught as `Exception` in the API generator, so telemetry/ops recording double-counts or a success is recorded for a cancelled stream | Medium | Medium |
| 3.3 | Cancelling a non-streaming request mid-retry leaves the loop in an inconsistent state (attempts list truncated, budget bookkeeping wrong) | Low | Medium |
| 3.4 | The event-loop-bound async handler accidentally runs blocking code (e.g., a sync store/`RequestLogger` call that does I/O), stalling the loop | Medium | Medium |

**Mitigations (green):** all upstream clients created with `async with`
context managers so exit/cancellation closes the connection; generator
`finally`/`except BaseException` ordering records exactly once and never after
`CancelledError`; dedicated `tests/test_async_cancellation.py` asserts no hang,
connection close (fake transport records close), and deterministic recording.
Carry-forward: a once-per-stream cancellation metric/ops label is acceptable;
the plan keeps the recording policy explicit (§6) rather than leaving it
implicit.

---

## 4. Scope creep and architecture drift

| # | Risk | Likelihood | Severity |
|---|---|---|---|
| 4.1 | Converting non-hot-path pieces (health checker, scan engine, `/health`, `/diagnostics`, TUI facade) to async inflates P3 beyond the approved scope | Medium | Medium |
| 4.2 | The shared `chat_policy.py` extraction (P3b) subtly changes sync `ChatService` behavior despite the "behavior-neutral" intent, regressing `test_retry_hardening.py` / `test_chat_service.py` | Medium | High |
| 4.3 | Dual-stack maintenance burden: two services and two client method sets drift over time after P3 lands | High | Low |
| 4.4 | Stale "P4" docstrings (`anthropic/gemini/ollama_client.py`, `setup/scan.py`) confuse readers about when async provider work lands | High | Low |

**Mitigations (green):** explicit non-goals in the plan (§1) and a checklist at
each sub-phase commit (only listed files touched); `chat_policy.py` extraction
is guarded by the full existing suite and landed before `AsyncChatService`
exists (P3b ordering), so any regression is caught in isolation; `test_ui_*.py`
+ `test_ui_boundary.py` keep the facade boundary stable; docstrings are updated
in P3a so code and roadmap agree ("P3", not "P4"). Carry-forward: P3 notes that
a future cleanup phase could consolidate the stacks once the async path is
proven in production.

---

## 5. Test-suite fragility

| # | Risk | Likelihood | Severity |
|---|---|---|---|
| 5.1 | `pytest-asyncio` interaction with existing `TestClient`-based tests (event-loop scope, `asyncio_default_fixture_loop_scope = "function"`) causes collection/loop-safety errors | Low | Medium |
| 5.2 | Flaky timing in async retry/backoff tests (real `asyncio.sleep` vs fake clock) makes CI nondeterministic | Medium | Medium |
| 5.3 | Existing tests that monkeypatch `httpx.post/get` or module-level `chat_svc` in `api/openai.py` break when the handler becomes async | Medium | High |

**Mitigations (green):** async timing tests use fake clocks or near-zero sleeps
where possible; `AsyncChatService` keeps the same method names
(`chat_across_messages`/`chat_across_stream_messages`) and result dict shape so
existing patches target the same surface; where a test specifically patches
sync `httpx.post`, an async twin (`MockTransport` on `httpx.AsyncClient`) is
added and the sync test remains valid for the retained sync path. P3c runs the
full API/SDK/rc_validation suite before and after the handler conversion to
isolate breakage.

---

## 6. Provider-specific async chat (Anthropic / Gemini / Ollama)

| # | Risk | Likelihood | Severity |
|---|---|---|---|
| 6.1 | Net-new `achat`/`achat_stream` for the three setup-only providers ship with wrong request/response shapes (Messages API, `generateContent`, Ollama `/api/chat`), producing 4xx or misparsed streams | Medium | High |
| 6.2 | Gemini API key passed as a query parameter (`key=...`) in an async request is accidentally logged (URL in an exception trace or metric) | Low | Medium |
| 6.3 | These providers' streaming endpoints have non-OpenAI SSE/delta shapes; assuming OpenAI framing yields empty/`[DONE]`-less streams | Medium | Medium |

**Mitigations (green):** each async provider method is exercised against a
fixture-shaped MockTransport with the provider's documented wire format
(`tests/test_async_provider_clients.py`), plus manual smoke via
`tests/run_live_smoke.py` for the three new chat providers where keys are
available; Gemini `key=` URL is built exactly as sync and never logged (reuse
`proxy_request_kwargs`, no URL emission; §9 of the plan). Carry-forward: if no
live key is available in the P3 window, these three chat paths ship behind the
same test-only verification as today's sync setup clients, documented as
"validated against fixture shapes; live smoke optional".

---

## 7. API compatibility and `relay serve`

| # | Risk | Likelihood | Severity |
|---|---|---|---|
| 7.1 | Converting `/chat` and `/v1/chat/completions` to `async def` changes status codes, error bodies, headers, or SSE framing (FastAPI sync→async handler differences) | Low | High |
| 7.2 | `Relay.achat` finishing logic (correlation id, request logging, telemetry, health feedback) diverges from `Relay.chat`, so async-served requests record different ops/metrics | Low | Medium |
| 7.3 | Embedded-server path (TUI `relay serve`/embedded uvicorn in a thread) is affected by async handlers or loop wiring | Low | Medium |

**Mitigations (green):** HTTP contract is asserted by the existing
TestClient/SDK-compat/rc_validation suites (unchanged) plus
`test_async_streaming.py` for the async wire format; shared finishing method
used by both `chat` and `achat` (§6.3); embedded server tests
(`test_embedded_server.py`) and TUI smoke (`tests/test_ui_windows_smoke.md`)
rerun after P3c.

---

## 8. Performance regressions

| # | Risk | Likelihood | Severity |
|---|---|---|---|
| 8.1 | Async path is *slower* than the threadpool path under low concurrency (event-loop overhead, per-call `AsyncClient`) | Low | Low |
| 8.2 | Residual blocking code on the event loop (sync stores/`RequestLogger`) becomes a serialization point under concurrency | Low | Medium |
| 8.3 | `aprobe_model` / `alist_models` async methods are added but unused on the hot path, so their value is only realized by later consumers (scan engine) | High | Low |

**Mitigations (green):** stores and `RequestLogger` are in-memory/thread-safe
and their per-call work is bounded (verified in P3c with a concurrency smoke
test reusing `test_concurrency.py` patterns); P3 does not claim to replace the
threadpool's throughput characteristics, only to remove per-request thread
occupancy and add cancellation/streaming ergonomics; the carry-forward note
tracks the connection-pooled `AsyncClient` and async health/scan consumers for
later phases.

---

## 9. Summary of carry-forward items (accepted, tracked)

1. Per-call `AsyncClient` overhead → connection-pooled client later.
2. `ScanEngine` async consumer of `aprobe_model` → deferred (documented seam).
3. Async `ServiceFacade` / TUI to_thread removal → deferred.
4. Consolidating the dual stack (sync + async) → future cleanup phase.
5. Live smoke for Anthropic/Gemini/Ollama async chat → optional, key-gated.
