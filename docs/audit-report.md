# Hardening Audit Report

Final report for the production-readiness hardening pass. Companion docs:
`architecture.md`, `configuration.md`, `routing-decisions.md`, `deployment.md`, `troubleshooting.md`.

## Summary

Repository-wide audit covering architecture, reliability/performance, and
security/privacy/observability. Two security gaps found and fixed, two
thread-safety defects found and fixed, and a set of lower-priority
performance items identified and explicitly deferred.

- Regression gate: `pytest tests -q` -> **747 passed, 5 skipped** (~32s)
- Bytecode compile: `python -m compileall -q app tests` -> clean
- New/changed files: 8 source files, 4 test files, 6 docs.

## Findings fixed

| # | Severity | Finding | Fix |
|---|----------|---------|-----|
| A | High | `/docs`, `/redoc`, `/openapi.json` were disabled, so there was no way to discover or exercise the API without reading source. | `app/main.py` now registers real docs/OpenAPI endpoints that inherit the global `require_api_key` dependency (`docs_url=None` + explicit routes using `get_swagger_ui_html`/`get_redoc_html`/`app.openapi()`). |
| B | High | Provider error bodies (`response.text`) were embedded in `ProviderHTTPError` -> `Attempt.reason` -> ops store and metrics. Bodies can contain secrets (e.g. an echoing provider API key). | `app/providers/openai_compat_client.py` gains `_safe_provider_body()` applied at all 4 `response.text` sites: strips the provider API key (replaced with `[REDACTED]`), removes non-printable control chars (keeps `\n`/`\t`), truncates to 200 chars, and falls back to `"status <code>"` for empty bodies. |
| C | Medium | `ProviderManager.register()` (hot reload) could race request-path iteration over the provider dict, raising `RuntimeError: dictionary changed size during iteration`. | `app/services/provider_manager.py` wraps the provider dict in an `RLock`; every method (`register`, `get`, `all`, `enabled`, `ranked`) guards the dict, and `best()` delegates to locked `ranked()`. |
| D | Medium | `Metrics.update_provider_health()` read-modify-wrote `_provider_statuses` without a lock; concurrent health reports could leave stale active statuses. | `app/services/metrics.py` adds `_provider_statuses_lock`; the status transitions (incl. `provider_health.set`) run under the lock. |

## New tests

- `tests/test_auth.py` (extended): docs/OpenAPI reachable when auth disabled;
  schema requires key; bearer token works on docs.
- `tests/test_openai_compat_client.py` (extended): `TestProviderBodyRedaction`
  covers key redaction, truncation bounds, control-char stripping, plain-text
  passthrough, probe details.
- `tests/test_hardening.py` (new): concurrent chat (no response mixing),
  failure storm (attempt/telemetry accounting under retries), reload racing
  requests (no provider-field tearing), lifespan (health refresher + state
  flusher start/stop, shutdown flush), provider_manager register-while-
  iterating, metrics concurrent health updates.

## Remaining risks (deferred, non-blocking)

All are performance/efficiency items behind feature flags, none are
correctness or security defects; changing them risks destabilizing the suite
at the end of the hardening pass.

- `app/services/candidate_builder.py`: iterates all providers and takes
  `manager._lock` per provider; O(m) lock churn on the request path.
- `app/services/adaptive.py`: `states()` / window scans are O(m^2) in provider
  count; bounded by provider fan-out, but worth noting for large setups.
- `app/services/telemetry.py`: `_entries` list is unbounded until persisted;
  checkpoints bound it in practice (default retention 0 = unbounded
  accumulation on disk). Consider an in-memory cap.
- `app/services/health_store.py`: pending-degradation counters are
  read-modify-write without a lock; single-writer refresher makes this safe
  today.
- `app/services/decision_engine.py`: per-request decision records accumulate
  in-memory for the lifetime of the decision service.
- `app/services/reload.py`: hot reload mutates `provider.enabled` /
  `api_key` / `model_priority` in place rather than replacing the provider
  object; consistent with the current manager-level locking.
- No `RELAY_API_KEY` in `.env` by default: auth is opt-in. Set it before
  exposing the service.

## Future improvements

- Cap `telemetry._entries` in memory and add persistence backpressure.
- Replace candidate-builder lock churn with a precomputed, immutable snapshot
  of provider state per reload cycle.
- Reduce adaptive scoring to O(m) using a running histogram.
- Add optional per-route API-key requirements (scope/audience separation).
- Add load tests for provider fan-out and a benchmark script in the test suite.

## Audit trail

- Auth: `app/core/auth.py:28-35` SHA-256 + `hmac.compare_digest`; global
  dependency applied in `app/main.py`.
- Exposure chain verified end-to-end: `openai_compat_client.py` error ->
  `chat_service.py` -> `Attempt.reason` -> ops store + metrics. Redaction at
  the client boundary prevents the value from ever entering `Attempt.reason`.
- Provider dict guarded at `app/services/provider_manager.py` (RLock).
- `_provider_statuses_lock` at `app/services/metrics.py`.
- Docs endpoints now behind `require_api_key` in `app/main.py`.
