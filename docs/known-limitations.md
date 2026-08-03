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
  no OpenAI completion could succeed until billing was restored. See
  `docs/blockers-before-public-release.md`.
- `RELAY_API_KEY` is empty by default, which **disables authentication**.
  Do not expose an instance without setting it.
