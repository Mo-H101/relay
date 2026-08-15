# Relay Configuration

Relay reads configuration from environment variables, loaded from a
`.env` file if present. The file is resolved from `RELAY_ENV_FILE`, then
the current working directory, then the project root — **for source
checkouts**. **Installed packages always use their per-user data
directory** (see `RELAY_DATA_DIR` below): `%LOCALAPPDATA%\relay\.env` on
Windows, `~/.local/share/relay/.env` on Linux, and
`~/Library/Application Support/relay/.env` on macOS. Values are validated
at startup; invalid values abort startup with a clear message. Hot reload
(`POST /admin/reload`) applies only the fields marked **reloadable** below.

## Terminal interface and startup

The `relay` command dispatches by configuration state:

| Command | Behavior |
| --- | --- |
| `relay` (no configuration) | Runs the interactive setup wizard. |
| `relay` (configured) | Runs the terminal interface (TUI) with an embedded API server bound to `RELAY_HOST`/`RELAY_PORT`. |
| `relay tui` | Forces the terminal interface. |
| `relay serve` | Runs the API server only; no terminal interface. |
| `relay setup` | (Re)runs the setup wizard and, on success, hands off to the terminal interface. |

The TUI requires an interactive terminal. On non-interactive launch
(stdin/stdout redirected, scheduled tasks, services) it prints guidance
and exits cleanly instead of crashing — run `relay serve` for headless
operation. On Windows the TUI runs inside Windows Terminal (ConPTY),
PowerShell, VS Code, or a conhost console; environments without a console
handle are detected and degrade to the same guidance. See
[docs/tui-guide.md](tui-guide.md) for the full user guide.

## Server and installation state

| Variable | Default | Meaning |
| --- | --- | --- |
| `RELAY_HOST` | `127.0.0.1` | Host the `relay` command binds the server to. |
| `RELAY_PORT` | `8000` | Port the `relay` command binds the server to. |
| `RELAY_TUI_NO_EMBED` | `false` | When true, `relay`/`relay tui` runs the terminal interface without starting an embedded API server (for setups managed by a service manager). |
| `RELAY_ENV_FILE` | *(resolved)* | Explicit path to the `.env` file. |
| `RELAY_STATE_DIR` | `<env dir>/.relay` | Directory holding setup state (`state.json`) and the `platform.db` database. |
| `RELAY_DATA_DIR` | *(user data dir)* | Installed installs: base per-user data directory for `.env`, `state_dir`, and the default `platform.db`. Defaults to `platformdirs.user_data_dir("relay")` — e.g. `~/.local/share/relay` on Linux, `%LOCALAPPDATA%\relay` on Windows. Read at startup. |

Setup state distinguishes "installed but not configured", "setup completed",
and "incomplete/failed setup"; it is independent of whether a `.env` exists.

## General

| Variable | Default | Reloadable | Meaning |
| --- | --- | --- | --- |
| `REQUEST_TIMEOUT` | `120` | yes | Seconds before a provider request times out (min 1). |
| `MAX_RETRIES` | `1` | yes | Retries per (provider, model) before failing over. |
| `RETRY_HONOR_RETRY_AFTER` | `false` | yes | Sleep for the provider's `Retry-After` before the next retry of the same candidate (capped at `RETRY_AFTER_MAX_SECONDS`). Off = immediate retry. |
| `RETRY_AFTER_MAX_SECONDS` | `60` | yes | Cap for an honored `Retry-After`, in seconds (min 0). |
| `RETRY_BACKOFF_BASE_SECONDS` | `0` | yes | Exponential backoff base between retries, in seconds. 0 = no backoff (immediate retry). |
| `RETRY_BACKOFF_MAX_SECONDS` | `60` | yes | Cap for exponential backoff, in seconds (min 0). |
| `REQUEST_TIMEOUT_BUDGET_SECONDS` | `0` | yes | Total wall-clock budget for a chat request, in seconds. 0 = no budget. Retries and backoff waits never exceed the remaining budget. |

## Logging

| Variable | Default | Reloadable | Meaning |
| --- | --- | --- | --- |
| `LOG_LEVEL` | `INFO` | no | Logging level. |
| `LOG_FILE` | *(empty)* | no | Optional log file path; empty logs to stdout. |

## Providers

| Variable | Default | Reloadable | Meaning |
| --- | --- | --- | --- |
| `NVIDIA_ENABLED` | `true` | yes | Enable the NVIDIA NIM endpoint. |
| `OPENAI_ENABLED` | `false` | yes | Enable the OpenAI endpoint. |
| `ANTHROPIC_ENABLED` | `false` | yes | Enable the Anthropic Messages endpoint. |
| `GEMINI_ENABLED` | `false` | yes | Enable the Google Gemini endpoint. |
| `OLLAMA_ENABLED` | `false` | yes | Enable the local Ollama endpoint. |
| `LMSTUDIO_ENABLED` | `false` | yes | Enable the local LM Studio endpoint. |
| `NVIDIA_MODEL_PRIORITY` | *(empty)* | yes | Comma-separated model ids to prefer, in order. |
| `OPENAI_MODEL_PRIORITY` | *(empty)* | yes | Comma-separated model ids to prefer, in order. |
| `ANTHROPIC_MODEL_PRIORITY` | *(empty)* | yes | Comma-separated Anthropic model ids to prefer, in order. |
| `GEMINI_MODEL_PRIORITY` | *(empty)* | yes | Comma-separated Gemini model ids to prefer, in order. |
| `OLLAMA_MODEL_PRIORITY` | *(empty)* | yes | Comma-separated Ollama model ids to prefer, in order. |
| `LMSTUDIO_MODEL_PRIORITY` | *(empty)* | yes | Comma-separated model ids to prefer, in order. |

### API keys

| Variable | Meaning |
| --- | --- |
| `NVIDIA_API_KEY` | NVIDIA key (reloadable, secret). |
| `OPENAI_API_KEY` | OpenAI key (reloadable, secret). |
| `ANTHROPIC_API_KEY` | Anthropic key (reloadable, secret). |
| `GEMINI_API_KEY` | Gemini key (reloadable, secret). |
| `LMSTUDIO_API_KEY` | Key for keyed local servers; usually empty (reloadable, secret). |

OpenRouter and Groq are **not** supported in v1.0.0 (decision D1); their
reserved keys were removed and no `OPENROUTER_*`/`GROQ_*` variable is
parsed. `.env` files that still contain them are unaffected (unknown
variables are ignored).

> **Deprecated** (P5): the cloud provider-key env vars (`NVIDIA_API_KEY`,
> `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`) are still
> honored as the runtime fallback and for installs that never enable the
> keyring, but the tools no longer write them when `RELAY_KEYRING=true`
> and the migrate command removes them from `.env`. Prefer the OS keyring
> (below); the env vars remain the fallback for the foreseeable future
> (removal is deferred beyond P6).

### Key storage and credential precedence

| Variable | Default | Meaning |
| --- | --- | --- |
| `RELAY_KEYRING` | `false` | When true, provider keys resolve keyring-first and config writes for `api_key` go to the OS keyring instead of `.env`. Read at startup (not reloadable). |
| `RELAY_KEYRING_BACKEND` | *(keyring default)* | Dotted `module.Class` path overriding the keyring backend (needed for headless servers). Read per call, so changes apply without restart. |
| `RELAY_AUTH_STORE` | `false` | When true, the auth dependency also accepts keys stored in `platform.db` (scrypt-hashed) with scope enforcement. Read per request. |

Resolution order is fixed:

1. **Bootstrap `RELAY_API_KEY` wins** over every store-backed key when
   both are set (Phase 4 "bootstrap always wins" contract).
2. **Provider keys**: keyring-stored key first when `RELAY_KEYRING=true`
   and an entry exists; otherwise the `.env`/settings value; otherwise
   empty. A keyring outage degrades to the `.env` fallback.
3. Store-backed Relay keys require `RELAY_AUTH_STORE=true`; a store
   outage fails closed (`401`).

`platform.db` (at `state_dir/platform.db`) stores only scrypt hashes of
Relay keys (raw keys are shown once at creation and never persisted), the
consolidated learned state, and the security event log. The `.env` file is
written user-only (`0600`) on POSIX. See [security.md](security.md) for
the full model and lifecycle.

The legacy stores (`relay_keys.db` and `relay_state.db` at the project root
or in `state_dir`) are **read-only migration sources**: `relay migrate`
imports them into `platform.db` and never deletes them (rollback contract),
so the startup guard that blocks creating a fresh `platform.db` over
unmigrated legacy data is permanent for the supported lifetime of pre-P6.1
layouts.

### Local providers

| Variable | Default | Reloadable | Meaning |
| --- | --- | --- | --- |
| `LMSTUDIO_BASE_URL` | `http://localhost:1234/v1` | no | Base URL of the OpenAI-compatible local server. |
| `LMSTUDIO_PRIORITY` | `1` | no | Priority of the LM Studio provider. |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | no | Base URL of the local Ollama endpoint. |

The preferred provider is resolved at runtime from provider priority order,
not from configuration.

## API authentication

| Variable | Default | Reloadable | Meaning |
| --- | --- | --- | --- |
| `RELAY_API_KEY` | *(empty)* | yes | When set, every route except `/` and `/health` requires this key via `Authorization: Bearer <key>` or `X-Relay-API-Key: <key>`. |

With `RELAY_AUTH_STORE=true`, per-client keys can be managed from the CLI
or admin API: `relay keys add|list|remove|rotate|prune` and
`GET/POST /admin/keys`. The `relay events` CLI and `GET /admin/events`
tail the security event log (schema v5) written to `state_dir/platform.db`;
rows are redacted at write time. See [security.md](security.md) for the
rotation/prune runbooks and event-log contract.

## Task routing and classification

| Variable | Default | Reloadable | Meaning |
| --- | --- | --- | --- |
| `TASK_ROUTING_ENABLED` | `false` | yes | Route by task category using the `TASK_*` model lists. |
| `CROSS_PROVIDER_MODEL_SELECTION` | `false` | yes | Allow cross-provider model selection for task routing. |
| `TASK_CODING`, `TASK_VISION`, `TASK_REASONING`, `TASK_GENERAL`, `TASK_CREATIVE`, `TASK_TRANSLATION` | *(empty)* | yes | Comma-separated model ids preferred for each task. |
| `TASK_CLASSIFICATION_ENABLED` | `false` | yes | Classify free-text `/chat` messages into a task. |
| `TASK_CLASSIFICATION_THRESHOLD` | `0.6` | yes | Minimum confidence to accept a classification. |

## Health-aware routing

| Variable | Default | Reloadable | Meaning |
| --- | --- | --- | --- |
| `HEALTH_AWARE_ROUTING` | `false` | yes | Skip unhealthy providers/models during routing. |
| `HEALTH_FEEDBACK_ENABLED` | `false` | yes | Feed real request outcomes into the health store. |
| `HEALTH_TTL_SECONDS` | `300` | yes | Time a healthy state is trusted before re-check. |
| `HEALTH_DEGRADED_TTL_SECONDS` | `60` | yes | Time a degraded mark is trusted. |
| `HEALTH_UNAVAILABLE_TTL_SECONDS` | `900` | yes | Time an unavailable mark is trusted. |
| `HEALTH_REFRESH_ENABLED` | `false` | no | Periodically re-probe providers in the background. |
| `HEALTH_REFRESH_INTERVAL_SECONDS` | `300` | no | Background probe interval (min 1). |
| `HEALTH_DEEP_REFRESH_ENABLED` | `false` | no | Probe every chat model on background refresh. |

### Health feedback tuning

| Variable | Default | Reloadable | Meaning |
| --- | --- | --- | --- |
| `HEALTH_FEEDBACK_MODEL_SERVER_ERROR_THRESHOLD` | `1` | yes | 5xx failures before a model is marked unavailable. |
| `HEALTH_FEEDBACK_PROVIDER_SERVER_ERROR_THRESHOLD` | `3` | yes | 5xx failures before a provider is marked unavailable. |
| `HEALTH_FEEDBACK_MODEL_TIMEOUT_DEGRADED_THRESHOLD` | `2` | yes | Timeouts before a model is degraded. |
| `HEALTH_FEEDBACK_MODEL_TIMEOUT_UNAVAILABLE_THRESHOLD` | `5` | yes | Timeouts before a model is unavailable. |
| `HEALTH_FEEDBACK_MODEL_INVALID_REQUEST_UNAVAILABLE_THRESHOLD` | `3` | yes | 4xx invalid-request failures before unavailable. |
| `HEALTH_FEEDBACK_MODEL_UNKNOWN_DEGRADED_THRESHOLD` | `3` | yes | Unclassified failures before degraded. |
| `HEALTH_FRESHNESS_EXPONENT` | `1.0` | yes | Exponent dampening stale state in scoring. |

## Scoring

| Variable | Default | Reloadable | Meaning |
| --- | --- | --- | --- |
| `SCORING_PRIORITY_WEIGHT` | `1.0` | yes | Weight of configured priority. |
| `SCORING_SUCCESS_WEIGHT` | `1.0` | yes | Weight of telemetry success rate. |
| `SCORING_LATENCY_WEIGHT` | `1.0` | yes | Weight of telemetry latency. |
| `SCORING_FAILURE_WEIGHT` | `1.0` | yes | Weight of recent failures. |
| `SCORING_PREFERENCE_WEIGHT` | `1.0` | yes | Weight of task/model preference. |
| `SCORING_PRIORITY_DENOM` | `10` | yes | Priority denominator (must be > 0). |
| `SCORING_LATENCY_REF_MS` | `250` | yes | Reference latency for normalization (must be > 0). |
| `SCORING_FAILURE_REF_COUNT` | `5` | yes | Reference failure count for normalization (min 1). |
| `SCORING_COST_WEIGHT` | `0.0` | yes | Cost weight placeholder; zero keeps legacy ordering. |

## Task capability catalog

| Variable | Default | Reloadable | Meaning |
| --- | --- | --- | --- |
| `TASK_CATALOG_ENABLED` | `false` | yes | Add a task-compatibility signal to within-band scoring. |
| `SCORING_TASK_COMPATIBILITY_WEIGHT` | `1.0` | yes | Weight of the task-compatibility signal. |

## Adaptive routing

| Variable | Default | Reloadable | Meaning |
| --- | --- | --- | --- |
| `ADAPTIVE_ROUTING_ENABLED` | `false` | yes | Learn EWMA reliability/latency and reorder within health bands. |
| `ADAPTIVE_MIN_SAMPLES` | `10` | yes | Minimum observations before an EWMA signal is trusted. |
| `ADAPTIVE_LEARNING_RATE` | `0.1` | yes | EWMA learning rate (>= 0). Values above 1.0 are accepted at startup but clamped by the store. |
| `ADAPTIVE_LATENCY_WEIGHT` | `1.0` | yes | Weight of the EWMA latency signal. |
| `ADAPTIVE_RELIABILITY_WEIGHT` | `1.0` | yes | Weight of the EWMA reliability signal. |

## Quality feedback

| Variable | Default | Reloadable | Meaning |
| --- | --- | --- | --- |
| `QUALITY_FEEDBACK_ENABLED` | `false` | yes | Learn from `/feedback` ratings within health bands. |
| `QUALITY_FEEDBACK_MIN_SAMPLES` | `10` | yes | Minimum ratings before a quality estimate is trusted. |
| `QUALITY_FEEDBACK_LEARNING_RATE` | `0.1` | yes | EWMA learning rate for quality in `[0, 1]`. |
| `QUALITY_FEEDBACK_RETENTION_LIMIT` | `10000` | yes | Cap on distinct (provider, model) aggregates (min 1). |
| `QUALITY_FEEDBACK_WEIGHT` | `1.0` | yes | Weight of the within-band quality signal. |

## Decision engine

| Variable | Default | Reloadable | Meaning |
| --- | --- | --- | --- |
| `DECISION_ENGINE_ENABLED` | `false` | yes | Route selection through the explainable decision engine. |
| `DECISION_EXPLANATIONS_ENABLED` | `false` | yes | Serve `GET /decision/explain`. |

## Telemetry

| Variable | Default | Reloadable | Meaning |
| --- | --- | --- | --- |
| `TELEMETRY_ENABLED` | `false` | yes | Record per-attempt telemetry (success, latency, failure type). |
| `TELEMETRY_MAX_FAILURE_HISTORY` | `50` | no | Recent failures kept per (provider, model). |

## Persistence

| Variable | Default | Reloadable | Meaning |
| --- | --- | --- | --- |
| `PERSISTENCE_ENABLED` | `false` | no | Persist learned state to SQLite. |
| `PERSISTENCE_PATH` | `state_dir/platform.db` | no | SQLite database path. |
| `PERSISTENCE_FLUSH_INTERVAL_SECONDS` | `60` | no | Write-behind flush interval (min 1). |
| `PERSISTENCE_RETENTION_DAYS` | `0` | yes | Retention in days for persisted failure history and the security event log (`events` table); `0` disables pruning. |
| `REQUEST_LOG_FLUSH_INTERVAL_SECONDS` | `5` | no | Write-behind flush cadence for the durable request log (min 1). |
| `REQUEST_LOG_RETENTION_DAYS` | `30` | yes | Retention in days for `request_log` rows; `0` disables pruning. |

## Project continuity (opt-in, P9)

Project continuity is **disabled by default** (`CONTINUITY_ENABLED=false`).
When enabled, opt-in conversations (`X-Relay-Conversation-Id` +
`X-Relay-Project-Id` headers) are tracked durably in `platform.db` so a
client can resume a conversation after a provider switch or a Relay
restart without re-executing acknowledged work. All continuity settings
are restart-required (none are hot-reloadable). Only metadata and derived
state are stored — raw prompts and responses are never persisted.

| Variable | Default | Reloadable | Meaning |
| --- | --- | --- | --- |
| `CONTINUITY_ENABLED` | `false` | no | Enable durable conversation tracking and the resume protocol. |
| `CONTINUITY_CONTENT_CONTEXT_ENABLED` | `false` | no | Opt-in content-aware handoff (P9f): derive a redacted, bounded content summary from the in-request messages and compact an over-budget array before forwarding. Ephemeral only — never persisted, logged, exported, or surfaced in metrics/events. Requires `CONTINUITY_ENABLED=true`. |
| `CONTINUITY_RETENTION_DAYS` | `30` | no | Retention in days for conversations, turns, summaries, compactions, and replay rows; `0` disables pruning. Active conversations are never pruned. |
| `CONTINUITY_FLUSH_INTERVAL_SECONDS` | `5` | no | Write-behind flush cadence for continuity rows (min 1). |
| `CONTINUITY_CONTEXT_TOKEN_BUDGET` | `32768` | no | Token budget used by the context manager when building the next-turn envelope. |
| `CONTINUITY_OUTPUT_RESERVE_TOKENS` | `2048` | no | Tokens reserved for model output when budgeting context. |
| `CONTINUITY_SUMMARY_SHARE` | `0.4` | no | Fraction of the context budget reserved for summaries when compacting. |
| `CONTINUITY_SUMMARY_MAX_CHARS` | `4096` | no | Maximum characters in a derived, redacted summary. |
| `CONTINUITY_TAIL_MAX_ITEMS` | `20` | no | Recent turns kept verbatim (as envelope data) after compaction. |
| `CONTINUITY_CHARS_PER_TOKEN` | `4` | no | Chars-per-token estimate used for budget math. |
| `CONTINUITY_SUMMARIZER_MODEL` | *(empty)* | no | Model id for optional LLM summaries; empty = extractive only (no summarizer calls). |
| `MAX_SWITCHES_PER_TURN` | `3` | no | Cap on provider switches within a single turn (a storm guard). |
| `MAX_SWITCHES_PER_WINDOW` | `5` | no | Cap on provider switches within the switch-window. |
| `MAX_RESUME_REPLAYS` | `3` | no | Durable cap on replay attempts per resume token; exhausted → the resume path denies. |

The resume protocol: an opt-in client sends
`X-Relay-Conversation-Id` / `X-Relay-Project-Id` and receives a
single-use resume token; after an interruption it resumes with that token
and Relay replays the durable, per-conversation sequence (each server
`seq` advances monotonically, so acknowledged work is never re-executed).
Replay attempts are tracked durably (`resume_replays`, schema v8) so the
cap survives restarts, and the resume path fails closed if the tracker
cannot be persisted. See [deployment.md](deployment.md),
[platform-db-schema.md](platform-db-schema.md), and the client-facing
[project continuity guide](clients/continuity.md) for the wire
contract and storage model.

## Recommended production profile

All intelligence flags default to `false`, so an unmodified deployment is
byte-identical to a plain priority + failover router. Enable the systems in
dependency order when you want Relay to learn:

1. **Telemetry** (`TELEMETRY_ENABLED=true`) — records success/latency/failure
   per attempt. Nothing else can learn without it.
2. **Health feedback** (`HEALTH_FEEDBACK_ENABLED=true`) — converts real chat
   failures into provider/model health state; combined with
   `HEALTH_AWARE_ROUTING=true`, degraded/unavailable models are excluded from
   routing. `HEALTH_FEEDBACK_MODEL_TIMEOUT_DEGRADED_THRESHOLD` (2 timeouts),
   `HEALTH_FEEDBACK_MODEL_TIMEOUT_UNAVAILABLE_THRESHOLD` (5 timeouts), and
   `HEALTH_FEEDBACK_MODEL_UNKNOWN_DEGRADED_THRESHOLD` (3) set the degradation
   triggers.
3. **Adaptive routing** (`ADAPTIVE_ROUTING_ENABLED=true`) — reorders candidates
   within a health band by EWMA reliability/latency once a (provider, model)
   pair has `ADAPTIVE_MIN_SAMPLES` (10) observations. Below that, adaptive
   signals resolve to neutral and do not change ordering.
4. **Quality feedback** (`QUALITY_FEEDBACK_ENABLED=true`) — blends `/feedback`
   ratings into within-band scoring once `QUALITY_FEEDBACK_MIN_SAMPLES` (10)
   ratings exist. Ratings are metadata-only and never capture message content.
5. **Decision engine** (`DECISION_ENGINE_ENABLED=true` and optionally
   `DECISION_EXPLANATIONS_ENABLED=true`) — additive selection audit served via
   `/decision/explain`. It never changes the routed result.
6. **Persistence** (`PERSISTENCE_ENABLED=true`) — keeps all learned state
   across restarts. Recommended once any of the above is on; without it,
   learned state is lost on process exit.

The full recommended profile, with every non-default threshold left at its
default:

```dotenv
TELEMETRY_ENABLED=true
HEALTH_FEEDBACK_ENABLED=true
HEALTH_AWARE_ROUTING=true
ADAPTIVE_ROUTING_ENABLED=true
QUALITY_FEEDBACK_ENABLED=true
DECISION_ENGINE_ENABLED=true
DECISION_EXPLANATIONS_ENABLED=true
PERSISTENCE_ENABLED=true
PERSISTENCE_PATH=/var/lib/relay/platform.db
```

`HEALTH_REFRESH_ENABLED` and `HEALTH_DEEP_REFRESH_ENABLED` are intentionally
not part of the profile: the background prober requires live network access
to every provider endpoint, which is not appropriate for all deployments.

## Observability

| Variable | Default | Reloadable | Meaning |
| --- | --- | --- | --- |
| `OPS_WINDOW_SECONDS` | `300` | yes | Rolling operations window for diagnostics; `0` disables age pruning. |
| `OPS_MAX_EVENTS` | `10000` | yes | Cap on in-memory request metadata events (min 1). |

## Proxy

| Variable | Default | Reloadable | Meaning |
| --- | --- | --- | --- |
| `PROXY_ENABLED` | `true` | yes | Honor outbound proxy configuration. |
| `HTTP_PROXY` | *(env)* | yes | HTTP proxy URL. |
| `HTTPS_PROXY` | *(env)* | yes | HTTPS proxy URL. |
| `NO_PROXY` | *(env)* | yes | Comma-separated hosts/domains to bypass the proxy (`*` = all). |

Proxy URLs and credentials are configuration only; they are never logged
or included in metrics or errors. Per-provider override is also possible
via the `Provider.proxy` field in code.
