# P4.3 Phase 4 — Implementation Plan: Provider Conformance Test Suite (Focus E)

Status: **Phase-4 planning only. No code yet.** Detailed implementation plan for the
approved Focus E work item (Provider Conformance Test Suite). Implementation starts
only after this plan is approved.

Source: `docs/platform-p4.3-plan.md` §6 (Focus E). Approved decisions applied: G2/G3/G4
done; keep `check_model` and the legacy shims; changes incremental with a commit per
logical phase; no `app/api/`, persistence/state, or `PROJECT_LOG.md` changes; Focus D
(authoring guide + docs capability matrix) remains deferred.

Constraints (user-mandated): **no code in this phase — plan only**; no `PROJECT_LOG.md`;
no API contract changes; stop after the plan and wait for approval.

## 1. Current provider architecture (inspection findings)

### 1.1 Registry as the single source of truth

`app/providers/registry.py` defines six `ProviderDefinition`s in `PROVIDER_REGISTRY`
(nvidia, openai, anthropic, gemini, lmstudio, ollama), all six in `RUNTIME_READY`
(`registry.py:200-207`). Each entry carries `client_class`, `build_provider()`, and
`client()` — the uniform construction path. `app/providers/factory.py`
`build_runtime_provider()` builds runtime `Provider` objects from settings +
`list_models` discovery. `app/services/client_registry.py` maps provider ids (and
legacy names) to client instances for the chat services.

### 1.2 Client surface (the contract to enforce)

Four concrete client classes back the six providers; the three OpenAI-compatible
providers share one implementation via ~10-line subclasses:

| Provider(s) | Client class |
|---|---|
| nvidia, openai, lmstudio | `OpenAICompatibleClient` (+ `NvidiaClient`/`OpenAIClient`/`LMStudioClient` thin subclasses) |
| ollama | `OllamaClient` |
| anthropic | `AnthropicClient` |
| gemini | `GeminiClient` |

All four expose the same method surface (verified by repo grep):

- Sync: `chat`, `chat_messages`, `chat_stream`, `chat_stream_messages`,
  `list_models`, `key_check`, `probe_model`, `proxy_request_kwargs`,
  `connectivity_probe`
- Async: `achat`, `achat_messages`, `achat_stream`, `achat_stream_messages`,
  `alist_models`, `aprobe_model`
- Optional: `check_model` — **only on `OpenAICompatibleClient`** (G5: keep)

No `NotImplementedError` stubs exist on any runtime path; the surface is real but
only partially guarded by tests today.

### 1.3 Wire families and auth conventions

| Provider | wire family | chat endpoint | discovery endpoint | auth |
|---|---|---|---|---|
| nvidia/openai/lmstudio | `openai` | POST `{base}/chat/completions` | GET `{base}/models` | `Authorization: Bearer` iff `has_api_key()` |
| ollama | `ollama` | POST `{base}/api/chat` | GET `{base}/api/tags` | none (keyless) |
| anthropic | `anthropic` | POST `{base}/messages` | GET `{base}/models` | `x-api-key` + `anthropic-version: 2023-06-01` |
| gemini | `gemini` | POST `{base}/models/{model}:generateContent` (stream: `?alt=sse`) | GET `{base}/models` | `?key=<api_key>` query param, no headers |

### 1.4 Return-shape contract (verified)

- `chat`/`achat` → `str` (assistant text).
- `chat_messages`/`achat_messages` → OpenAI-shaped `dict`
  (`choices[0].message.{content|tool_calls}`, `finish_reason`, optional `usage`).
  OpenAI-wire providers forward the payload **verbatim**; natives translate
  OpenAI→native wire and back.
- `chat_stream`/`achat_stream` → `Generator[str]` / `AsyncIterator[str]` of content
  delta strings.
- `chat_stream_messages`/`achat_stream_messages` → OpenAI-shaped chunk `dict`s
  (`delta.content`, `delta.tool_calls`, terminal `finish_reason`, optional `usage`
  chunk).
- `list_models`/`alist_models` → `List[str]`; `probe_model`/`aprobe_model` →
  `ModelProbe`; `connectivity_probe` → `(ok: bool, details: str, latency_ms: int)`;
  `proxy_request_kwargs` → `dict`.
- Usage always mapped to `prompt_tokens`/`completion_tokens`/`total_tokens`
  (Ollama `_ollama_usage` 100, Anthropic `_anthropic_usage` 273, Gemini
  `_gemini_usage` 357; OpenAI passthrough).

### 1.5 Error, timeout, retry contract (verified)

- `httpx.TimeoutException`/`ReadTimeout` → `ProviderTimeout`
  (`app/providers/exceptions.py`).
- HTTP ≥ 400 → `ProviderHTTPError(status_code, message, retry_after)` with the body
  bounded + redacted (`_safe_provider_body` / `safe_error_body`; key stripped).
- `Retry-After` parsed via `_retry_after_seconds` (`openai_compat_client.py:81-117`)
  and threaded into `retry_after` in all four clients.
- `failure_classifier.classify` (`app/services/failure_classifier.py:44-72`) reads
  exactly `status_code`/`message`/`retry_after` to drive retry/failover — the
  conformance suite must guarantee these attributes exist and map correctly.
- Timeouts: chat/stream/messages surfaces use `settings.request_timeout`;
  `list_models` uses `30`; `probe_model` and `connectivity_probe` use `10`
  (consistent across all four clients, verified by grep).

### 1.6 Known divergences the matrix must encode (documented-drops, not bugs to fix here)

1. **G1 gen-params drop:** Anthropic/Gemini/Ollama silently drop `seed`,
   `frequency_penalty`, `presence_penalty` (native payload never contains them);
   OpenAI-compatible forwards them.
2. **G7 tool-call finish_reason:** Gemini emits `finish_reason="stop"` for
   tool-call responses (`_gemini_finish_reason("STOP")` → `"stop"`,
   `gemini_client.py:343-354,424-441`). Ollama hardcodes `"stop"` even when
   `tool_calls` present (`ollama_client.py:136`). Anthropic maps `tool_use` →
   `"tool_calls"` (`anthropic_client.py:269`); OpenAI-compatible emits `"tool_calls"`.
3. **Discovery prefix normalization:** only Gemini strips the `models/` prefix
   (`_gemini_model_ids`, `gemini_client.py:327-340`).
4. **`check_model`:** OpenAI-compatible only.

### 1.7 Existing coverage (what NOT to duplicate)

- `tests/test_openai_conformance.py` — `/v1` API end-to-end OpenAI-wire conformance
  against the loopback `MockOpenAIProvider` (threaded server in
  `tests/conformance_helpers.py`). This layer stays as-is.
- `tests/test_{anthropic,gemini,ollama}_runtime.py` — deep native wire translation,
  auth, proxy, health, failover, reload tests using monkeypatched
  `httpx.post/get/stream` + `FakeResponse`/`FakeStreamResponse`/`_SpyAsyncClient`.
- `tests/test_provider_factory.py` — six-provider parametrized surface-symmetry tests
  for `proxy_request_kwargs` (P4.3.2) and `connectivity_probe` (P4.3.3).
- `tests/test_provider_registry.py` — registry invariants + setup-method presence.

Gap: no single provider-agnostic suite asserts the **uniform contract** across all six
providers (return shapes, sync/async parity, error/timeout/usage/tool mapping,
capability-gated behavior).

## 2. Goals

1. **Prevent provider drift** — a future change to one provider's client that breaks
   a shared contract fails the suite; the matrix pins documented-drops so silent
   divergence is caught.
2. **Make adding new providers predictable** — a new registry entry (with a client)
   is either in `RUNTIME_READY` and must pass the parametrized suite, or is excluded
   explicitly; the checklist of assertions is mechanical and registry-driven.
3. **Enforce the common provider contract** — one source of truth for surface,
   return shapes, auth, errors, timeouts, usage, tools, discovery, and
   connectivity/proxy behavior, expressed as parametrized tests gated by the
   capability matrix.

## 3. Providers covered

All six `RUNTIME_READY` providers, parametrized by registry id:

- nvidia, openai, lmstudio (wire family `openai`, shared `OpenAICompatibleClient`)
- ollama (wire `ollama`, `OllamaClient`)
- anthropic (wire `anthropic`, `AnthropicClient`)
- gemini (wire `gemini`, `GeminiClient`)

The suite parametrizes over `sorted(RUNTIME_READY)` and derives everything (client
class, wire family, auth, endpoints) from `PROVIDER_REGISTRY[provider_id]` plus the
capability matrix — never from hard-coded per-provider branches.

## 4. Required conformance areas (14) with concrete assertions

Each area is a test group; assertions run per-provider unless the matrix gates them.

1. **Provider registration** (extends `tests/test_provider_registry.py`)
   - `RUNTIME_READY == set(PROVIDER_REGISTRY)`; every entry has a `client_class`,
     `health_endpoint`, `base_url_default` (http/https), consistent kind/key fields.
   - `defn.build_provider()` round-trip: `id`, `name`, `base_url`, `health_endpoint`,
     `priority == runtime_priority`, `requires_api_key` match the definition.

2. **Runtime factory creation**
   - `defn.client()` returns the registered `client_class` instance.
   - `ClientRegistry().get(defn.id)` resolves to an equivalent client; legacy
     `get(defn.provider_name)` still resolves (backward-compat).
   - `build_runtime_provider(defn)` builds a `Provider` with `identity() == defn.id`;
     discovery failure yields empty `models`, never raises.

3. **Sync chat surface**
   - `chat(provider, model, message, ...)` returns a non-empty `str` on HTTP 200.
   - Auth header sent per convention (§1.3) — Bearer only when key present (keyless
     lmstudio/ollama send no `Authorization`).
   - HTTP 4xx/5xx → `ProviderHTTPError` with correct `status_code`; timeout →
     `ProviderTimeout`.
   - Request body sent to the correct endpoint with `model` and the message array.

4. **Async chat surface**
   - `achat` returns the same string as `chat` on identical mocked responses
     (sync/async parity).
   - Same parity for `achat_messages == chat_messages`,
     `achat_stream == chat_stream`, `achat_stream_messages == chat_stream_messages`,
     `alist_models == list_models`, `aprobe_model == probe_model`.

5. **Messages API**
   - `chat_messages` returns an OpenAI-shaped dict (`choices[0].message.content`,
     `finish_reason`); usage passthrough where the wire provides it.
   - OpenAI-wire providers: payload forwarded **verbatim** (message array, tools,
     tool_choice, stream_options unchanged).
   - Natives: OpenAI payload translates to the native wire and back — messages
     (system→systemInstruction for Gemini, tool role → tool_result/user for
     Anthropic/Ollama) round-trip without flattening; tool_call_id preserved.

6. **Streaming behavior**
   - `chat_stream` yields content delta strings and terminates.
   - `chat_stream_messages` yields OpenAI-shaped chunk dicts: `delta.content`,
     `delta.tool_calls` (where tool support), a terminal chunk with `finish_reason`,
     no malformed chunks (empty/malformed SSE lines skipped, never crash).
   - Mid-stream error → `ProviderHTTPError`; mid-stream read timeout →
     `ProviderTimeout`.

7. **Tool call translation**
   - With `tools` + `tool_choice` in the payload, the outgoing wire carries a native
     tool definition (OpenAI `tools` verbatim; Ollama `tools`; Anthropic `tools`
     blocks; Gemini `tools.functionDeclarations`) and the round-trip returns OpenAI
     `tool_calls` with `id`/`type="function"`/`function.name`/`function.arguments`.
   - Streaming tool-call deltas emitted for all six providers.
   - `tool_choice` honored per wire (auto/required/named where supported).

8. **Usage reporting**
   - Non-stream: `usage.prompt_tokens`/`completion_tokens`/`total_tokens` present and
     correctly mapped from native counts (Ollama `prompt_eval_count`/`eval_count`,
     Anthropic `input_tokens`/`output_tokens`, Gemini `promptTokenCount`/
     `candidatesTokenCount`, OpenAI passthrough); `total == prompt + completion`.
   - Stream: a usage chunk is emitted **at most once** (Gemini `usage_emitted` dedupe;
     OpenAI passthrough when upstream sends one; Ollama from the `done` line;
     Anthropic from the message-final usage).

9. **Error mapping**
   - 4xx/5xx → `ProviderHTTPError`; `status_code` preserved; 401/403/429/500
     distinguishable via `classify()`.
   - Provider body redacted: an API key echoed in a 500 body must not appear in the
     exception message or response text.
   - Malformed/non-JSON response bodies map to an error, not a crash.

10. **Timeout behavior**
    - `httpx.TimeoutException`/`ReadTimeout` → `ProviderTimeout` on chat, stream
      (incl. mid-stream), messages, and discovery.
    - Timeout kwargs are forwarded: `settings.request_timeout` on chat/stream/
      messages surfaces, `30` on `list_models`, `10` on `probe_model` and
      `connectivity_probe` (capture `timeout=` in mocked `httpx` calls).

11. **Retry/failover compatibility**
    - Exceptions expose the attributes `failure_classifier.classify` consumes
      (`status_code`, `message`, `retry_after`) and classify to the documented
      `FailureKind` (429→rate_limit, 401/403→auth_error, ≥500→server_error,
      timeout→timeout).
    - `Retry-After` header parsed into `retry_after` on error responses that carry it.
    - Client-level failover: a provider returning HTTP 500 (then 200) plus a second
      provider succeed across a `chat_across`/`achat_across` attempt sequence
      (mirrors `TestMessagePathFailover` at the client level, per wire family).

12. **connectivity_probe**
    - `(ok, details, latency_ms)` shape; `ok` True only for HTTP 200; `details ==
      f"HTTP {code}"`; `latency_ms` is `int`.
    - Auth per convention; URL = `base_url.rstrip("/") + health_endpoint`;
      `timeout=10`; `**proxy_request_kwargs(...)`.
    - Every exception path returns `(False, str(exc), ms)` — never raises; keys never
      appear in `details`.

13. **proxy_request_kwargs**
    - `provider.proxy is None` → defers to global proxy handling; `""` → bypass;
      a URL → forces that proxy.
    - `NO_PROXY` matching (exact host, suffix, wildcard) respected
      (`_matches_no_proxy`, `openai_compat_client.py:118-144`).
    - OpenAI-compatible: method result equals the module-level `proxy_request_kwargs`
      result (parity, already asserted in P4.3.2 — conformance keeps it).

14. **Model discovery**
    - `list_models` parses each wire's discovery body into `List[str]` from the right
      endpoint (`/models`, `/api/tags`, `:listModels` for Gemini).
    - Gemini strips the `models/` prefix; other providers return ids verbatim
      (matrix-driven expectation).
    - Discovery HTTP error → `ProviderHTTPError`; timeout → `ProviderTimeout`;
      `alist_models` matches `list_models`.

## 5. Test architecture

### 5.1 New test file layout

- `tests/test_provider_conformance.py` (new) — the provider-agnostic parametrized
  suite. Test classes map 1:1 to the 14 areas.
- `tests/conformance_helpers.py` (extend) — add the capability matrix, the
  wire-fixture builder, the parametrized `runtime_provider` fixture, and the
  sync/async fake-http install helpers. No production code changes.
- `tests/test_provider_registry.py` (extend) — registry/factory conformance
  assertions (areas 1-2) so non-runtime registry invariants stay with the existing
  file.

### 5.2 Shared fixtures

1. `runtime_provider` — parametrized over `sorted(RUNTIME_READY)`; yields
   `(provider_id, defn, client, provider)` where `provider = defn.build_provider(
   api_key="sk-test", base_url=defn.base_url_default)` (plus a keyless variant for
   auth-sensitive tests). All construction via the registry — the suite stays
   registry-driven.
2. `http_mocks(wire)` — installs the wire-family fake HTTP layer (see §5.4) and
   returns a request recorder (method, url, headers, json, kwargs) so assertions can
   inspect exactly what each client sends.
3. `matrix` — the capability matrix (see §5.3); tests read capability via a small
   `cap(provider_id, key)` helper.
4. `async_mock` — pytest-asyncio loop-scoped async client fakes
   (`asyncio_default_fixture_loop_scope = "function"` already configured); reuses the
   `_SpyAsyncClient` pattern from the runtime suites.

### 5.3 Provider capability matrix (single source of truth for the suite)

In `tests/conformance_helpers.py`, a dict keyed by provider id:

| key | nvidia/openai/lmstudio | ollama | anthropic | gemini |
|---|---|---|---|---|
| `wire` | `openai` | `ollama` | `anthropic` | `gemini` |
| `auth` | `bearer` | `none` | `x-api-key` | `query` |
| `tools` | True | True | True | True |
| `stream_usage` | True | True | True | True |
| `check_model` | True | False | False | False |
| `gen_params` (seed/penalties passthrough) | True | False (documented drop) | False (documented drop) | False (documented drop) |
| `discovery_normalize` (`models/` strip) | False | False | False | True |
| `tool_finish_reason` | `tool_calls` | `stop` | `tool_calls` | `stop` |
| `chat_forwarded_verbatim` | True | False | False | False |
| `discovery_endpoint` | `/models` | `/api/tags` | `/models` | `/models` |

Tests never hard-code per-provider expectations; they read the matrix. The matrix
**is** the conformance contract for documented-drops (G1, G7, prefix normalization,
`check_model`).

### 5.4 Mock strategy

Three layers, chosen to be fast, hermetic (loopback only / monkeypatch only), and
consistent with existing coverage:

1. **OpenAI /v1 end-to-end (unchanged):** the threaded loopback
   `MockOpenAIProvider` in `tests/conformance_helpers.py` continues to serve
   `tests/test_openai_conformance.py`; the new suite does **not** duplicate it.
2. **Standardized wire fixtures for all six providers (the conformance harness):**
   a `wire_fixtures(wire)` helper in `conformance_helpers.py` returns per-wire fake
   builders driven by the matrix, monkeypatching the exact httpx entry points each
   client uses (`httpx.post`, `httpx.get`, `httpx.stream`, and `httpx.AsyncClient` for
   the async surface). The fakes reuse the proven `FakeResponse`/
   `FakeStreamResponse`/`_SpyAsyncClient` patterns already in the runtime suites.
   Scenario builders:
   - `chat_ok(content, usage, tool_calls, finish_reason)`
   - `chat_stream_lines([...])` (per-wire framing: OpenAI SSE, Ollama NDJSON,
     Anthropic SSE events, Gemini SSE frames)
   - `chat_error(status, message, retry_after=None)` (echo-capable body for the
     redaction assertion)
   - `models_response([...])` (per-wire discovery body; Gemini emits `models/`
     prefixes)
   - `install_http_mocks(wire, handlers)` patches sync+async entry points and records
     every request (url, headers, json, `timeout=` kwarg).
   This makes the whole suite uniform: test logic is written once per scenario and
   only the wire helper differs per provider.
3. **Native wire detail tests (unchanged):** the per-provider runtime suites keep
   their deep translation coverage; the conformance suite asserts the *contract* on
   top, not a second copy of every translation edge case.

Rationale: the uniform monkeypatch harness is deterministic, fast, and lets the same
assertion run across all six providers with identical failure signals; the existing
loopback + runtime suites already cover byte-level wire fidelity.

### 5.5 Parametrization approach

- Primary axis: `pytest.mark.parametrize("provider_id", sorted(RUNTIME_READY))` via
  the `runtime_provider` fixture.
- Secondary axis (used sparingly): wire family (`openai`/`ollama`/`anthropic`/
  `gemini`) for tests that assert family-specific wire shapes; expressed through the
  matrix, not separate test bodies.
- Capability-gated tests use `pytest.param(provider_id, marks=skipif)` so skips are
  visible in the report and stay stable (see §5.6).
- Sync/async parity tests share a scenario helper that runs the same scripted wire
  against the sync and async client methods and compares results.

### 5.6 Expected skips for unsupported native features

Skips are **only** capability-absence skips, each keyed to a matrix entry:

- `check_model` surface test → skipped for ollama/anthropic/gemini
  (`matrix["check_model"] is False`). Expected **3 skips**.
- G1 gen-params: no skip — two matrix-driven assertions instead: providers with
  `gen_params=True` must forward seed/penalties; providers with `gen_params=False`
  must **not** include them in the native payload (documented-drop asserted, not
  skipped).
- G7 tool finish_reason: no skip — assert `finish_reason ==
  matrix["tool_finish_reason"]` per provider.
- `chat_forwarded_verbatim` vs native-translate: no skip — matrix-driven branch.

Baseline suite is **1396 passed, 7 skipped, 0 failed** (post-P4.3.3). Target after
this phase: **0 failed**, **10 skipped** (7 existing + 3 `check_model` skips), plus
the new conformance tests passing. Exact new-test count (~150-250 parametrized
instances across ~40 test functions) is confirmed during implementation; the suite
must stay under ~5s so the full suite remains quick.

## 6. Acceptance criteria

1. **Existing providers pass without behavior changes** — the conformance suite is
   green against today's code with **no production changes**. Any genuine defect the
   suite surfaces is either already a documented drop (encoded in the matrix) or
   recorded as a follow-up phase — **not fixed here** (no behavior changes allowed).
2. **No API contract changes** — zero edits to `app/`; the diff touches only tests +
   `tests/conformance_helpers.py`.
3. **No persistence/state changes** — none; `PROJECT_LOG.md` untouched.
4. **All six providers pass** the parametrized suite (mock-based, no external
   network; loopback/monkeypatch only).
5. **Full suite green**: previous 1396 passed, 7 skipped + new conformance tests,
   **0 failed**, skip count exactly 7 + 3 documented `check_model` skips (reconciled
   in the run).
6. **`git diff` limited to** `tests/test_provider_conformance.py` (new),
   `tests/conformance_helpers.py`, `tests/test_provider_registry.py`, and this plan.
   Never `app/`, `app/api/`, persistence/state, or `PROJECT_LOG.md`.
7. **Async parity locked**: sync/async methods produce identical results on
   identical scripted wires for all six providers.

## 7. Files expected to change

Change (tests only):

- `tests/conformance_helpers.py` — capability matrix, `wire_fixtures(wire)`,
  `install_http_mocks`, sync/async fake-HTTP install, `runtime_provider` fixture.
- `tests/test_provider_registry.py` — areas 1-2 (registry invariants, factory/registry
  client identity, `RUNTIME_READY` gate on surface presence).
- `tests/test_provider_conformance.py` — **new**; the 14-area parametrized suite.

Explicitly **not** changed: all of `app/providers/`, `app/services/`, `app/api/`,
`app/core/config.py`, persistence/state, `PROJECT_LOG.md`, and the existing
per-provider runtime suites (`test_{anthropic,gemini,ollama}_runtime.py`,
`test_openai_conformance.py`) except where a test must be deleted/migrated because it
duplicates a conformance assertion (checked during implementation; preference is to
leave existing tests untouched).

Docs: this phase produces no documentation deliverables (Focus D authoring guide and
docs capability matrix remain deferred). The test-side matrix (§5.3) is the
capability contract for now.

## 8. Implementation order

1. Extend `tests/conformance_helpers.py`: matrix + wire fixtures + mock install +
   `runtime_provider` fixture.
2. Stand up `tests/test_provider_conformance.py` skeleton: surface presence (areas 1-2
   helpers), sync/async parity helpers, and the streaming/error/usage/tool harness.
3. Add the 14 area test groups, matrix-driven; run per-provider so failures pinpoint
   the offending provider.
4. Extend `tests/test_provider_registry.py` for areas 1-2.
5. Reconcile skip count; run affected files, then the full suite.

Verification commands:

1. `python -m pytest tests/test_provider_conformance.py -q` — all six providers.
2. `python -m pytest tests/test_provider_registry.py tests/conformance_helpers.py -q`
   (helpers are fixtures; conftest-style).
3. `python -m pytest tests -q` → 1396 + new passing, **0 failed**, 7 + 3 = 10 skipped
   (reconciled).
4. `git diff --stat` → only the three test files + this plan.

## 9. Risks and compatibility concerns

- **Suite churn / slow suite:** 250+ parametrized instances risk slowing the run.
  Mitigation: uniform harness with a single HTTP-patch per test, matrix-driven
  scenarios, target <5s for the conformance file.
- **Duplicate coverage with runtime suites:** mitigated by explicitly *not*
  re-testing native wire translation edges; the conformance suite asserts the
  contract, runtime suites assert translation fidelity.
- **Gemini/G7 divergence:** the suite must assert the matrix value (`stop`), not
  `tool_calls`, or it fails today. The matrix documents it; G7 itself stays a
  follow-up (out of scope — no behavior changes).
- **`check_model` asymmetry:** handled as a capability-gated skip, never a failure.
- **Async fakes drift from sync fakes:** parity tests run both against the same
  wire builder, so a divergence fails loudly on both axes.
- **Scope-creep guard:** any surfaced defect is documented as a follow-up, not fixed;
  no `app/` edits, no API/persistence/`PROJECT_LOG.md` changes.

## 10. Commit

Single logical commit after all acceptance criteria hold:

`test: add provider conformance suite gating the runtime provider contract (P4.3.4)`

Staged files only: `tests/test_provider_conformance.py` (new),
`tests/test_provider_registry.py`, `tests/conformance_helpers.py`, and this plan if
requested. The other untracked plan files (`docs/platform-p4.2-plan.md`,
`docs/platform-p4.3-phase1/2/3-plan.md`) remain untracked as before.
