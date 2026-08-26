# Known Limitations

Validated, intentional, or confirmed-at-RC behavior that operators should
know before (or after) running Relay as a production OpenAI-compatible
gateway. Each item states what happens, why, and any mitigation.

## 1. 429 `Retry-After` is honored only when explicitly enabled

Relay retries rate-limit (429) responses **immediately by default**; the
provider's `Retry-After` header is ignored unless `RETRY_HONOR_RETRY_AFTER=true`.

When enabled, Relay sleeps for the provider's `Retry-After` (capped at
`RETRY_AFTER_MAX_SECONDS`, default 60s) before the next retry of the same
candidate. Without a `Retry-After` header, exponential backoff can be
enabled via `RETRY_BACKOFF_BASE_SECONDS` (base in seconds, capped at
`RETRY_BACKOFF_MAX_SECONDS`). A total request wall-clock budget can be set
with `REQUEST_TIMEOUT_BUDGET_SECONDS` (0 = unlimited, the default); waits
never exceed the remaining budget.

- Proven by: `tests/test_retry_hardening.py` (Retry-After honored/ignored,
  exponential backoff base and cap, budget bounds) and
  `tests/test_rc_validation.py::TestReliabilityMatrix::test_retry_on_429_ignores_retry_after`
  (default profile: the retry completes in well under the `Retry-After: 1`
  second the scripted upstream returned).
- Impact: with defaults, a retry under sustained rate limiting may arrive
  too early and fail again, consuming a retry slot. Behavior is safe (no
  busy-loop; one retry per candidate per `MAX_RETRIES`), but throughput
  under heavy 429 load is not optimal.
- Mitigation: enable `RETRY_HONOR_RETRY_AFTER=true` (and optionally
  `RETRY_BACKOFF_BASE_SECONDS`) when upstreams set `Retry-After`.

## 2. Streaming start failures are not retried within a candidate

When a streaming request fails to start (provider error, timeout, empty
stream), Relay does **not** retry that candidate. It falls through to the
next candidate in the ranked list only (`app/services/chat_service.py`:
`chat_across_stream_messages`, `chat_across_stream`). This is intentional
(the candidate list is the failover path) and is exercised by the RC
reliability tests.

## 3. Mid-stream failures become an SSE `stream_error` chunk

A stream that dies after producing content emits
`data: {"error":{"type":"stream_error",...}}` followed by `[DONE]`
instead of a terminal HTTP error. The client has already received partial
content, so this is the only correct signal available. SDK clients surface
it as a provider error; raw SSE consumers must handle the `stream_error`
chunk. Proven by `test_mid_stream_failure_emits_error_and_records`.

## 4. Dynamic model discovery over-lists models an account cannot invoke

NVIDIA's `/models` endpoint returns the full catalog (221 models in the
RC environment), but a given account can only invoke a subset. Attempting
the others returns HTTP 404 `"Function ... Not found"`, which Relay
classifies as `invalid_request` (non-retryable) and moves to the next
candidate.

Observed live during the smoke run: the native `/chat` walk visited
several inaccessible NVIDIA models before finding a working one. This
inflates latency and generates misleading `invalid_request` telemetry.

- Mitigation: set `NVIDIA_MODEL_PRIORITY` / `OPENAI_MODEL_PRIORITY` to the
  models the account can actually invoke. Priority ordering only reorders
  candidates — it never removes them — so this shortens but does not
  eliminate the walk. The live smoke pins `SMOKE_NVIDIA_MODEL` for exactly
  this reason.

## 5. `/v1` errors always use the OpenAI error shape

Non-2xx responses from `/v1` are `{"error": {...}}` (never FastAPI's
`{"detail": ...}`), and provider bodies are bounded (200 chars), scrubbed
of control characters, and redacted of the API key before they appear in
error responses or logs. This is deliberate: OpenAI SDK clients expect the
`error` shape. Streamed provider errors now surface the provider's real
body (an earlier bug where httpx's internal `ResponseNotRead` message
leaked is fixed and covered by
`test_stream_start_error_surfaces_provider_body`).

## 6. Environment facts recorded at RC time

- The `.env` OpenAI key was **out of quota** during RC (HTTP 429 on every
  completion). Relay surfaced this correctly (502 + `provider_error`), but
  no OpenAI completion could succeed until billing was restored.
- `RELAY_API_KEY` is empty by default, which **disables authentication**.
  Do not expose an instance without setting it.

## 7. Single-process storage model; no horizontal scale

`platform.db` (SQLite, schema v8) is **single-process / single-writer**.
Running more than one Relay process against the same database file (for
example `gunicorn -w N` or `uvicorn --workers N`) is **unsupported**: every
process gets its own writer to the same file, which is a documented
corruption risk, plus split-brain routing and per-process counters.

- Mitigation: run exactly one Relay process per database file; scale by
  running isolated instances with separate `PERSISTENCE_PATH` values and
  client-side routing.
- Continuity tables (schema v7/v8) are written only on the
  `ContinuityFlusher` thread of the single process; the coordinator is
  bounded at 512 in-memory states and the flusher queue at 10,000 rows, so
  write pressure stays bounded at large conversation counts.
- Operators may run `PRAGMA wal_checkpoint(TRUNCATE)` during maintenance
  to shrink the WAL; this is documented, not automated.

## 8. Continuity resume protocol (opt-in, bounded replays)

Project continuity is **off by default** and additive when enabled
(`CONTINUITY_ENABLED=true`). Opt-in conversations are resumed with a
**single-use resume token**; replay attempts are capped by
`MAX_RESUME_REPLAYS` (default 3) and tracked durably in `resume_replays`
(schema v8) so the cap survives restarts. The resume path **fails closed**
(a denial, not an error) when the replay tracker cannot be persisted.

- Clients that do not send `X-Relay-Conversation-Id` /
  `X-Relay-Project-Id` see no continuity behavior and no storage.
- Content-aware handoff is a further opt-in (`CONTINUITY_CONTENT_CONTEXT_ENABLED=true`,
  requires `CONTINUITY_ENABLED=true`, both restart-required). When enabled, a
  bounded, redacted content summary of the in-request messages is added to the
  envelope and over-budget arrays are compacted (digest + recent tail) before
  forwarding. This is **ephemeral only**: the digest exists solely inside the
  forwarded payload and is never persisted, logged, exported, or surfaced in
  metrics/events. Because it is derived from message *content*, operators who
  enable it must accept that derived content text flows to the provider on the
  request path (it remains redacted for secret shapes and framed as data, not
  instructions). Default is off, so the metadata-only privacy posture is
  unchanged unless explicitly enabled.
- The durable replay tracker only covers the resume path: it is not a
  general rate limiter on `validate_resume`. RC1 decision (D13): **no
  dedicated rate limiter on the resume path for v1.0.0** — the 256-bit
  conversation/token space and the durable replay cap bound abuse
  (document-only disposition, revisit post-v1).

## 9. RC1 release-caveat record (decisions D11–D15)

Recorded at the RC1 decision closure:

- **OpenAI quota (B1) — NVIDIA-ready-only RC1.** The `.env` OpenAI key is
  out of quota (HTTP 429 on every completion). RC1 and the `v1.0.0` release
  proceed **NVIDIA-ready-only**; the quota is a documented release caveat,
  not a gate. The OpenAI live smoke (`tests/run_live_smoke.py`) is a
  **future validation item** to be re-run when the key has quota. The quota
  error path itself is proven correct live (correct `502` + `provider_error`
  shape).
- **Dormant paths reserved (D12).** `ContextOverflowSignal` /
  `should_retry_compacted` and `summarize_and_persist` have no production
  call site and are **accepted as reserved** for v1.0.0. The live compaction
  path is preflight compaction via the envelope builder
  (`handoff.py:673`); overflow wiring is a post-v1 candidate if live soak
  shows overflow.
- **Known timing flake accepted (D14).** One pre-existing timing flake,
  baseline-reproduced at `d344116`, is **excluded from every measured
  baseline** and accepted as a known limitation for the
  tag; it is not a P0–P8 regression and is tracked for a post-v1
  stabilization pass.
