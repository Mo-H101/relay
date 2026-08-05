# P4.3 Plan — Platform Stabilization: Provider Parity, Conformance Tests, and the TUI Ops-Feed Defect

Status: **Planning only. No code in this phase.** This document is the design for
P4.3. Implementation is a separate phase that follows this plan and requires
approval of the decisions called out below.

Scope relationship: P4.2 (Ollama `132d1c4`, Anthropic `70162e3`, Gemini `6c69aef`)
made all six registry providers runtime-ready. P4.3 is the stabilization pass that
makes the provider platform production-ready and safe to extend: it audits provider
consistency, fixes the one pre-existing failing test, and adds the developer
surface (authoring guide + conformance suite) for adding providers safely.

The audit bounded itself by the same rules as P4.2: no `app/api/` changes, no
persistence/state changes, no `PROJECT_LOG.md` changes, and no UI code changes
beyond the TUI defect fix recommended here. This phase adds **no runtime code** —
it inspects, documents, and plans.

---

## 1. Current state after P4.2

The runtime is registry-driven (`app/providers/registry.py` is the single source of
truth; `app/providers/factory.py::build_runtime_provider` builds every provider from
its `ProviderDefinition`). All six providers are in `RUNTIME_READY`.

| Provider | Registry id | Client | `RUNTIME_READY` | Runtime priority |
|---|---|---|---|---|
| NVIDIA | `nvidia` | `NvidiaClient(OpenAICompatibleClient)` | ✅ yes | 10 |
| OpenAI | `openai` | `OpenAIClient(OpenAICompatibleClient)` | ✅ yes | 5 |
| Anthropic | `anthropic` | `AnthropicClient` (native) | ✅ yes | 8 |
| Google Gemini | `gemini` | `GeminiClient` (native) | ✅ yes | 7 |
| LM Studio | `lmstudio` | `LMStudioClient(OpenAICompatibleClient)` | ✅ yes | 1 |
| Ollama | `ollama` | `OllamaClient` (native) | ✅ yes | 2 |

Menu order: `nvidia, openai, anthropic, gemini, lmstudio, ollama`. No other
providers have registry entries. OpenRouter/Groq keys are **reserved-future**
config only (documented in `docs/configuration.md` as "parsed but unused"); the
`openrouter` string in `tests/test_setup_wizard.py` is a wizard-test fixture id,
not a registry entry.

Every client implements the full uniform surface, verified by inspection:

- All four chat surfaces share one signature across clients:
  `chat(provider, model, message, temperature, top_p, max_tokens, stop,
  frequency_penalty, presence_penalty, seed) -> str`, plus
  `chat_messages(provider, payload)`, `chat_stream`, `chat_stream_messages`, and
  the async twins `achat`/`achat_messages`/`achat_stream`/`achat_stream_messages`.
- Natives implement `list_models`, `key_check`, `probe_model` (sync) and
  `alist_models`, `aprobe_model` (async); OpenAI-compatible adds `check_model`
  (see G5).
- `proxy_request_kwargs` is available on every provider; `connectivity_probe`
  exists on Anthropic and Gemini.

Test baseline for P4.3 planning: **1365 passed, 7 skipped, 1 failed**. The sole
failure is the pre-existing TUI `DuplicateKey` defect (Focus C) — order-dependent,
passes in isolation, fails in full-suite runs.

## 2. Focus A — Provider consistency audit

### 2.1 Parity matrix (6 providers × 11 dimensions)

| Dimension | nvidia | openai | lmstudio | anthropic | gemini | ollama |
|---|---|---|---|---|---|---|
| Sync chat (`chat`) | ✅ verbatim | ✅ verbatim | ✅ verbatim | ✅ | ✅ | ✅ |
| Async chat (`achat`) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Messages API (`chat_messages`/`achat_messages`) | ✅ verbatim | ✅ verbatim | ✅ verbatim | ✅ translated | ✅ translated | ✅ translated |
| Streaming (sync+async, messages variants) | ✅ verbatim SSE | ✅ verbatim SSE | ✅ verbatim SSE | ✅ SSE content-block | ✅ SSE `streamGenerateContent` | ✅ NDJSON |
| Tool calls | ✅ verbatim | ✅ verbatim | ✅ verbatim | ✅ → `"tool_calls"` | ⚠️ `functionCall` finishes as `"stop"` (see G7) | ✅ translated |
| Usage reporting | ⚠️ passthrough only (see G2) | ⚠️ passthrough only | ⚠️ passthrough only | ✅ input/output mapped | ✅ `promptTokenCount`/`candidatesTokenCount` mapped, stream deduped | ✅ `prompt_eval_count`/`eval_count` mapped |
| Errors (`ProviderHTTPError`, `ProviderTimeout`, redaction, Retry-After) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Retries (service-level, provider-agnostic) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Health checks (`connectivity_probe`) | ⚠️ generic GET fallback (G3) | ⚠️ generic GET fallback | ⚠️ generic GET fallback | ✅ `x-api-key`+version probe | ✅ `?key=` probe | ⚠️ generic GET fallback (keyless, correct today) |
| Proxy support (`proxy_request_kwargs`) | ⚠️ module function only (G4) | ⚠️ module function only | ⚠️ module function only | ✅ method | ✅ method | ✅ method |
| Model discovery (`list_models`/`alist_models`, prefix normalization) | ✅ | ✅ | ✅ | ✅ | ✅ (`models/` strip) | ✅ (`/api/tags`) |

### 2.2 Concrete divergences found (G1–G7)

- **G1 — Parameter forwarding diverges silently.** `seed` is forwarded by
  OpenAI-compatible (verbatim), Gemini (`config["seed"]`), and Ollama
  (`options["seed"]`), but **dropped by Anthropic** (accepted in the signature at
  `anthropic_client.py:635,804,998,1093`, never mapped to the payload).
  `frequency_penalty`/`presence_penalty` are forwarded by OpenAI-compatible but
  **dropped by Anthropic, Gemini, and Ollama** (their APIs do not support them).
  Ollama documents the drop in its module docstring; Anthropic/Gemini do not.
  Recommendation: codify a provider capability matrix (which params are forwarded
  vs. documented-drop) in the docs and assert the drops in conformance tests. No
  runtime code change — the current behavior is intended, it is just silent.
- **G2 — Streaming usage asymmetry.** OpenAI-compatible providers pass upstream
  usage through verbatim and only report it when the upstream emits it (requires
  `stream_options: {include_usage: true}`); the three native providers always
  synthesize a terminal usage chunk. Consequence: `/v1` streaming consumers get
  usage for 5/6 providers but not nvidia/openai/lmstudio by default. Options
  (decide in execution): **O1 (recommended)** inject
  `stream_options={"include_usage": true}` into OpenAI-compatible upstream payloads
  when the caller did not set it — additive, internal wire change, keeps the API
  contract; **O2** document the asymmetry and leave passthrough.
- **G3 — `connectivity_probe` asymmetry.** Implemented only by Anthropic
  (`anthropic_client.py:486`) and Gemini (`gemini_client.py:613`); OpenAI-compatible
  and Ollama rely on the generic Bearer GET fallback in
  `app/services/health_checker.py:169–194`. Functionally correct today (verified by
  health tests), but the auth-convention knowledge is split across two files.
  Recommendation: add `connectivity_probe` to `OpenAICompatibleClient` (Bearer) and
  `OllamaClient` (keyless) for surface symmetry — purely additive, no behavior
  change — and assert every provider has a working connectivity path in conformance.
- **G4 — `proxy_request_kwargs` naming asymmetry.** It is a method on the three
  native clients (delegating to the shared module function) but only a module-level
  function for OpenAI-compatible (`openai_compat_client.py:146`), which
  `health_checker.py` imports directly. All six providers have functional proxy
  support. Recommendation: add a thin `proxy_request_kwargs` method on
  `OpenAICompatibleClient` delegating to the module function so every client exposes
  the same surface. Additive; `health_checker` keeps its module import.
- **G5 — `check_model` orphan.** Exists only on `OpenAICompatibleClient`
  (`openai_compat_client.py:521`), is unused by app code (grep: no callers), and is
  tested only by `test_openai_compat_client.py:298`. It is not part of the native
  surface. Recommendation: exclude it from the conformance "uniform surface"
  contract (canonical surface is `list_models`/`probe_model`/`key_check`); decide
  in execution whether to keep it as legacy or remove it as dead code.
- **G6 — `user` field divergence.** Anthropic maps `user` → `metadata.user_id`;
  OpenAI-compatible passes it verbatim; Gemini and Ollama drop it. Recommendation:
  record in the capability matrix and assert forwarded-or-documented-drop in
  conformance.
- **G7 — Gemini tool-call `finish_reason`.** Gemini maps a `functionCall`
  `finishReason` of `STOP` to `finish_reason "stop"` rather than a tool marker;
  Anthropic maps tool-use to `"tool_calls"`. Not a blocker (callers receive the
  tool call in the message), but it is a shape divergence. Recommendation: note in
  the capability matrix; cover in conformance tool-call round-trip where supported.

## 3. Focus B — Remaining provider gaps

- **No runtime gaps:** all six providers are `RUNTIME_READY` with the full surface
  and no `NotImplementedError` on routing paths.
- **Reserved-future config (not bugs):** `groq_api_key`
  (`app/core/config.py:319`) and OpenRouter key handling are documented as reserved
  and unused; no registry entries or clients exist. P4.3 keeps them as-is; the
  authoring guide (Focus D) documents how a future provider (OpenRouter, Groq) is
  added as a registry entry + client + `RUNTIME_READY` promotion.
- **Legacy wrapper shims:** `app/providers/{nvidia,openai,lmstudio}.py` are thin
  `create_provider()` wrappers over `build_runtime_provider`. App code never imports
  them (verified by grep); only tests do (`test_provider_factory.py`,
  `test_nvidia_provider.py`, `test_lmstudio_provider.py`, `test_lmstudio_real.py`).
  Recommendation: migrate the tests onto the registry/factory path and delete the
  shims, or explicitly document them as legacy. Decision point for execution.

## 4. Focus C — Pre-existing TUI `DuplicateKey` defect

**Root cause (inspected):** `app/ui/data.py::ops_tail(limit)` builds `OpsEventView`s
without a unique key (lines 779–801), and
`app/ui/screens/diagnostics.py::_refresh_ops_table` (lines 107–126) passes the
event object as the Textual `DataTable` row `key` after `table.clear(columns=True)`.
When two ops events occur in the same second, the object-as-key hashes/equals by
value (or `age_seconds` collides), raising `DuplicateKey`. The failure surfaces on
`on_mount → _refresh_all → _refresh_ops_table` (`diagnostics.py:71`). It is
order-dependent: `test_ui_app.py::test_boots_to_dashboard_and_walks_all_tabs`
passes in isolation but fails in full-suite runs (the baseline's only failure).

**Fix recommendation (for P4.3 execution):** derive a unique, monotonic row key for
each `OpsEventView` (e.g., an integer sequence or `(timestamp, index)`) instead of
passing the event object — or a collision-prone `age_seconds` — as the `DataTable`
row key. Scope: `app/ui/screens/diagnostics.py` (row key), optionally
`app/ui/data.py` (expose a stable key on the view). Verification: full-suite run
must show the test passing in both isolation and full-suite order.

**Decision point:** fix in P4.3 (recommended) — it is the last red test and blocks
an all-green gate.

## 5. Focus D — Developer experience for adding providers

**Gap:** `docs/` has platform plans (P1–P4.2), readiness, architecture,
known-limitations, deployment, and configuration docs, but **no provider-authoring
guide or template**; adding a provider today requires reading the registry, a
client, and the tests to reconstruct the contract.

**Deliverables (docs only):**

- **`docs/provider-authoring-guide.md`** — the contract for adding a provider:
  - Registry entry checklist: `ProviderDefinition` fields, `priority_env`
    (`<NAME>_MODEL_PRIORITY` — consistent across all six today, verified),
    optional `priority_attr` (only `lmstudio_priority` uses it), `key_attr`,
    default base URL, `runtime_priority` ordering guidance.
  - Client surface contract: the uniform chat signatures (all four surfaces,
    sync+async), messages translation, stream chunk shape, `proxy_request_kwargs`
    (method), `connectivity_probe`, `list_models`/`alist_models`/`probe_model`/
    `key_check` — no `NotImplementedError` on routing paths before promotion.
  - Error/redaction conventions: `ProviderHTTPError(status_code, message,
    retry_after)`, `ProviderTimeout`, `safe_error_body`, `_stream_error_text`,
    `_retry_after_seconds`.
  - Capability matrix: per-parameter forward vs. documented-drop, usage-reporting
    shape, tool-call mapping — so a new provider either forwards or documents a
    drop, never drops silently.
  - Test requirements: must pass the conformance suite (Focus E) plus per-provider
    wire tests before `RUNTIME_READY` promotion.
  - Registry-driven notes: routing picks the provider up automatically once its id
    is in `RUNTIME_READY`; no `app/api/` changes needed; wizard deferral messaging
    is registry-driven and disappears automatically.
- **`docs/provider-capability-matrix.md`** (or folded into the guide) — the
  single source of truth for G1/G6/G7 and the G2 usage behavior, kept in sync with
  the conformance suite assertions.

## 6. Focus E — Provider conformance tests

**Proposal:** a provider-agnostic parametrized suite (`tests/test_provider_conformance.py`)
that parametrizes over registry entries (or `RUNTIME_READY`) and asserts, per
provider via its client and a mock wire server:

1. **Surface presence:** all of `chat`, `chat_stream`, `chat_messages`,
   `chat_stream_messages`, `achat`, `achat_stream`, `achat_messages`,
   `achat_stream_messages`, `list_models`, `alist_models`, `probe_model`,
   `aprobe_model`, `key_check`, `proxy_request_kwargs` — no `NotImplementedError`
   on the routing surface.
2. **Sync/async parity:** `chat` produces the same result as `achat` on identical
   mock responses.
3. **Messages shape:** `chat_messages` returns a string; OpenAI-compatible payload
   translates to native wire format and parses back.
4. **Stream chunk shape:** chunks are OpenAI-shaped (`delta.content`,
   `finish_reason`, terminal marker), no malformed chunks.
5. **Tool-call round-trip:** where supported, `functionCall`/tool-use maps to
   `"tool_calls"` (skip providers without tool support).
6. **Usage reporting:** sync and stream produce `prompt_tokens`/
   `completion_tokens`/`total_tokens` with correct per-provider mapping; Gemini
   stream usage deduped (`_GeminiStreamState.usage_emitted`).
7. **Errors:** `ProviderHTTPError`/`ProviderTimeout` on HTTP/timeout failures;
   bodies redacted; `retry_after` parsed.
8. **Proxy kwargs:** `proxy_request_kwargs` honors `provider.proxy` + `NO_PROXY`
   semantics for every provider.
9. **Connectivity:** every provider has a working connectivity path — either a
   `connectivity_probe` (G3, after the additive change) or the generic fallback.
10. **Discovery:** `list_models`/`alist_models` + prefix normalization where
    applicable.
11. **Registry invariants:** extend `tests/test_provider_registry.py` to gate
    `RUNTIME_READY` promotion on conformance pass.

**Implementation notes:** reuse `tests/conformance_helpers.py` (threaded
`MockOpenAIProvider`, OpenAI-wire) for the three OpenAI-compatible providers; the
three natives need per-provider mock wire servers — reuse the HTTP-mock patterns
already used in `tests/test_anthropic_runtime.py`, `tests/test_gemini_runtime.py`,
`tests/test_ollama_runtime.py`. Build providers via `build_runtime_provider` +
per-provider fixtures so the suite stays registry-driven. The capability matrix
(§5) encodes the documented-drops asserted by the suite.

## 7. Acceptance gates

- **All-green full suite:** 1365 passed, 7 skipped, **0 failed** after the TUI fix
  (Focus C), including `test_ui_app.py` passing in full-suite order.
- **Conformance suite passes for all six providers** (mock-based, no network).
- **`git diff` touches only:** docs (authoring guide, capability matrix, this
  plan), the TUI row-key fix, conformance tests, and the approved additive surface
  changes (G3 probes, G4 method, G2 include_usage if O1 approved). Never
  `app/api/`, persistence/state, or `PROJECT_LOG.md`.
- **No wire regression:** for the three OpenAI-compatible providers, wire behavior
  stays byte-identical unless the G2/O1 `include_usage` injection lands, in which
  case only the `stream_options` key is added and asserted by a conformance test.

## 8. Out of scope (explicitly)

- Display-name-keyed → registry-id migration of stores/API/persisted state
  (deferred from P4.1, still pending renumbering).
- New providers (OpenRouter, Groq) — documentation only, no registry/client work.
- Changing the OpenAI-compatible `/v1` or relay hot-path wire behavior beyond the
  additive G2/O1 `stream_options` injection (if approved).
- UI changes beyond the Focus C row-key fix.

## 9. Files expected to change during implementation

- `docs/provider-authoring-guide.md` (new), `docs/provider-capability-matrix.md`
  (new, or folded into the guide).
- `app/ui/screens/diagnostics.py` (+ optionally `app/ui/data.py`) — Focus C fix.
- `app/providers/openai_compat_client.py` — additive `proxy_request_kwargs` method
  (G4), `connectivity_probe` (G3), optional `include_usage` injection (G2/O1);
  keep-or-remove `check_model` (G5, decision).
- `app/providers/ollama_client.py` — `connectivity_probe` (G3, optional symmetry).
- `app/providers/{nvidia,openai,lmstudio}.py` — delete or document as legacy
  (decision, §3).
- `tests/test_provider_conformance.py` (new); `tests/test_provider_registry.py`
  extended; per-provider runtime tests extended.
- `PROJECT_LOG.md` updated only when the phase is implemented, not during planning.

## 10. Risks

- **Capability-matrix drift:** a new param or wire behavior on one provider
  diverges silently from others. Mitigation: conformance asserts documented-drops,
  and the matrix is the single source of truth kept in sync with the suite.
- **G2/O1 `include_usage` injection:** if an OpenAI-compatible upstream rejects
  `stream_options`, `/v1` streaming breaks for nvidia/openai/lmstudio. Mitigation:
  gate with a conformance test (before/after using `MockOpenAIProvider`), inject
  only when the caller did not set it, keep it additive.
- **Focus C fix regressing table rendering:** verify in full-suite order, not just
  isolation (the defect is order-dependent by nature).
- **Legacy-shim deletion breaking test imports:** migrate `test_provider_factory.py`,
  `test_nvidia_provider.py`, `test_lmstudio_*` onto the factory path before deleting.
- **G3/G4 additive changes:** keep them purely additive; existing health tests and
  proxy tests must pass unchanged.
