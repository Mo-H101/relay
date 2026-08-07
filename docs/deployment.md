# Deployment

## Cloud gateway configuration (NVIDIA + OpenAI)

Relay runs as an OpenAI-compatible cloud gateway: clients point their
`base_url` at Relay's `/v1` and keep their existing OpenAI SDK/tooling.
Provider credentials, enabled flags, and model priority live in `.env`
(see [configuration.md](configuration.md)). The `.env` file is
`%LOCALAPPDATA%\relay\.env` on Windows for an installed package, or
`project-root/.env` for a source checkout (see
[README Configuration](../README.md#configuration)).

```bash
# Cloud providers
NVIDIA_ENABLED=true
OPENAI_ENABLED=true

# API keys
NVIDIA_API_KEY=nvapi-...
OPENAI_API_KEY=sk-proj-...

# Ranked model priority (first model is the default target)
NVIDIA_MODEL_PRIORITY=deepseek-ai/deepseek-r1,meta/llama-3.3-70b-instruct
OPENAI_MODEL_PRIORITY=gpt-4o-mini,gpt-4o
```

Client wiring:

```bash
# OpenAI SDK / Cline / OpenCode / etc.
base_url=http://relay-host:8000/v1
api_key=<RELAY_API_KEY>        # required when auth is on
```

Step-by-step setup for Cline, OpenCode, Continue, and any other
OpenAI-compatible client (including per-app keys): [client setup
guides](clients/index.md).

The `/v1` surface forwards `messages`, `tools`, `tool_choice`,
`stream`, and `stream_options` verbatim, streams with a stable relay
generated `id`, and returns OpenAI-shaped errors
(`{"error":{...}}`) so SDK clients parse them natively. Validate a
deployment with the offline RC suite and the live smoke:

```bash
python -m pytest tests/test_rc_validation.py -q
python tests/run_live_smoke.py                 # uses live .env keys
```

See [release-candidate-checklist.md](release-candidate-checklist.md) for
the full validation matrix and current results.

### Recommended production profile

```bash
RELAY_API_KEY=<long-random-value>
PERSISTENCE_ENABLED=true
PERSISTENCE_PATH=/var/lib/relay/platform.db
HEALTH_FEEDBACK_ENABLED=true
TELEMETRY_ENABLED=true
HEALTH_AWARE_ROUTING=true
# Optional: background provider probing. Needs live network access to
# every provider and incurs probe cost on the configured interval.
# HEALTH_REFRESH_ENABLED=true

# Optional retry hardening (defaults keep retries immediate):
# RETRY_HONOR_RETRY_AFTER=true       # sleep for the provider's Retry-After
# RETRY_AFTER_MAX_SECONDS=60         # cap for an honored Retry-After
# RETRY_BACKOFF_BASE_SECONDS=0       # exponential backoff base (0 = none)
# REQUEST_TIMEOUT_BUDGET_SECONDS=0   # total wall-clock budget (0 = none)
```

### Continuity (opt-in) profile

The full hardened profile for a continuity-enabled deployment (superset of
the block above). **Run exactly one Relay process** (single-writer SQLite,
see below); **enable continuity only when intended** — it is additive
(headers, SSE `relay:*` events, new tables) and older clients ignore it.

```dotenv
# --- Identity & transport ---
RELAY_API_KEY=<long-random-value>
RELAY_AUTH_STORE=true                  # per-client scoped keys (D6)
RELAY_HOST=0.0.0.0                     # TLS at the reverse proxy

# --- Persistence (single SQLite file) ---
PERSISTENCE_ENABLED=true
PERSISTENCE_PATH=/var/lib/relay/platform.db
PERSISTENCE_FLUSH_INTERVAL_SECONDS=60
PERSISTENCE_RETENTION_DAYS=30

# --- Routing & learning ---
HEALTH_FEEDBACK_ENABLED=true
TELEMETRY_ENABLED=true
HEALTH_AWARE_ROUTING=true
HEALTH_REFRESH_ENABLED=true
HEALTH_REFRESH_INTERVAL_SECONDS=60

# --- Providers (only the six supported) ---
NVIDIA_ENABLED=true
OPENAI_ENABLED=true
NVIDIA_MODEL_PRIORITY=<account-invocable-ids>    # D4: pin at deploy time
OPENAI_MODEL_PRIORITY=<account-invocable-ids>    # D4: pin at deploy time

# --- Provider keys ---
RELAY_KEYRING=true
# RELAY_KEYRING_BACKEND=<dotted.module.Class>    # headless servers

# --- Retry hardening (D3) ---
RETRY_HONOR_RETRY_AFTER=true
RETRY_AFTER_MAX_SECONDS=60
RETRY_BACKOFF_BASE_SECONDS=1
REQUEST_TIMEOUT_BUDGET_SECONDS=120

# --- Continuity (opt-in) ---
CONTINUITY_ENABLED=true
CONTINUITY_RETENTION_DAYS=30
CONTINUITY_FLUSH_INTERVAL_SECONDS=5
CONTINUITY_CONTEXT_TOKEN_BUDGET=32768
CONTINUITY_OUTPUT_RESERVE_TOKENS=2048
CONTINUITY_SUMMARY_SHARE=0.4
CONTINUITY_SUMMARY_MAX_CHARS=4096
CONTINUITY_TAIL_MAX_ITEMS=20
CONTINUITY_CHARS_PER_TOKEN=4
# CONTINUITY_SUMMARIZER_MODEL=<model-id>         # empty = extractive only
MAX_SWITCHES_PER_TURN=3
MAX_SWITCHES_PER_WINDOW=5
MAX_RESUME_REPLAYS=3
```

If the optional LLM summarizer is desired, set
`CONTINUITY_SUMMARIZER_MODEL` to a model id the account can invoke and add
it to a priority list; any failure degrades to the extractive path.

### Supported storage model

| Aspect | Model |
| --- | --- |
| Database | Single SQLite file `platform.db` (schema **v8**), WAL + `busy_timeout 5000`, `0600` + sidecars, corrupt-file backup-aside-and-reopen |
| Writer | Single guarded connection per process; continuity writes only on the `ContinuityFlusher` thread; hot-path reads bounded (single-row `last_turn` + `resume_envelope` hydration) |
| Process model | Single process, single writer; **no horizontal scale** (scale by isolated instances with separate `PERSISTENCE_PATH`) |
| Backup | `relay migrate` backup/rollback copies `platform.db` whole; operators back up `state_dir/` (db + `-wal`/`-shm` + `.env`) |
| Retention | `CONTINUITY_RETENTION_DAYS` (30) + `PERSISTENCE_RETENTION_DAYS` (30); active conversations are never pruned |
| Migration | Additive v6→v7→v8, idempotent, guarded by `PRAGMA user_version`; newer-version files are refused; `relay migrate --rollback` restores backups |

### Resource requirements

| Resource | Estimate | Notes |
| --- | --- | --- |
| CPU | 1 core nominal | Async I/O (`httpx.AsyncClient`); health refresher probes on an interval; no heavy local compute |
| Memory | ~100–300 MB | FastAPI/uvicorn + in-memory coordinator (≤ 512 states, LRU) + flusher queue (≤ 10,000 rows, bounded) + ops/metrics windows |
| Disk | < 1 GB sustained | Metadata-only rows; growth bounded by retention pruning; `platform.db` + WAL sidecars; `.env` + keyring |
| Network | 2+ provider endpoints | Outbound to provider base URLs; inbound behind a reverse proxy (TLS) |
| OS keyring | Required for at-rest provider secrets | `RELAY_KEYRING_BACKEND` on headless servers; `.env` fallback must be disk-encrypted + ACL'd |

Both `/chat` and `/v1` record **per-attempt** telemetry/health, so failed
attempts inside a request (even ones recovered by retry or failover) feed
the same learning signals. See [configuration.md](configuration.md) for
the retry knobs and [known-limitations.md](known-limitations.md) for the
default-off retry policy and remaining limitations.

## Running the server

```bash
relay serve
```

`relay serve` binds to `RELAY_HOST` / `RELAY_PORT` (defaults
`127.0.0.1:8000`). To bind all interfaces, set `RELAY_HOST=0.0.0.0` before
starting.

**Run exactly one Relay process.** Learned state is stored in SQLite,
which is single-process/single-writer (`app/services/state_store.py`), and
learned health, telemetry, metrics, and the ops window are per-process
in-memory state. Running multiple workers (e.g. `gunicorn -w N` or
`uvicorn --workers N`) gives every process its own writer to the same
database file — a documented corruption risk — plus split-brain routing
and per-process counters. Do not scale Relay horizontally; if you need
capacity, scale by running isolated instances with their own
`PERSISTENCE_PATH` and client routing.

## Production hardening checklist

### 1. Enable API authentication

Set `RELAY_API_KEY` to a long random value. Every route except `/` and
`/health` then requires either:

```
Authorization: Bearer <key>
X-Relay-API-Key: <key>
```

This includes `/docs`, `/redoc`, and `/openapi.json`, which are
registered as real endpoints so they inherit the same guard. When the key
is empty (default), authentication is off — do not run an exposed Relay
with an empty key.

Optionally add store-backed keys (scrypt-hashed in `platform.db`) for
per-client credentials with scope enforcement:

```bash
RELAY_AUTH_STORE=true
relay keys add --label opencode --scopes chat,v1
```

The bootstrap key always wins; see [security.md](security.md) for the
precedence contract.

### 2. Keep provider keys in the OS keyring

On desktops the OS credential store is available automatically. On
headless servers pick an encrypted backend and set `RELAY_KEYRING_BACKEND`
to a dotted `module.Class` path:

```bash
RELAY_KEYRING=true
# RELAY_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring   # example
relay provider keys set nvidia <key>
```

Keys written after `RELAY_KEYRING=true` go to the keyring. To move keys
that still live in `.env`, run the migration command (§ runbook below).
With keyring enabled, runtime resolution is keyring-first with the `.env`
value as fallback.

**At-rest requirement:** Relay has no encryption-at-rest subsystem of its
own. Provider keys are protected at rest only by the OS keyring. If the
keyring is unavailable (for example, a Windows service under a virtual
account, or a headless box without a configured
`RELAY_KEYRING_BACKEND`), provider keys fall back to plaintext `.env` —
treat that file as a secret and protect it with disk encryption and
service-account ACLs. See [security.md](security.md) for the full
"At-rest protection of provider secrets" section and the Windows-specific
behavior notes.

### 3. Enable persistence for learned state

```bash
PERSISTENCE_ENABLED=true
PERSISTENCE_PATH=/var/lib/relay/platform.db   # default: <state_dir>/platform.db
PERSISTENCE_FLUSH_INTERVAL_SECONDS=60
PERSISTENCE_RETENTION_DAYS=30                    # 0 disables pruning
```

Learned health, telemetry aggregates, quality aggregates, and decision
statistics are flushed on an interval and once more on graceful shutdown.
The database never contains prompts, responses, API keys, proxy
credentials, or correlation ids. Ensure the directory is writable by the
service account and backed up like any other database file.

`PERSISTENCE_RETENTION_DAYS` also bounds the security event log (the
`events` table in `state_dir/platform.db`): rows older than the window are
pruned on the same retention tick (`0` disables pruning). See the
[security model](security.md) for the event-log contract.

### 4. Enable feedback so routing learns

```bash
HEALTH_FEEDBACK_ENABLED=true
TELEMETRY_ENABLED=true
```

Health feedback and telemetry are recorded from real request outcomes and
are what let routing react to provider outages. Without them, routing
stays static (priority + health probes only).

### 5. Proxies

If Relay sits behind a corporate proxy, outbound provider calls honor
`HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` when `PROXY_ENABLED=true`.
Proxy credentials are never logged or exposed. Set `PROXY_ENABLED=false`
to disable proxy support entirely.

### 6. Logs

`LOG_LEVEL` (default `INFO`) and optional `LOG_FILE` control logging.
Log records never include prompts, responses, or secrets.

### 7. Health refresh

For fast detection of provider outages, enable the background health
refresher:

```bash
HEALTH_REFRESH_ENABLED=true
HEALTH_REFRESH_INTERVAL_SECONDS=60
```

It re-probes providers (and, with `HEALTH_DEEP_REFRESH_ENABLED`, every
chat model) on an interval.

## Security and privacy model

- **Never in the database**: prompts, responses, API keys, proxy
  credentials, correlation ids (`app/services/memory_contract.py` + tests).
- **Never in metrics/ops**: prompts, responses, or user data. `/metrics`
  and `/diagnostics` are operational metadata only.
- **Never in admin responses**: secrets are reported by field name.
- **Provider error bodies are redacted**: raw provider response text is
  truncated, stripped of control characters, and scrubbed of the API key
  before it can reach error responses or logs
  (`app/providers/openai_compat_client.py`).
- **Feedback is metadata-only**: the `/feedback` schema rejects any
  payload carrying prompt/message/response/content fields.

## Operational endpoints

- `GET /health` — public liveness: `{"status": "ok"}` (or
  `degraded`/`unavailable`). Use this for load balancers and container
  health checks.
- `GET /diagnostics` — full operational snapshot (provider states,
  learned health, telemetry summaries, decision stats, persistence
  status). Use it before contacting support.
- `GET /metrics` — Prometheus endpoint; scrape it with Prometheus or any
  OpenMetrics-compatible collector.

## Hot configuration reload

`POST /admin/reload` re-reads `.env` and applies the reloadable allowlist
(see [configuration.md](configuration.md) for the reloadable flags) in
place, including provider `enabled`/`api_key`/`model_priority` and the
persistence retention window. Use `?dry_run=true` to preview. Validation
errors return `400`; mid-apply failures roll back and return `500`.

## Provider key migration runbook

Move cloud-provider keys out of `.env` into the OS keyring with one
command (canonical form; `relay keys provider migrate` is an alias):

```bash
# 1. Preview the plan — never prints secrets
relay provider keys migrate --dry-run

# 2. Run it (non-interactive automation passes --yes)
relay provider keys migrate --yes

# 3. Make sure runtime reads the keyring after the flip
#    Add to .env: RELAY_KEYRING=true
```

Behavior:

- **Env only** → moved to the keyring, then removed from `.env`.
- **Keyring == env** → reported `already`, left alone (idempotent re-run
  is a no-op).
- **Keyring != env** → reported as a conflict with masked tails; skipped
  unless `--force` (which overwrites the keyring entry with the `.env`
  value). Without `--force` the command exits non-zero on any conflict.
- **Keyring only** → skipped (nothing to migrate).
- Output never contains raw key material; only `********abcd` tails.

Safety:

- Writes land **first**; `.env` entries are removed only after **all**
  writes succeed. A keyring outage aborts with `.env` untouched and
  providers keep working via the `.env` fallback.
- A mid-run failure is not a broken state: each provider resolves
  independently (keyring entries for migrated providers + env fallback
  for the rest).
- **Rollback one provider**: `relay provider keys set <id> <value>`
  restores the `.env` value, or remove the keyring entry and re-set.
- **Full undo**: `relay provider keys remove <id>` for each provider,
  then `relay provider keys set <id>` to write `.env` again.

After migration the provider-key env vars can be removed; they are
already deprecated and are resolved as a fallback only.

## Relay key rotation and pruning runbook

Operator access to Relay is authenticated by `rl_` keys stored as scrypt
hashes in `platform.db` (the legacy `relay_keys.db` content was imported by
`relay migrate`). Rotate and prune them from the CLI or the admin API.

```bash
# Rotate one key: prints the new raw key exactly once, then revokes the
# original. Non-interactive automation passes --yes.
relay keys rotate <key_id> --yes

# Dry-run: list terminal keys (revoked, or expired) that are past the
# 30-day grace window. Nothing is changed.
relay keys prune

# Execute the prune (removes only terminal rows past the grace window).
relay keys prune --yes

# Tail the security event log (key.create / key.rotate / key.prune /
# auth.failure / ...), newest first.
relay events --limit 100
```

- `--older-than-days N` shortens/lengthens the prune grace window; active
  rows are never touched.
- Equivalent API surfaces: `POST /admin/keys/{id}/rotate`,
  `GET /admin/keys` (entries carry `expires_soon`),
  `GET /admin/events?action=&outcome=&limit=` (admin scope).
- `relay migrate` prunes terminal keys automatically after import and
  records `key.prune`; a purge failure never fails the migration.

## Platform database and `relay migrate`

Relay consolidates persistence in `state_dir/platform.db` (SQLite, schema
v8): API keys, learned-state aggregates, model status, the security event
log, and (when `CONTINUITY_ENABLED=true`) the project-continuity tables.
An existing installation imports the legacy stores
(`relay_keys.db`, `relay_state.db`) in place, in-process and under a
lock:

```bash
relay migrate --dry-run   # print the plan, change nothing
relay migrate --yes       # import + verify + commit (auto-prunes terminal keys)
relay migrate --rollback  # restore sources from backup, remove platform.db
```

The migration aborts safely on import or verification failure and rolls
back from a backup; it never fails the run because the auto-prune found
nothing or failed. Back up `state_dir/` before migrating.

## Graceful shutdown

Send `SIGTERM` (or `SIGINT`) to the server process. The lifespan handler
stops the health refresher and state flusher, and performs a **final
flush** of learned state before exiting, so no learned intelligence is
lost. Hard-killing the process can lose up to one flush interval of
learned state.

## Reverse proxy notes

Put TLS termination at your reverse proxy. Relay itself serves plain HTTP
by default. The `X-Relay-Correlation-Id` response header is set on both
success and error responses; surface it in your client logs to correlate
failures with `/diagnostics` and ops-window data.
