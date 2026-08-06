# Release Candidate Checklist

Validation gate for taking Relay to production as an OpenAI-compatible
cloud gateway (NVIDIA + OpenAI). Everything below was executed against the
real application code in the documented production configuration. Local
providers remain disabled (deferred until the cloud gateway is stable).

## How this was validated

| Artifact | What it proves | Result |
|---|---|---|
| `tests/test_rc_validation.py` | Full production-profile gateway behavior against scripted loopback upstreams standing in for NVIDIA/OpenAI (deterministic, repeatable) | **28 passed** |
| `tests/run_live_smoke.py` | Real cloud connectivity through the real server using live `.env` keys | **6/6 vs NVIDIA** |
| Full regression `pytest tests -q` | No behavior change outside the gateway surface | **2360 passed, 22 skipped** |
| CI (`.github/workflows/ci.yml`) | Full suite + compile check on Linux (Python 3.11/3.12) and Windows (Python 3.12); sdist/wheel build + fresh-venv install smoke on Linux | Pass on merge gate |

The 2360/22 count is the post-R2 baseline (post-P9 was 2338/22; P6.4 was
1916/18, originally 821/5). The suite is
run as a merge gate by CI, so the regression row is verified on every
push.

Run order (offline checks first, then the paid live smoke):

```bash
python -m pytest tests/test_rc_validation.py -q
python -m pytest tests -q
python tests/run_live_smoke.py                 # uses .env live keys
```

`SMOKE_MODEL` and `SMOKE_NVIDIA_MODEL` override the models the live smoke
targets (see `tests/run_live_smoke.py`).

## 1. Production gateway workflow

- [x] `/v1/chat/completions` non-stream round trip returns the provider
      body verbatim (no invented defaults) — `test_sdk_non_stream_round_trip`
- [x] Streamed responses carry one stable relay-generated `id`, passthrough
      finish reason, and the provider usage chunk when requested —
      `test_sdk_stream_with_usage`, `test_streaming_stable_id_and_correlation`
- [x] Tool calling round trip: `tool_calls`, `tool_call_id`, `tools`,
      `tool_choice` all forwarded verbatim — `test_sdk_tool_calling_round_trip`
- [x] Streaming tool-call deltas pass through — `test_sdk_streaming_tool_calls`
- [x] Unknown model → `400` with `{"error":{...}}` shape —
      `test_openai_error_shape_unknown_model`
- [x] `tool_choice` without `tools` → `400` — `test_tool_choice_without_tools_is_400`
- [x] `/v1/models` lists provider models — `test_models_listing`
- [x] API-key auth enforced; `/` and `/health` stay public —
      `test_auth_enforced`
- [x] Native `/chat` routes to the highest-priority provider —
      `test_chat_happy_path_routes_to_priority_provider`
- [x] Native `/chat` task classification — `test_chat_classifies_task`

## 2. Reliability matrix

- [x] 5xx retried then success (2 upstream attempts, final `200`) —
      `test_retry_on_5xx_then_success`
- [x] 429 retried immediately by default (documented default policy, see
      `docs/known-limitations.md`) — `test_retry_on_429_ignores_retry_after`
- [x] `Retry-After` honored when `RETRY_HONOR_RETRY_AFTER=true`, capped at
      `RETRY_AFTER_MAX_SECONDS` — `tests/test_retry_hardening.py`
- [x] Exponential backoff and request-timeout budget bounds —
      `tests/test_retry_hardening.py`
- [x] Provider timeout then retry — `test_timeout_then_retry_success`
- [x] Malformed provider body (`<html>not json</html>`) retried to success —
      `test_malformed_provider_response_retries`
- [x] Provider unavailable → failover across providers —
      `test_provider_unavailable_fails_over_across_providers`
- [x] All providers fail → `502` with `{"error":{...}}` shape (never
      `{"detail":...}`) — `test_all_providers_fail_returns_502_error_shape`
- [x] Mid-stream hang → `stream_error` SSE chunk then `[DONE]`, timeout
      recorded — `test_mid_stream_failure_emits_error_and_records`
- [x] Stream-start provider error surfaces the real provider body, not
      httpx internals — `test_stream_start_error_surfaces_provider_body`
- [x] Verified live: NVIDIA returned 529 overloads mid-smoke and Relay
      retried then failed over to a working model — `tests/run_live_smoke.py`

## 3. Routing intelligence

- [x] Health learning reroutes away from a degraded model —
      `test_health_learning_reroutes_away_from_degraded_model`
- [x] `/v1` records per-attempt telemetry/health (failover keeps the winning
      provider clean; retry recovery clears degradation; all-failed requests
      record every attempt) — `TestV1HealthLearning`
- [x] Adaptive EWMA learns from telemetry — `test_adaptive_telemetry_learns_ewma`
- [x] Quality feedback recorded and exposed — `test_quality_feedback_recorded_and_exposed`
- [x] Duplicate `correlation_id` feedback deduped —
      `test_quality_feedback_duplicate_correlation_deduped`
- [x] Feedback payloads carrying content fields rejected —
      `test_quality_feedback_rejects_content_fields`
- [x] Learned state survives restart via SQLite —
      `test_persistence_survives_restart`
- [x] `/diagnostics` reflects telemetry, providers, persistence, operations
      — `test_diagnostics_accuracy`

## 4. Live cloud smoke (paid)

- [x] NVIDIA: `/health`, `/v1/models` (221 models discovered), native
      `/chat`, non-stream, stream, tool call — **all pass** against
      `deepseek-ai/deepseek-v4-flash` (the RC-pinned smoke model; the
      earlier `deepseek-ai/deepseek-v4-pro` now times out upstream at 120s).
      Upstream intermittently returns `HTTP 529 Service temporarily
      overloaded`; Relay retries and fails over correctly (see the
      reliability matrix).
- [ ] OpenAI: **BLOCKED — the `.env` key is out of quota** (HTTP 429 on
      every completion). Non-stream/stream/tool-call steps surface the
      quota error correctly (correct `502` + `provider_error` shape), which
      itself proves the error path works, but no OpenAI completion can
      succeed until billing is restored. Re-run the smoke after fixing the
      key.

## 5. Deliverables

- [x] `docs/release-candidate-checklist.md` (this file)
- [x] `docs/deployment.md` — cloud gateway deployment section added
- [x] `docs/known-limitations.md` — validated limitations and findings
- [x] `docs/rollback-procedure.md`
- [x] `docs/blockers-before-public-release.md`
- [x] `docs/v1.0.0-readiness-report.md` — final audit gate: checklist,
      verification evidence, remaining risks, required actions

## Sign-off state

The gateway is **release-candidate ready with one environment blocker**:
the OpenAI key has no quota. NVIDIA traffic is fully validated. See
`docs/blockers-before-public-release.md` for the exact steps before
general availability.
