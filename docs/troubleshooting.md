# Troubleshooting

## Startup problems

**`Invalid value for <VAR>: ...` at startup**
Configuration validation failed. The error names the offending variable.
Fix it in `.env` (or the environment) and restart. Common causes:
non-numeric values for integer settings, values below the documented
minimum, weights outside `[0, 1]`, and URLs not starting with `http://`
or `https://`.

**Server starts but `/providers` returns no providers**
No provider is enabled or configured. Check `NVIDIA_ENABLED`,
`OPENAI_ENABLED`, and `LMSTUDIO_ENABLED` in `.env`, and that the enabled
provider has an API key (`NVIDIA_API_KEY`, `OPENAI_API_KEY`) or does not
require one (LM Studio). Run `python -m app.cli setup` for an interactive
walkthrough.

## Authentication problems

**Everything returns `401 Unauthorized`**
`RELAY_API_KEY` is set and the request did not carry a valid key. Send
`Authorization: Bearer <key>` or `X-Relay-API-Key: <key>`. `/health` and
`/` remain public.

**`/docs` and `/openapi.json` return `401` while `RELAY_API_KEY` is set**
Expected: documentation routes are protected by the same global
dependency as every other route. Add the key header to your browser via
the "Authorize" button, or access them through a client that sends the
header.

## Chat failures

**`502` with a correlation id**
Every candidate failed. The response body contains the (redacted,
truncated) failure reasons. Capture the `X-Relay-Correlation-Id` header
and inspect `GET /diagnostics` to see provider health, telemetry, and
failure summaries. Common causes:

- Invalid/expired API key for the provider (see `auth_error` failures).
- Quota exhausted (see `quota_exhausted`).
- Provider outage (see `server_error`/`timeout`).
- Local server not running (LM Studio): confirm `LMSTUDIO_BASE_URL`.

**`503`**
No provider was available at all (none registered or none enabled).

**Provider error bodies contain `...` or `[REDACTED]`**
Raw provider response text is deliberately truncated and scrubbed of the
API key before it reaches error responses. The status code and a bounded
message remain.

**Routing keeps hitting a model that is failing**
This is the learning loop working as designed. Enable
`HEALTH_FEEDBACK_ENABLED` and `TELEMETRY_ENABLED` so real failures update
the health store, and/or lower the `HEALTH_FEEDBACK_*_THRESHOLD` values
so marks are applied sooner. Marks expire after
`HEALTH_DEGRADED_TTL_SECONDS` / `HEALTH_UNAVAILABLE_TTL_SECONDS` unless
fresh failures keep them alive.

## Persistence

**Learned state is not surviving restarts**
Ensure `PERSISTENCE_ENABLED=true` and the process is shut down with a
graceful signal (not killed), so the final flush runs. Check
`/diagnostics` → `persistence` for `storage_status` and `load_errors`.
A corrupted database is backed up as `<PERSISTENCE_PATH>.corrupt-*.bak`
(and the `-wal`/`-shm` sidecar files) and
persistence is disabled for that process rather than failing startup —
check for `persistence_init_error`.

**State file grows without bound**
Enable retention pruning with `PERSISTENCE_RETENTION_DAYS` (e.g. `30`).
The retention window is reloadable via `POST /admin/reload` without a
restart.

## Performance

**`/health/deep` is slow**
It probes every chat-capable model across providers. Prefer `/health` for
liveness and configure `NVIDIA_MODEL_PRIORITY` /
`OPENAI_MODEL_PRIORITY` so normal checks probe only the models that
matter.

**High memory**
The ops window (`OPS_WINDOW_SECONDS`, `OPS_MAX_EVENTS`) and quality
retention (`QUALITY_FEEDBACK_RETENTION_LIMIT`) bound in-memory growth.
Tighten them if needed.

## Testing

**Tests hit the network**
They should not. `tests/conftest.py` disables provider loading for the
whole session. If a test tries real I/O, a provider is being constructed
from a non-disabled setting — check the test for `nvidia_enabled`/
`openai_enabled`/`lmstudio_enabled` overrides.
