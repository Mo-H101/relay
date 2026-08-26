# Known Issues

Accepted, intentional, or confirmed-at-RC behavior that operators and users
should know before running Relay in production. This is the consolidated
public-facing list. The detailed engineering records live in
[docs/known-limitations.md](docs/known-limitations.md) and
[docs/release-decisions.md](docs/release-decisions.md).

Each item states what happens, why, and any mitigation.

---

## 1. OpenAI completions require an account with active billing

**Release caveat (RC1).** During RC every OpenAI completion returned
`HTTP 429 "You exceeded your current quota..."`. Relay surfaces this
correctly (`502` + `provider_error`), but **no OpenAI completion can
succeed until the account behind `OPENAI_API_KEY` has an active
plan/billing**. Until then the gateway is effectively NVIDIA-only.

- Why: the RC key was out of quota (external, environment issue).
- Mitigation: restore billing and re-run
  `python tests/run_live_smoke.py`. The quota error path itself is proven
  correct.
- References: known-limitations.md §6.

## 2. Unconfigured instances have no authentication

`RELAY_API_KEY` is empty by default, which **disables authentication**.
An instance exposed without setting it is unauthenticated.

- Mitigation: set `RELAY_API_KEY` to a long random value and enable
  `RELAY_AUTH_STORE=true` for per-client scoped keys (recommended
  production profile in [docs/deployment.md](docs/deployment.md)).
- References: known-limitations.md §6.

## 3. Single-process storage; no horizontal scale

`platform.db` (SQLite, schema v8) is **single-process / single-writer**.
Running more than one Relay process against the same database file
(`gunicorn -w N`, `uvicorn --workers N`) is **unsupported**: documented
corruption risk, split-brain routing, per-process counters.

- Mitigation: run exactly one Relay process per database file; scale with
  isolated instances (separate `PERSISTENCE_PATH`) and client-side routing.
- References: known-limitations.md §7.

## 4. 429 `Retry-After` honored only when explicitly enabled

Relay retries rate-limit (429) responses **immediately by default**; the
provider's `Retry-After` header is ignored unless
`RETRY_HONOR_RETRY_AFTER=true`. When enabled, Relay sleeps for
`Retry-After` (capped at `RETRY_AFTER_MAX_SECONDS`, default 60s), with
optional exponential backoff (`RETRY_BACKOFF_BASE_SECONDS`) and an overall
budget (`REQUEST_TIMEOUT_BUDGET_SECONDS`).

- Impact: with defaults, a retry under sustained rate limiting may arrive
  too early and fail again, consuming a retry slot. Behavior is safe (no
  busy-loop; one retry per candidate per `MAX_RETRIES`).
- Mitigation: enable `RETRY_HONOR_RETRY_AFTER=true` when upstreams set
  `Retry-After`.
- References: known-limitations.md §1.

## 5. Streaming start failures are not retried within a candidate

When a streaming request fails to start (provider error, timeout, empty
stream), Relay falls through to the **next candidate** in the ranked list
and does not retry the failed candidate. Mid-stream failures surface as an
SSE `data: {"error":{"type":"stream_error",...}}` chunk followed by
`[DONE]` rather than a terminal HTTP error.

- Why: the candidate list is the failover path; the client already holds
  partial content, so a `stream_error` chunk is the only correct signal.
- Mitigation: raw SSE consumers must handle the `stream_error` chunk;
  SDK clients surface it as a provider error.
- References: known-limitations.md §2 and §3.

## 6. Dynamic model discovery over-lists models an account cannot invoke

NVIDIA's `/models` endpoint returns the full catalog (221 models in the RC
environment), but a given account can only invoke a subset. Attempting the
others returns HTTP 404, which Relay classifies as `invalid_request`
(non-retryable) and moves to the next candidate. This inflates latency and
generates misleading telemetry when priorities are empty.

- Mitigation: set `NVIDIA_MODEL_PRIORITY` / `OPENAI_MODEL_PRIORITY` to the
  models the account can actually invoke. Priority ordering only reorders
  candidates — it never removes them.
- References: known-limitations.md §4; decision D4.

## 7. OpenRouter and Groq are not shipped in v1.0.0

**Release decision (D1).** The reserved `OPENROUTER_API_KEY` and
`GROQ_API_KEY` keys were removed; the runtime wires exactly six providers
(NVIDIA, OpenAI, Anthropic, Google Gemini, LM Studio, Ollama). Wiring
OpenRouter/Groq is deferred post-v1.

- References: CHANGELOG.md v1.0.0.

## 8. Dormant compaction paths are reserved, not wired

**Release decision (D12).** `ContextOverflowSignal` /
`should_retry_compacted` and `summarize_and_persist` have no production
call site (tests only) and are **accepted as reserved for v1.0.0**. The
live path is preflight compaction via the envelope builder; overflow wiring
is a post-v1 candidate.

- References: known-limitations.md §9 (D12).

## 9. Resume path has no dedicated rate limiter

**Release decision (D13).** `validate_resume` has no per-key rate limit;
protection relies on the 256-bit conversation/token space plus the durable
replay cap (`MAX_RESUME_REPLAYS`, default 3). The resume path **fails
closed** (a denial, not an error) when the replay tracker cannot be
persisted.

- References: known-limitations.md §8; decision D13.

## 10. One pre-existing timing flake is excluded from baselines

**Release decision (D14).** One pre-existing timing flake,
baseline-reproduced at commit `d344116`, is excluded from every measured
baseline and accepted as a known limitation. It is not a P0–P8 regression
and remains tracked for a post-v1 stabilization pass.

- References: known-limitations.md §9 (D14).

## 11. `relay events` is the log surface; some planned CLI subcommands are not shipped

**Release decision (D2).** The original roadmap listed `status`,
`providers`, `models`, `routing`, `logs`, `test` subcommands. The shipped
CLI is `setup`, `tui`, `serve`, `keys`, `provider`, `migrate`, `events`,
`conversations`, `apps`, `config`. The TUI provides the
status/models/providers/routing surfaces, and `relay events` tails the
security event log.

- References: CHANGELOG.md v1.0.0.

---

## Reporting

If you hit an issue not covered above, or a mitigation does not work,
open a bug report using [BUG_REPORT_TEMPLATE.md](BUG_REPORT_TEMPLATE.md).
Include the exact version (`relay --version`), OS, environment variables
set, and the request/response that failed.
