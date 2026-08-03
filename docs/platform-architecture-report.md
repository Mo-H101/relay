# Relay — Current Architecture Report

Date: 2026-08-03
Prepared for: Phase 9 — platform transformation (analysis only, no code changed)

This report describes the system as it exists today: what Relay is, how it
is structured, its components, data flows, configuration surface, storage,
observability, concurrency model, testing, and the gaps that matter when
turning it into a user-facing AI gateway platform.

---

## 1. Summary

Relay is a **single-process, synchronous, FastAPI-based LLM routing proxy**
with advanced routing intelligence. It sits between a client (Cline,
OpenCode, curl, any OpenAI-compatible tool) and one or more LLM providers
(NVIDIA, OpenAI, LM Studio), picks the best provider/model per request,
fails over across models and providers, and learns from real request
outcomes.

It is **developer-oriented**: configured through a `.env` file and a
minimal `argparse` CLI, operated via `curl`/API, and observable via
Prometheus text metrics and a JSON diagnostics endpoint. There is **no
user-facing UI, no packaged install, no web console, no API-key
management, no multi-tenancy.**

Current state: 821 passing tests, 5 skipped; single git commit
("Initial Relay release candidate"); working tree clean.

---

## 2. Tech stack

| Area | Choice |
| --- | --- |
| Web framework | FastAPI 0.116.1 + uvicorn 0.35.0 (ASGI) |
| HTTP client | httpx 0.28.1 (synchronous) |
| Config | python-dotenv 1.1.1 (`.env` at project root) |
| Validation | pydantic 2.11.7 (request schemas, env validation) |
| Storage | SQLite (stdlib `sqlite3`), write-behind |
| Metrics | hand-rolled Prometheus text registry (no dependency) |
| Logging | stdlib `logging` with JSON formatter |
| CLI | stdlib `argparse` (single `setup` subcommand) |
| Tests | pytest 8.3.4, `openai` SDK (opt-in conformance), 48 files |
| Packaging | **none** (no `pyproject.toml`, no console scripts) |

Runtime dependencies (pinned): `fastapi`, `uvicorn`, `httpx`,
`python-dotenv`, `pydantic`. Dev: `pytest`, `openai`.

---

## 3. Layered architecture

One-way dependency flow (documented in `docs/architecture.md`):

```
api routers (thin HTTP adapters)
      ↓
Relay facade (app/core/relay.py)  ← composition root, singleton
      ↓
services (app/services/*, 30 modules)
      ↓
providers (app/providers/*)  ← Provider dataclass + OpenAI-compatible clients
      ↑
app/core/config.py  ← root of the dependency graph (settings singleton)
```

### 3.1 Core (`app/core/`)

- **`config.py`** — loads `.env` at import, validates ~80 env vars eagerly
  (fail-fast with clear `ValueError` messages), exposes a process-wide
  `settings` singleton. Strict boolean parsing (only literal `true`).
  Several parsed keys are **unused**: `ANTHROPIC_API_KEY`,
  `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `OLLAMA_BASE_URL`,
  `DEFAULT_PROVIDER`; `SCORING_COST_WEIGHT` is a deliberate placeholder.
- **`relay.py`** — `Relay` facade constructs and wires every service in
  dependency order, optionally opens persistence, then loads providers.
  Module-level singleton `relay = Relay()` **performs synchronous model
  discovery network calls at import time** (30 s timeout per provider)
  when a provider is enabled. `_load_providers()` **swallows factory
  exceptions silently** — a failed provider simply never registers.

### 3.2 Providers (`app/providers/`)

- **`base.py`** — `Provider` is a **plain mutable dataclass** (name,
  base_url, api_key, enabled, priority, requires_api_key, proxy, models,
  priority_models), not an abstract interface. Plus `ModelProbe` and
  `apply_model_priority()`.
- **`exceptions.py`** — `ProviderError` / `ProviderTimeout` /
  `ProviderHTTPError(status, message, retry_after)`.
- **`openai_compat_client.py`** (825 lines) — the real client: sync
  `httpx` chat / chat_messages / streaming / list_models / probe_model.
  Hosts proxy-selection matrix, `Retry-After` parsing, and untrusted-body
  sanitization (`_safe_provider_body` — strips keys, control chars,
  truncates to 200 chars). **All three providers inherit from it**; the
  per-provider client files (`nvidia_client.py`, `openai_client.py`,
  `lmstudio_client.py`) only set a display name.
- **Factories** (`nvidia.py`, `openai.py`, `lmstudio.py`) — build
  `Provider` instances from settings and discover models (key-gated;
  failures → empty model list, provider still registers).
- Capability detection (`detect_capability`) lives in
  `app/services/capabilities.py`, not providers.

**Provider abstraction is name-string-keyed**: `"NVIDIA"`, `"OpenAI"`,
`"LM Studio"` are duplicated as literal keys across factories,
`ClientRegistry`, and `reload._PROVIDER_SPECS`. Adding a provider requires
touching all of them; a mismatch raises `RuntimeError` at request time.

### 3.3 Services (`app/services/`, 30 modules)

| Module | Responsibility |
| --- | --- |
| `provider_manager` | Thread-safe registry; `ranked()` = enabled + (has key or no key required), priority desc |
| `routing` | Task-category (coding/vision/reasoning/general/creative/translation) model preferences |
| `task_classifier` | Deterministic keyword free-text task classification |
| `model_catalog` | Deterministic per-task compatibility scores (seeded profiles) |
| `health_store` | TTL-bounded snapshots + learned degradation (thresholds, sliding window) |
| `health_checker` | Live connectivity + model probes (`ThreadPoolExecutor(12)` fan-out) |
| `health_refresher` | Background daemon thread periodic re-probe |
| `feedback` | FailureKind → degraded/unavailable action policy |
| `telemetry` | Per-(provider,model) EWMA success/latency, bounded failure history |
| `quality` | Metadata-only user ratings (1–5) → EWMA score, confidence ramp |
| `adaptive` | Adaptive EWMA routing state (observability) |
| `scoring` | Pure signal combination into fitness; health band is primary key |
| `signals` | Declarative scoring-signal registry |
| `candidate_builder` | Orders (provider, model) candidates from routing+health+telemetry+quality |
| `decision_engine` | Explicit explainable decision scores (observational; off by default) |
| `explanation` | Human-readable `/decision/explain` rationale |
| `chat_service` | Failover/retry execution across candidates (blocking, `time.sleep`) |
| `failure_classifier` | Exception → FailureKind (timeout/rate_limit/quota/auth/invalid/server/unknown) |
| `state_store` | **SQLite** write-behind copy of learned state (schema v3, see §7) |
| `state_flusher` | Background daemon thread flushing stores → SQLite; final flush on shutdown |
| `reload` | Hot reload of `.env` allowlist with snapshot/rollback |
| `correlation` | Opaque per-request id (header + ephemeral log field; never persisted) |
| `log_service` | JSON-line structured logging (stdout + optional file); metadata only |
| `ops_store` | Bounded in-memory rolling request metadata (diagnostics) |
| `metrics` | Self-contained Prometheus registry (0.0.4 text) |
| `diagnostics` | Read-only, no-probe operational snapshot |
| `client_registry` | Provider-name → client instance map |
| `capabilities` | Model-id → capability classification |
| `memory_contract` | Durable/ephemeral/never classification; negative-test guard |

### 3.4 API layer (`app/api/`) and security

All routes except the public allowlist `{"/", "/health"}` are guarded by a
**global FastAPI dependency** (`require_api_key`) — Bearer or
`X-Relay-API-Key`, constant-time comparison, key read per request (rotation
without restart).

| Method | Path | Public | Purpose |
| --- | --- | --- | --- |
| GET | `/` | yes | status banner |
| GET | `/health` | yes | aggregate liveness (live probes) |
| GET | `/health/deep` | no | per-model health |
| GET | `/providers` | no | registered providers + models |
| GET | `/provider` | no | provider chat would select next |
| GET | `/decision/explain` | no | ranking rationale |
| POST | `/chat` | no | native task-aware chat |
| GET | `/diagnostics` | no | operational snapshot (no probes) |
| GET | `/metrics` | no | Prometheus text |
| POST | `/feedback` | no | metadata-only quality rating |
| POST | `/admin/reload` | no | hot reload from `.env` |
| POST | `/v1/chat/completions` | no | OpenAI-compatible chat (streaming) |
| GET | `/v1/models` | no | OpenAI-compatible model list |
| GET | `/docs`, `/redoc`, `/openapi.json` | no | interactive docs (auth-gated) |

Key behavior: `/v1/*` is a **raw OpenAI passthrough** (verbatim payload,
OpenAI-shaped `{"error": ...}` bodies, streaming passthrough of tool-call
deltas/usage, `data: [DONE]`). `/chat` uses the full candidate-intelligence
stack. Note: `/v1/chat/completions` builds candidates from **all**
providers filtered by model presence — it does **not** go through
`ranked()`/health-aware selection, while `/chat` does.

---

## 4. Request flow

**Non-streaming `/v1/chat/completions`:**
1. Global auth dependency → `MetricsMiddleware` (duration, TTFB, status).
2. Validate `tool_choice`-without-tools (400) and model-availability
   (400 `model_not_found`).
3. Build candidates = providers that declare `req.model`.
4. `chat_across_messages`: walk candidates; per attempt classify failure;
   skip provider on auth/quota; retry only RETRYABLE kinds up to
   `MAX_RETRIES` (with optional Retry-After/backoff/budget); `time.sleep`
   between retries.
5. On success return provider response; record telemetry/health per
   attempt; on total failure 502 `provider_error`.
6. `X-Relay-Correlation-Id` on every response; ops/metrics recorded.

**Streaming:** same candidate walk but pull only the first chunk; stream
start failures fail over to the next candidate (no retry); once started,
SSE passthrough with a `stream_error` chunk + `data: [DONE]` on mid-stream
failure; telemetry/health recorded in `finally`.

**`/chat`:** task resolution (explicit or classification) → `relay.chat()`
→ ranked providers → `candidate_builder.build()` → decision-engine audit
(if enabled) → failover execution → request logging + telemetry/health.

---

## 5. Configuration surface

~80 env vars in groups: request handling/retries, logging, provider
toggles + keys + priorities, local providers (LM Studio validated URL;
Ollama parsed but unused), proxy, API auth, task routing + classification
+ catalog, health system + feedback tuning, scoring weights/refs, adaptive
routing, quality feedback, decision engine, telemetry, persistence.

Validation is eager and fail-fast with clear messages. Strict booleans
(`"1"`/`"yes"` are false). `reload.py` re-reads `.env` and applies only an
allowlist of reloadable fields (secrets reported by name only; rollback on
failure).

---

## 6. Concurrency model

- **100% synchronous service/provider layer.** Blocking `httpx`, locks
  (`RLock`/`Lock`), `time.monotonic`.
- API handlers are sync `def` (FastAPI threadpool); only docs endpoints +
  the ASGI middleware are async.
- Background work on daemon threads: `HealthRefresher`, `StateFlusher`.
- `HealthChecker` fans out model probes via a fresh `ThreadPoolExecutor(12)`
  per check.
- Streaming occupies a worker thread for the stream's lifetime.
- **Single-process, single-writer SQLite** is a hard constraint (documented;
  multi-worker corruption risk).

---

## 7. Persistence (SQLite schema v3)

Tables: `learned_state` (provider health + counts JSON), `telemetry`
(per-pair aggregates + EWMA), `telemetry_failures` (bounded history),
`quality_aggregates` (EWMA score + category counts), `decision_stats`
(single row). WAL mode, busy timeout, corrupt-DB backup, additive
migrations, retention pruning. **Never persists prompts, responses, keys,
correlation ids** (enforced by `memory_contract.py` tests).

---

## 8. Observability

- **`/metrics`**: hand-rolled Prometheus text — HTTP, chat, provider,
  routing, auth, persistence, process metrics. Labels from **bounded fixed
  sets**; unmatched routes bucketed `"unmatched"`.
- **`/diagnostics`**: read-only snapshot (providers, learned health,
  telemetry, operations, scoring, adaptive, quality, persistence). Never
  probes, never leaks secrets.
- **Logs**: JSON lines to stdout (+ optional file); metadata only —
  provider, model, attempts, latencies, failure kinds, correlation id.
  Never prompts/responses/keys.
- **Ops store**: bounded in-memory rolling request metadata with p50/p95
  latency summaries.

---

## 9. Testing

- 48 pytest files + `conformance_helpers.py` (scriptable fake OpenAI
  upstream) + `run_live_smoke.py` (opt-in, not collected).
- `conftest.py` **globally disables provider loading** so the import-time
  singleton never hits the network.
- Coverage areas: routing/scoring/adaptive/quality/decision, health,
  persistence + migrations, OpenAI wire conformance + SDK compat, retry
  hardening, proxy matrix, auth, metrics, diagnostics, reload, memory
  contract, full-stack production profile, RC validation.
- No CI config, no pytest.ini/pyproject.

---

## 10. Packaging and CLI

- **No packaging.** No `pyproject.toml`/`setup.py`/console scripts.
- CLI: `argparse`, single `setup` subcommand; interactive provider config
  (keys masked, model picker, task routing, provider probe) writing to
  `.env` via `python-dotenv`. No Rich/Textual/Click/Typer anywhere.
- Entry points: `python -m uvicorn app.main:app`, `python -m app.cli`.

---

## 11. Known gaps and risks relevant to platform transformation

1. **No packaging / no console script** — cannot `pip install relay` or run
   a `relay` command.
2. **Minimal CLI** — one subcommand; no server management, status, logs,
   key generation, config listing/editing.
3. **No UI** — everything is API/`curl`/`.env`. No dashboard, no setup
   wizard, no usage views.
4. **Import-time network I/O** — the `relay` singleton does model
   discovery at import (slow startup, blocks when offline).
5. **Silent provider-loading failures** — `_load_providers` swallows
   exceptions; a bad key yields a provider with zero models and no warning
   (a documented first-time-user trap).
6. **Only 3 providers**; Anthropic/OpenRouter/Groq/Ollama keys parsed but
   unused. Name-string coupling makes adding providers brittle.
7. **`/v1` bypasses the routing intelligence stack** (raw passthrough
   differs from `/chat` selection).
8. **Unused settings and dead code paths** (default_provider, cost signal,
   `ChatService.chat()`, `RoutingEngine.resolve()`, `HealthStore.freshness()`,
   `check_model`, `FeedbackAction.clear`).
9. **Single-process/single-writer persistence** — an architectural
   constraint for any future multi-process deployment.
10. **No web console, no persistent request history, no usage/billing
    tracking, no multi-tenant key issuance** — everything a "gateway
    platform" typically adds is absent.
11. **Hard-coded timeouts** (list_models 30 s, probe 10 s) and per-check
    `ThreadPoolExecutor` churn.
12. **Auth is a single shared key** (global dependency) — no per-user/per-
    app keys, no scopes, no rotation UI.

---

## 12. What exists vs. what a gateway platform adds

| Capability | Exists today | Notes |
| --- | --- | --- |
| OpenAI-compatible API | ✅ strong | conformance + SDK tests |
| Routing/failover/health/learning | ✅ strong | the core differentiator |
| Streaming + tools passthrough | ✅ | /v1 |
| Prometheus metrics + diagnostics | ✅ | bounded labels, privacy-safe |
| Single shared API key | ✅ basic | one key, header-based |
| SQLite persistence of learned state | ✅ | schema v3, migrations |
| Interactive CLI setup | ⚠️ minimal | argparse, one command |
| Packaging (pip / console script) | ❌ | none |
| Rich/Textual user experience | ❌ | none |
| Web console / dashboard | ❌ | none |
| Per-app API keys + scopes | ❌ | single global key |
| Request history / usage analytics | ❌ | in-memory ops window only |
| Additional providers | ❌ | 3 wired, 5 keys parsed |
| Async-first provider layer | ❌ | fully synchronous |
| Configuration UI / wizard | ❌ | .env only |

---

*Prepared as deliverable 1 of the Phase 9 analysis. No code was modified.*
