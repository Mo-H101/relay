# Relay Pre-Release Adversarial Test Campaign — Record

- **Date**: 2026-08-24
- **Repo under test**: `<repository-root>` @ `2e74c6e` ("docs: record final hardening checkpoint")
- **Environment**: Android aarch64, Python 3.14.6
- **Campaign root**: `<campaign-root>`
- **Repo integrity**: working-tree status hash `3f62f322…4358` byte-identical before/after all stages. Repository untouched; all artifacts external.

## Environment deviations (documented, not code changes)
| Pin | Installed | Why |
|---|---|---|
| pydantic==2.11.7 | **2.12.0** | pydantic-core (2.11.x sdist) cannot build on-device (no usable Rust for its build backend); 2.12.0 ships compatible wheels |
| Schemathesis CLI | substitute fuzzer | schemathesis 4.25.1 imports fail on-device: `dlopen failed: cannot locate symbol "_Py_NoneStruct"` in `jsonschema_rs.abi3.so` |

All other exact pins honored (`fastapi==0.116.1`, `uvicorn==0.35.0`, `httpx==0.28.1`, `python-dotenv==1.2.2`, `rich==15.0.0`, `textual==8.2.8`, `platformdirs==4.11.0`, `keyring==25.7.0`). pytest restored to 8.3.4 after schemathesis pulled 9.x.

## Stage results

### 0. Baseline test suite — PASS (with harness caveat)
2885 passed / 8 skipped / 2 failed. Both failures caused by campaign's exported `RELAY_STATE_DIR`:
`test_source_checkout_prefers_cwd_env_when_present`, `test_installed_cli_runs_from_arbitrary_cwd_with_stable_state`.
Both pass standalone without the env var. Historical flakes did not reproduce.
Evidence: `evidence/01_BASELINE.md`, `logs/BASELINE_FULL.txt`.

### 1. Chaos L1 (provider-client layer over real TCP) — 13/13 PASS
RST/premature-FIN/truncation/mid-stream-RST/slow-drip/UTF-8/oversized-line/dup-headers/secret-leak scenarios.
Slow-drip hit `ProviderResponseLimit` at 17.8s with shrunk budget. All errors Provider*-classified, bounded wall-clock,
zero fd leaks, zero secret leakage (`sk-EVILUPSTREAMSECRET`, `sk-upstream-test` absent from all outputs/state).
Evidence: `logs/CHAOS_L1_V2.txt`, `evidence/CHAOS_RESULTS_V2.json`.

### 2. Chaos L2 (end-to-end via real uvicorn server) — 5/5 PASS
Pre-chunk RST → clean immediate 502; post-first-chunk RST → partial content + terminal `stream_error` + [DONE];
midstream secret injection → not leaked; provider failover LMStudio(hostile A)→Ollama(healthy B) succeeds;
happy path exposed F-C3 once (see findings).
Evidence: `logs/CHAOS_L2_V3.txt`, `evidence/CHAOS_L2_RESULTS.json`.

### 3. Graceful shutdown with active SSE streams — F-S1 (non-blocking)
SIGTERM during 5 active SSE streams does not exit within 30s (uvicorn default has no graceful-shutdown bound;
no `timeout_graceful_shutdown` set in `app/cli/__init__.py`). Patient variant: all 5 streams complete cleanly (~114s),
lifespan final-flushes run, exit code `-15` is uvicorn's intentional captured-signal re-raise. No orphan processes.
Evidence: `logs/SHUTDOWN_SSE_PATIENT.txt`, `evidence/SHUTDOWN_SSE.json`.

### 4. SQLite multi-process durability — PASS
45s server+chat while 3 concurrent CLI hammers (`provider keys`, `keys`, `events`, `apps`): zero lock errors.
SIGKILL mid-write-burst → `integrity_check`=ok, restart 1.74s, health OK, WAL recovered (~2.7MB→checkpointed).
Evidence: `logs/SQLITE_MULTIPROC.txt`, `evidence/SQLITE_MULTIPROC.json`.

### 5. Property-based tests (Hypothesis 6.165.10) — 5/5 PASS
P1 error-classifier totality/closed-set; P2 AuthThrottle stateful (bucket cap `_MAX_BUCKETS` holds);
P3a redact_text keyish strings; P3b redact_dict idempotence; P4 mask_key never exposes body.
Evidence: `logs/HYPOTHESIS.txt`, `evidence/HYPOTHESIS_RESULTS.json`.

### 6. OpenAPI schema fuzzing (Schemathesis substitute) — 0 findings
244 calls across all 18 documented paths: mixed auth, garbage-body phase (150 examples), auth-attack mix.
No crashes, no unexpected 5xx, no leaks. Real Schemathesis deferred to x86 machine (import blocked on-device).
Evidence: `logs/SCHEMA_FUZZ.txt`, `evidence/SCHEMA_FUZZ_RESULTS.json`.

### 7. Process soak (30min baseline + 8h main, 6 workers, burst-14 waves, auth probes)
Launched detached (`state/SOAK_PID`). Harness validated by short smokes up to 10,967/10,967 OK,
mixed streaming/non-streaming, fds stable (15–22), zero admission 503s, sub-ms latencies.
Final numbers appended post-run to `evidence/SOAK_SUMMARY.json`.

### 8. Wheel install verification — PASS (with documented substitution)
Built out-of-tree (repo untouched): `relay-1.0.0rc1-py3-none-any.whl` (`logs/WHEEL_BUILD.txt`).
Fresh venv install requires pydantic substitution as above; installed CLI runs from arbitrary cwd,
boots against mock upstream, serves `/health` 200 and a full streamed chat completion 200.
Evidence: `logs/WHEEL_SMOKE.log`, `scripts/debug_wheel_smoke.py`.

### 9. pip-audit — 1 FINDING
52 packages audited (`evidence/PIPAUDIT.json`). pip's own advisory cleared by upgrade.
**starlette 0.47.3** (pinned transitively via `fastapi==0.116.1`) carries 6 open advisories:
PYSEC-2026-161 (fix 1.0.1), -1942 (0.49.1), -2280/-2281 (1.1.0), -248 (1.3.0), -249 (1.3.1).
Full remediation requires starlette ≥1.3.1 — likely incompatible with fastapi 0.116.1's own constraint;
needs upstream dependency bump decision.

## Findings ledger
Classification vocabulary: genuine Relay defect / campaign-harness defect /
environmental-device-specific behavior / known-documented behavior /
intentional design / dependency-supply-chain finding / documentation-process gap.

### F-C3 — Spurious terminal stream_error after fully-delivered stream — **genuine Relay defect (reliability)**
When an upstream closes the connection immediately after `[DONE]`, Relay can emit a terminal
`{"error":{"message":"Provider request failed.","type":"stream_error"...}}` event AFTER all content was delivered;
the turn is recorded as failed despite complete delivery. Observed twice (chaos L2 happy path; soak debug run).
Root cause: `achat_stream_messages` breaks on `[DONE]` without draining the HTTP chunked terminator
(`app/providers/openai_compat_client.py` ~L1419–1424); upstream-close race makes `aclose()` raise inside the
client context and the exception is classified as mid-stream failure. Not deterministic
(16/16 negative in targeted race harness `scripts/fc2_close_race.py`). Client impact: error event after complete
content; metrics/history record false failures. Recommendation: fix before release (drain terminator / suppress
post-[DONE] exceptions).

### F-C1 — list_models raises unclassified parse exceptions — **known/documented behavior (contract gap, contained)**
Malformed 200 bodies from provider discovery raise raw `JSONDecodeError`/`UnicodeDecodeError` rather than a
ProviderError subclass; contained by broad handlers (`app/providers/factory.py:86`, `app/services/reload.py:273`),
so behavior is safe. Contract gap only.

### F-S1 — Unbounded graceful-shutdown window — **intentional design (upstream uvicorn default); operational gap**
Active SSE streams delay SIGTERM exit indefinitely (uvicorn default). Suggest `timeout_graceful_shutdown`
in server construction. Exit code `-15` semantics are intentional upstream design.

### Supply-chain — starlette advisories under pinned fastapi — **dependency/supply-chain finding; adjudicated NOT exploitable against Relay**
See stage 9 and `evidence/EVIDENCE_STARLETTE.md` for advisory-by-advisory disposition
(all six target code paths Relay never uses, or are engineered away in Relay's auth).
Release decision needed: time-bounded fastapi/starlette upgrade task.

### Metadata/doc gap — OPENAI_BASE_URL — **documentation/process gap**
Registry metadata advertises OPENAI_BASE_URL but config_spec defines no runtime setting; env var is never read.
Documentation or metadata fix required.

### Harness defects 1–7 — **campaign/harness defects** (external scripts only, enumerated above)

### On-device build limitations (pydantic pin substitution, schemathesis abi3 dlopen failure,
release.sh in-tree build) — **environmental/device-specific behavior**

## Harness defects found & fixed during campaign (all in external scripts only)
1. Chunked framing invalid in v1 chaos suite (rewritten properly).
2. Fixed ports couldn't rebind between scenarios → ephemeral ports everywhere.
3. Hostile/mock servers not closed between scenarios → teardown added.
4. Mocks not draining request bodies → spurious RST-on-close races (mass instant 502s) → body drain added.
5. Soak driver spawned a single Worker whose idx equaled n_workers (even) forcing permanent non-stream mode;
   mocks answered SSE to non-stream requests → rewritten: workers idx=1..N mixed modes, mock replies JSON
   completion for `stream:false`.
6. Timed-out drivers orphaned uvicorn children poisoning later runs (stale relay answered /health with dead
   upstream → 100% instant 502) → preflight port-bind guard + SIGTERM handler killing child added.
7. `OPENAI_BASE_URL` unusable at runtime (see metadata gap) → E2E drivers use LMSTUDIO_BASE_URL (OpenAI-wire).

## Verdict
See `FINAL_VERDICT.md` (issued after soak completion).
