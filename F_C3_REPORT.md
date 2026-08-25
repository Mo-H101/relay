# F-C3 Final Report: Spurious stream_error After Full Delivery

**Date:** 2026-08-25
**Status:** FIXED & VERIFIED
**Release blocker:** RESOLVED

> **Post-Codex Update:** F-C3 fix and regression tests are now committed and pushed
> (commit `8f9085b`). The F-4 finding from the same Codex review is also remediated
> (commit `70946d1`). This report retains original content for historical record.

---

## 1. Problem Summary

After a provider successfully delivered all content and emitted `[DONE]`, the relay
sometimes emitted a terminal `stream_error` SSE chunk. This happened when the upstream
provider closed the TCP connection immediately after `[DONE]`, causing
`response.aclose()` to raise `httpx.HTTPError`. The error propagated through the
`except httpx.HTTPError` handler as `ProviderHTTPError(0, ...)`, which the streaming
emitter interpreted as a failure — despite content being fully delivered.

## 2. Root Cause

In all four streaming methods in `app/providers/openai_compat_client.py`:

| Method | Line (pre-fix) | Type |
|---|---|---|
| `achat_stream_messages` | ~1409 | async, messages API |
| `achat_stream` | ~1193 | async, legacy completions |
| `chat_stream_messages` | ~950 | sync, messages API |
| `chat_stream` | ~813 | sync, legacy completions |

When `[DONE]` is received, the generator `break`s out of the line-reading loop.
However, the HTTP chunked transfer terminator is never drained. When the upstream
closes TCP immediately after `[DONE]`, `response.aclose()` (or `__exit__` of the
`httpx.stream()` context) raises `httpx.HTTPError`. That exception propagates to:

```python
except httpx.HTTPError as exc:
    raise ProviderHTTPError(0, redact_text(str(exc))) from exc
```

The caller `stream_generator()` in `app/api/openai.py:596` catches this via
`except Exception` and emits:

```python
yield {"type": "stream_error", "error": {...}}
```

...despite `success` never being set to `True` (it was skipped by the error path).

## 3. Fix Applied

**Minimal correct fix:** Added a `done_seen` flag to all four methods.

Pattern (identical in each):

```python
done_seen = False
try:
    # ... streaming loop ...
    for line in bounded_aiter_lines(response):
        if data_str.strip() == "[DONE]":
            done_seen = True
            break
        # ... yield chunks ...
except httpx.HTTPError as exc:
    if done_seen:
        return          # ← suppress cleanup noise after successful delivery
    raise ProviderHTTPError(0, ...) from exc
```

**Scope:** Only `app/providers/openai_compat_client.py` modified. No other
production files changed. No behavioral change for genuine errors (those occur
before `[DONE]` and are still raised).

## 4. Regression Tests

New file: `tests/test_fc3_stream_cleanup.py` (5 tests, all pass)

| Test | Scenario | Expected | Result |
|---|---|---|---|
| A | Clean stream, no errors | All chunks yielded | ✅ PASS |
| B | `[DONE]` received + `aclose()` raises | Error suppressed, chunks delivered | ✅ PASS |
| C | `httpx` error BEFORE `[DONE]` | `ProviderHTTPError` raised | ✅ PASS |
| D | `aclose()` error BEFORE `[DONE]` | Error propagates | ✅ PASS |
| E | Sync `chat_stream_messages`: same as B | Error suppressed | ✅ PASS |

## 5. Full Regression Suite

```
2891 passed, 8 skipped, 1 deselected (pre-existing Termux path issue)
```

Baseline: 2886 passed → 2886 + 5 new F-C3 tests = 2891. Zero regressions.

The single deselected test (`test_packaging.py::test_installed_cli_runs_from_arbitrary_cwd_with_stable_state`)
is a pre-existing failure caused by a Termux path resolution mismatch (expects
`/data/data/...` but gets `/storage/emulated/0/...`). Unrelated to F-C3.

## 6. Diff Review

`git diff -w` shows only 12 added lines across 4 methods in `openai_compat_client.py`:
4 × `done_seen = False`, 4 × `done_seen = True`, 4 × `if done_seen: return`.
No other production file changes. The 53-file working-tree modifications are
pre-existing whitespace normalization (zero content diff with `-w`).

## 7. E2E Reproduction Note

The race is extremely narrow and non-deterministic. 16/16 negative from Chaos L2,
55/55 negative from Step 2 reproduction scripts. The fix is based on proven
code-path analysis, not deterministic reproduction. The regression tests verify
the fix at the unit level by injecting the exact failure mode.

## 8. Recommendation

F-C3 is resolved. All release blockers cleared. Ready for release tagging.
