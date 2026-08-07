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
| Full regression `pytest tests -q` | No behavior change outside the gateway surface | **2399 passed, 20 skipped** |
| CI (`.github/workflows/ci.yml`) | Full suite + compile check on Linux (Python 3.11/3.12) and Windows (Python 3.12); sdist/wheel build + fresh-venv install smoke on Linux | Pass on merge gate |

The 2399/20 count is the RC1 tag baseline (post-R2 was 2360/22; post-P9
was 2338/22; P6.4 was 1916/18, originally 821/5). The suite is
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

## 6. RC1 stage execution (2026-08-07)

RC1 stage run against the `v1.0.0-rc.1` tag (package `1.0.0rc1`), per the
hardening-plan §7 gate. Decisions recorded in `docs/release-decisions.md`
(R2–R3 / RC1 closure): **NVIDIA-ready-only RC1** (D11) — OpenAI quota is a
documented release caveat, not a gate.

| Check | Result | Evidence |
|---|---|---|
| sdist + wheel build | PASS | `python -m build` → `relay-1.0.0rc1.tar.gz` + `relay-1.0.0rc1-py3-none-any.whl`; metadata `Name: relay / Version: 1.0.0rc1 / License: MIT / Requires-Python: >=3.10` |
| Fresh install (Windows venv) | PASS | Wheel installed into a clean venv with all deps; `pip check` clean; `relay --help` shows the full subcommand surface |
| `relay --version` | PASS | `relay 1.0.0rc1` |
| Full regression (tag baseline) | PASS | **2399 passed, 20 skipped, 0 failed** (240.56s); `python -m compileall -q app tests` clean |
| RC offline suite + adversarial | PASS | `test_rc_validation.py` + `test_continuity_adversarial.py`: **108 passed** (39.26s) |
| Security review suites | PASS | auth / key-auth / hardening / retry-hardening / memory-contract / security-hardening: **117 passed, 3 skipped** |
| Deployed profile (`/diagnostics`) | PASS | Booted `relay serve` from the installed wheel with the hardened profile: persistence `enabled=True, available=True, schema v8, status=ok`; NVIDIA `healthy` (1 healthy model) with live `/v1/chat/completions` round trip; OpenAI `degraded` + `rate_limited` (429 quota, B1 as documented); `/admin` POST **401** without key, **200** with bootstrap key; scoped key (`chat,v1`) chats and is **403** on `/admin` |
| Migration upgrade drill | PASS | `relay migrate --state-dir <scratch> --dry-run` (plan correct) → `--yes` (backup + platform.db v8 + integrity ok + verified row counts; expired key pruned by the 30-day grace window) → re-run reports "Already migrated" |
| Migration rollback drill | PASS | `relay migrate --rollback last` restored all legacy sources and removed `platform.db`; re-migrate succeeds after rollback (full round trip) |
| Windows smoke | PASS | Live `/v1` non-stream through the deployed profile (NVIDIA `meta/llama-3.1-8b-instruct`) |
| Linux smoke + CI matrix on the tag | CI-pending | Local equivalents green (Windows 3.12 full suite, build, wheel install). Linux 3.11/3.12 + packaging job run on the tag when pushed (CI workflow unchanged; `on: push` is `main` only) |

## Sign-off state

RC1 stage is **passing on all runnable checks**. One environment blocker
remains documented: the OpenAI key has no quota (B1), so the release is
**NVIDIA-ready-only** with OpenAI as a documented caveat and a future
validation item (re-run `tests/run_live_smoke.py` once the key has quota).
The only remaining gate action before the `v1.0.0` tag is the Linux/CI
matrix on the tag, which requires pushing the branch/tag to the remote.
See `docs/blockers-before-public-release.md` for the exact steps before
general availability.
