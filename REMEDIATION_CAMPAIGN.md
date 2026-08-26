# REMEDIATION CAMPAIGN — PERSISTENT STATE

STATUS: ACTIVE — DO NOT EXIT LOOP

## Objective
Find → Understand → Fix → Test → Verify → Re-scan → Repeat until the entire connected defect surface is exhausted and an independent final review confirms RELEASE-READY.

## Campaign State

| Field | Value |
|-------|-------|
| Current Iteration | 2 (beginning) |
| Current Commit | `6ef4c14` |
| Current Phase | INVESTIGATE — Iteration 2 pre-fix scanning complete, fixes identified |
| Tests Baseline | 3006 passed, 20 skipped, 0 failures |
| Loop Status | ACTIVE — DO NOT EXIT |

## Issues Discovered — This Campaign

| ID | Description | Severity | Status | Fix SHA |
|----|-------------|----------|--------|---------|
| N-10 | Malformed provider accounting bypasses commit() validation | Release-blocking | FIXED | `f5ae82c` |
| N-10-H | Metric increment unguarded — metrics failure breaks commit | Medium | FIXED | `1aabfb0` |
| N-10-G | No test for metric increment observability | Low | FIXED | `1aabfb0` |
| I1-EL | Event log emit failures have no log line (metric only) | Medium | FIXED | `6ef4c14` |
| I1-LL | LOG_LEVEL accepts any string, invalid values fail at runtime | Low | FIXED | `6ef4c14` |
| I2-H1 | Unguarded `response.json()` on 200 OK — unclassified error bypasses retry | High | OPEN | — |
| I2-P0a | Streaming error chunks use non-standard shape — crashes SDK parsers | High | OPEN | — |
| I2-H2 | Anthropic simple streams ignore in-stream error events | High | OPEN | — |
| I2-H3 | Gemini simple streams ignore in-stream error events | High | OPEN | — |

## Issues Investigated and Closed (Not Defects)

| ID | Concern | Evidence | Why Not a Defect |
|----|---------|----------|-----------------|
| I2-DI-M1 | update() enqueues durable turn.update outside lock | handoff.py:1010-1019; recovery at continuity_recovery.py:351-352 | Crash between in-memory mutation and enqueue leaves durable "denied" while in-memory shows updated. But: recovery rejects stranded "denied" rows; next `start()` re-hydrates from durable state. Transient divergence only, resolved on restart. Not a data loss or corruption path. |
| I2-DI-M2 | No retry path for dropped turn.append | handoff.py:890-898 vs continuity_flusher.py:137-147 | Flusher accepted but later dropped diverges in-memory from durable until restart. But: recovery re-hydrates from durable; in-memory committed_turns is ephemeral; no durable data loss. Transient divergence only. |
| I2-DI-M3 | Single-process single-writer assumption | state_store.py:17, conversation_store.py:101 | Design constraint documented at state_store.py:17. WAL + busy_timeout=5000 handles SQLITE_BUSY. Architecture is single-instance. Not a defect unless architecture changes. |
| I2-SC-M2 | retry_honor_retry_after defaults to False | config_spec.py:188 | Configurable option. Default immediate retry with backoff when `retry_backoff_base_seconds > 0`. Not a defect — it's a tuning choice. |
| I2-SC-M4 | EMPTY_RESPONSE not retryable | failure_classifier.py:26-31 | Intentional per chat_policy.py:88-98 docstring — empty response means content filter or provider choice, not transient failure. Failover to next candidate is the correct behavior. |
| I2-SC-L1 | Status 0 (connection failure) classified as UNKNOWN | failure_classifier.py:82 | Unknown is retryable, which is correct for transient DNS. Permanent DNS failures are rare and the budget system prevents infinite retries. Not a defect. |
| I2-SC-L3 | 529 (overloaded) classified as UNKNOWN | failure_classifier.py:82 | Functionally correct — UNKNOWN is retryable. Availability layer correctly treats 529 as degraded. Label is suboptimal but not a defect. |
| I2-API-P2a | _resolve_continuity_scope return type is a lie | app/api/openai.py:136-138 | Caller uses isinstance check. Works correctly. Type annotation is misleading but not a defect — no runtime impact. |
| I2-API-P2c | /v1/models advertises task categories as model IDs | app/api/openai.py:759-760 | Intentional design — task routing via model name. Well-documented in relay routing logic. Not a defect. |

## Issues Deferred (External Limitation)

| ID | Concern | Justification |
|----|---------|---------------|
| I2-SC-L4 | `_translate_gemini_line` iterates `data.get("candidates") or []` without list type check | Non-list `candidates` would iterate characters. Theoretical — Gemini API always returns a list. Not worth adding defensive type check for a provider-specific API contract. |

## Tests Added

| Test File | Tests | Iteration |
|-----------|-------|-----------|
| tests/test_n10_commit_accounting_validation.py | 26 tests | Pre-campaign |
| tests/test_event_log.py | 1 test (emit warning) | Iteration 1 |
| tests/test_config_spec.py | 2 tests (LOG_LEVEL validation) | Iteration 1 |

## Areas Re-scanned

| Area | Iteration | Result |
|------|-----------|--------|
| Provider client streaming parsers | 2 | Found I2-H2, I2-H3 |
| Provider client non-streaming error handling | 2 | Found I2-H1 |
| Error classification (failure_classifier) | 2 | Closed 3 findings as not defects |
| Data integrity / state transitions | 2 | Closed 3 findings as not defects |
| API contract / OpenAI compatibility | 2 | Found I2-P0a, closed 2 as not defects |
| Security | 1 | PASS |
| Concurrency | 1 | PASS |
| Configuration | 1 | Fixed I1-LL |
| Supply chain | 1 | PASS (CVE mitigated) |
| TODO/debt markers | 1 | PASS |

## Areas Still Requiring Investigation

- [ ] Post-fix re-scan after I2-H1, I2-P0a, I2-H2, I2-H3 fixes
- [ ] Async equivalents of all fixed sync paths (audit async variants)
- [ ] Test coverage gap analysis for all new fixes
- [ ] Ollama client — equivalent patterns not yet fully audited
- [ ] Azure provider client — not yet investigated
- [ ] Retry/cancellation interaction with streaming fixes
- [ ] Metrics/observability for new error paths
- [ ] Process lifecycle edge cases (startup/shutdown under load)
- [ ] Hot reload under concurrent load

## Known Risks

- CVE-2026-48710 in Starlette <1.0.1 — code mitigated via scope["path"], FastAPI upgrade recommended
- Time-dependent test flake pre-existing (not introduced by this campaign)

## Next Action

1. Update REMEDIATION_CAMPAIGN.md with Iteration 2 fix plan
2. Implement Fix I2-H1: Guard all `response.json()` calls in provider clients
3. Implement Fix I2-P0a: Fix streaming error chunk shape
4. Implement Fix I2-H2/H3: Add in-stream error detection for Anthropic/Gemini simple streams
5. Write regression tests for all three fixes
6. Run full test suite
7. Perform mandatory cascading re-scan
8. Continue loop
