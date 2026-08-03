# Blockers Before Public Release

Items that must be resolved before Relay is considered generally available
as a public cloud gateway. Blocker #1 is the only hard environment
failure; #2 and #3 were decisions, both now implemented (see below).

## 1. OpenAI key has no quota — HARD BLOCKER

During RC, every OpenAI completion returned
`HTTP 429 "You exceeded your current quota, please check your plan and
billing details."` The `/v1` surface surfaced this correctly (502 +
`provider_error`), and the streamed-error path is now fixed
(`test_stream_start_error_surfaces_provider_body`), but **no OpenAI
completion can succeed until the account has an active plan/billing**.

Action:

1. Restore billing/quota on the OpenAI account behind `OPENAI_API_KEY`.
2. Re-run the live smoke and confirm all six steps pass against OpenAI:
   ```bash
   python tests/run_live_smoke.py
   ```

Until this is resolved, the gateway is NVIDIA-only in practice.

## 2. `/v1` health feedback gap — RESOLVED

The `/v1` OpenAI surface now records **per-attempt** telemetry and health
feedback, matching the native `/chat` pipeline
(`app/core/relay.py::_record_telemetry` / `_record_feedback`). Failed
attempts inside a request feed real failure signals; the winning attempt
records its success, so a request recovered by failover does not take down
the provider that served it.

Implemented in `app/api/openai.py` (`_record_attempts_telemetry_and_health`),
which iterates `result["attempts"]` on both the success and failure paths.
The `/v1` path also honors `MAX_RETRIES` like `/chat`.

Coverage:

- `tests/test_openai_api.py::TestV1HealthLearning` — `/v1` failover learns
  the failure and keeps the winning provider clean; retry recovery clears
  transient degradation; fully-failed requests record every attempt.
- `tests/test_rc_validation.py::TestReliabilityMatrix` — per-attempt
  telemetry counts (`request_count == 2` after a retried failure+success).

## 3. 429 `Retry-After` — RESOLVED (opt-in feature)

Relay now supports honoring the provider's `Retry-After` on retries, with
an exponential-backoff fallback and a total request-timeout budget. All new
knobs default to preserving the previous immediate-retry behavior:

- `RETRY_HONOR_RETRY_AFTER` (default `false`) — sleep for the provider's
  `Retry-After`, capped by `RETRY_AFTER_MAX_SECONDS`.
- `RETRY_BACKOFF_BASE_SECONDS` (default `0`) — exponential backoff base;
  capped by `RETRY_BACKOFF_MAX_SECONDS`.
- `REQUEST_TIMEOUT_BUDGET_SECONDS` (default `0`) — total wall-clock budget
  for a request; waits never exceed the remaining budget.

The remaining operator decision is whether to enable these for the deployed
profile. Until then the default immediate-retry behavior (documented in
`docs/known-limitations.md` item 1) applies. Reference:
`tests/test_retry_hardening.py`.

## 4. Pin production model priorities

NVIDIA `/models` returns 221 models but the account can only invoke a
subset; the rest 404 as `invalid_request`, lengthening `/chat` candidate
walks and polluting telemetry. Ship with explicit
`NVIDIA_MODEL_PRIORITY` / `OPENAI_MODEL_PRIORITY` set to models the
account can invoke. See [known-limitations.md](known-limitations.md)
item 4.

## 5. Enforce auth and persistence in the deployed profile

Defaults disable authentication (`RELAY_API_KEY` empty) and persistence.
The public release profile must set:

```bash
RELAY_API_KEY=<long-random-value>
PERSISTENCE_ENABLED=true
PERSISTENCE_PATH=/var/lib/relay/relay_state.db
```

Otherwise an exposed instance is unauthenticated and learns nothing
across restarts.

## Definition of done (all blockers cleared)

- [ ] OpenAI quota restored; live smoke 6/6 against OpenAI
- [x] `/v1` health feedback per-attempt implemented and tested
      (`app/api/openai.py` + `TestV1HealthLearning`)
- [x] 429 `Retry-After` policy implemented as opt-in retry hardening
      (`RETRY_HONOR_RETRY_AFTER`, backoff, budget; defaults unchanged)
- [ ] Decide whether to enable retry hardening in the deployed profile
- [ ] Model priorities pinned for the deploying account
- [ ] Deployed with `RELAY_API_KEY` and `PERSISTENCE_ENABLED=true`
- [ ] Full regression green on the release candidate:
      `python -m pytest tests -q`
- [ ] RC checklist re-run and signed off:
      `python -m pytest tests/test_rc_validation.py -q`
