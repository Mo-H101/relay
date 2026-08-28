# N-17 Handoff — Native Provider Streaming Terminal-Signal Enforcement

**Date:** 2026-08-28
**Performed by:** OpenCode (big-pickle)
**Status:** COMPLETE (implementation + tests + verification done) — **NOT yet committed/pushed**
**Purpose:** Clean transfer point for phone-side OpenCode to continue from the laptop state.

> This is a **standalone handoff file** for the N-17 remediation. The existing
> `HANDOFF_REPORT.md` / `FINAL_VERDICT.md` / `PROGRESS.md` release-state docs were
> intentionally **not** modified (out of scope for this pass).

---

## 1. Repository / Git state (current, safe transfer point)

- **Branch:** `master`
- **HEAD:** `ee9cffdda0cb1c7a272d6c2ff60b157450297224`
- **origin/master:** `ee9cffdda0cb1c7a272d6c2ff60b157450297224` (== HEAD, clean baseline)
- **Working tree:** N-17 changes present but **uncommitted** (see §5).
- Git history has **NOT** been altered (no commit / push / reset / clean / stash / amend).

---

## 2. N-17 design decision (approved)

A native provider streaming method must treat a **clean EOF that never carried the
provider's required wire-level terminal signal** as a **failure**, never as success.
The **raw native provider signal** is used for terminal detection — even in the
`*_messages` variants (a translated OpenAI `finish_reason` is **not** a substitute for
the provider's actual wire terminal).

Per-provider required terminal signal:

| Provider   | Required terminal                                   |
|------------|------------------------------------------------------|
| Anthropic  | SSE `message_stop` event                             |
| Gemini     | a candidate carrying the terminal `finishReason`     |
| Ollama     | NDJSON `done: true` marker                           |

On missing terminal at EOF: record provider outcome status **0** (network band — so the
operation is **never** recorded as HTTP 200 success) and raise the existing
`ProviderHTTPError(0, ...)` via the standard provider-error path.

---

## 3. Affected providers / methods (all 12)

Each provider guards all 4 stream methods:

- `chat_stream` (sync, single-message text)
- `achat_stream` (async, single-message text)
- `chat_stream_messages` (sync, full-payload)
- `achat_stream_messages` (async, full-payload)

| Provider   | Guard var          | Helper (messages paths)                      |
|------------|--------------------|----------------------------------------------|
| Anthropic  | `message_stop_seen`| `_line_is_message_stop(line)`                |
| Gemini     | `finish_reason_seen`| `_line_has_gemini_finish_reason(line)`      |
| Ollama     | `done_seen`        | `_line_is_ollama_done(line)`                 |

12 guard sites confirmed present (4 per provider):
`app/providers/anthropic_client.py:899,1010,1251,1451`
`app/providers/gemini_client.py:1003,1107,1334,1520`
`app/providers/ollama_client.py:661,763,989,1173`

---

## 4. Files changed

### Source (3)
- `app/providers/anthropic_client.py` (+89)
- `app/providers/gemini_client.py` (+94)
- `app/providers/ollama_client.py` (+87)
  - adds `_TRUNCATED_STREAM_MSG_*`, native-terminal helper, and the 4 guarded
    methods per provider.

### Tests — fixtures made protocol-complete (4)
Existing tests whose fixtures were complete streams missing the native terminal
signal were updated (NOT weakened; the production checks are unchanged):

- `tests/test_async_provider_clients.py` (+8/-1)
  - anthropic `achat_stream` skip-non-text: added `message_stop`
  - gemini `achat_stream` endpoint/yields-text + skip-empty-chunks: added `finishReason`
  - ollama `achat_stream` skip-malformed: added `done: true`
- `tests/test_gemini_runtime.py` (+8/-1)
  - `chat_stream_yields_sse_deltas`: added `finishReason`
  - `chat_stream_messages_skips_metadata_lines`: added terminal `finishReason` candidate,
    len 2→3, `finish_reason == "stop"` assertion
- `tests/test_ollama_runtime.py` (+4/-1)
  - `chat_stream_messages_skips_malformed_lines`: added `done: true`, len 1→2,
    `finish_reason == "stop"` assertion
- `tests/test_provider_json_and_stream_errors.py` (+2)
  - anthropic `chat_stream` normal-stream: added `message_stop`

### Tests — new regression suite (1, untracked)
- `tests/test_n17_native_streaming_terminal.py` (new) — 48 tests:
  - 24 success cases (terminal present → content yielded)
  - 24 truncated-EOF failure cases (content + clean EOF, no terminal → `ProviderHTTPError`),
    asserting `status="network" == 1` and `status="success" == 0` (no false HTTP 200),
    covering sync + async for all 12 methods across 3 providers.

No unrelated files or findings (N-18, N-19, CI, packaging pinning) were touched.

---

## 5. Verification results (all on laptop, current state)

- **N-17 focused suite:** `tests/test_n17_native_streaming_terminal.py` → **48 passed**
- **Mutation testing:** for each provider, the 4 terminal guards were temporarily
  bypassed (`if not <x>_seen:` → `if False`). In every case the N-17 truncation tests
  failed with **"DID NOT RAISE ProviderHTTPError"** (8 failures per provider). Originals
  fully restored afterward and the focused suite re-passes. → **Mutation is caught.**
- **Affected provider/streaming tests:** `test_async_provider_clients.py`,
  `test_anthropic_runtime.py`, `test_gemini_runtime.py`, `test_ollama_runtime.py` →
  **180 passed**; broader streaming/service/conformance set → **399 passed, 3 skipped**.
- **Full suite:** **3139 passed, 20 skipped, 0 failures**
- **Packaging:** `tests/test_packaging.py` → **29 passed**
- **`pip check`:** → **No broken requirements found**
- **Compile:** `python -m compileall -q app` → OK; per-file `py_compile` on all 3
  providers → OK
- **CI:** **NOT VERIFIED on this laptop** (no GitHub Actions evidence; `gh` not
  authenticated). Do **not** claim CI passed.

---

## 6. Still pending (phone-side or later)

1. **Commit + push N-17 to `origin/master`** — NOT yet done. Working tree has the
   uncommitted N-17 changes. After committing and pushing, verify
   `HEAD == origin/master` and a clean tree.
2. **CI verification** for the new commit (requires GitHub Actions evidence).
3. Continuing release-readiness remediation (other findings: N-18, N-19, etc.) as
   directed.

---

## 7. Critical instructions for the next session

- The only source change in scope is **N-17**. Do **not** touch N-18 (OpenAI-compat
  post-DONE cleanup TimeoutException), N-19 (docs placeholder URL), CI config, or
  unrelated docs.
- Do **not** weaken the OpenAI-compatible E1 behavior
  (`app/providers/openai_compat_client.py`: `done_seen` / `_TRUNCATED_STREAM_MSG`).
- Do **not** weaken the new N-17 native terminal checks to make a fixture pass.
- Native terminal signal is detected from the **raw wire stream** in all variants;
  do not replace it with translated `finish_reason`.
- Commit message is your choice but should reference N-17. Do not amend.
