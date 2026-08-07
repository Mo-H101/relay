# P4.2 Plan — Enable the Remaining Runtime Providers (Ollama → Anthropic → Gemini)

Status: **Planning only. No code in this phase.** This document is the design for
P4.2. Implementation is a separate phase that follows this plan.

Scope relationship note: `docs/platform-p4-plan.md` §8 originally described P4.2 as
migrating display-name-keyed surfaces (stores, API payloads, persisted state) to the
provider registry ids. This plan **supersedes that description** per product intent:
P4.2 is the enabling of the remaining providers into `RUNTIME_READY` while keeping
API contracts and persisted state unchanged. The old §8 id-migration work is
explicitly **out of scope** here and is not renumbered; it is deferred until after
the three providers ship.

---

## 1. Implemented providers (current state after P4.1)

The runtime is registry-driven (`app/providers/registry.py` is the single source of
truth; `app/providers/factory.py::build_runtime_provider` builds every provider from
its `ProviderDefinition`). Six providers are defined and guarded by
`EXPECTED_IDS = ["nvidia", "openai", "anthropic", "gemini", "lmstudio", "ollama"]`
(`tests/test_provider_registry.py`).

| Provider | Registry id | Client | `RUNTIME_READY` | Runtime priority |
|---|---|---|---|---|
| NVIDIA | `nvidia` | `NvidiaClient(OpenAICompatibleClient)` | ✅ yes | 10 |
| OpenAI | `openai` | `OpenAIClient(OpenAICompatibleClient)` | ✅ yes | 5 |
| Anthropic | `anthropic` | `AnthropicClient` (native, async-first) | ❌ no | 8 |
| Google Gemini | `gemini` | `GeminiClient` (native, async-first) | ❌ no | 7 |
| LM Studio | `lmstudio` | `LMStudioClient(OpenAICompatibleClient)` | ✅ yes | 1 |
| Ollama | `ollama` | `OllamaClient` (native, async-first) | ❌ no | 2 |

`RUNTIME_READY = {"nvidia", "openai", "lmstudio"}` in `app/providers/registry.py`.

Implemented client surface today:

- `OpenAICompatibleClient` (`app/providers/openai_compat_client.py`) owns the
  complete runtime surface used by nvidia/openai/lmstudio:
  `chat`, `chat_messages`, `chat_stream`, `chat_stream_messages`, `list_models`,
  `probe_model`, `key_check`, `achat`, `achat_messages`, `achat_stream`,
  `achat_stream_messages`, `alist_models`, `aprobe_model`, `proxy_request_kwargs`.
- `AnthropicClient`, `GeminiClient`, `OllamaClient` implement the async-first
  surface: `achat`, `achat_stream`, `alist_models`, `aprobe_model`, plus
  `list_models` (sync). Their sync `chat*` methods and `chat_messages` raise
  `NotImplementedError` by design. These three already pass async client tests in
  `tests/test_async_provider_clients.py` (~lines 571–703).

No other providers have registry entries or clients. Any future provider
(OpenRouter, Groq, etc.) starts with a registry `ProviderDefinition` + client and is
out of scope for P4.2.

## 2. Client-compatible but not `RUNTIME_READY`

**Anthropic, Gemini, and Ollama** are client-compatible (their clients exist, are
wired into the registry, are tested asynchronously, and the wizard/setup flow
accepts them) but are not enabled for chat routing. Today:

- The setup wizard configures them and `app/setup/wizard.py:255–277` defers them
  with: *"not wired into chat routing yet; that lands in a later phase."*
- `build_runtime_provider` would already build them (base URL, key, discovery,
  priority) if their ids were added to `RUNTIME_READY`.
- `HealthRefresher` iterates `provider_manager.enabled()` and the health checker is
  provider-agnostic, so enabling a provider pulls it into health automatically.

What makes them *not* ready (the P4.2 client gaps):

1. **Message-payload chat surface missing** — the OpenAI-compatible `/v1` endpoint
   (`app/api/openai.py`) and the sync `Relay.chat` path
   (`app/core/relay.py:237` → `ChatService.chat_across` → `client.chat_messages`)
   require `chat_messages`/`chat_stream_messages` and
   `achat_messages`/`achat_stream_messages`. None of the three native clients
   implement these. Because `/v1/chat/completions` builds candidates from
   `provider_manager.all()` filtered by `model in p.models` (`app/api/openai.py:206`),
   enabling any of these providers without the messages surface would cause an
   `AttributeError` for a matching model id (e.g. a `claude-…` or `ollama/…` request).
2. **Sync chat surface missing** — `chat`, `chat_stream`, `chat_messages`,
   `chat_stream_messages` raise `NotImplementedError`. The TUI and `Relay.chat`
   use the sync path, so those surfaces must exist before a provider can route
   traffic there.
3. **`proxy_request_kwargs` missing** — the relay hot path forwards proxy headers
   only when a client supports `proxy_request_kwargs`; native clients don't have it.
4. **Health connectivity auth mismatch** — the generic connectivity probe in
   `app/services/health_checker.py` does `httpx.get(base_url + health_endpoint)`
   with a `Bearer` header only. That is correct for OpenAI-compatible providers and
   for Ollama (keyless) but wrong for Anthropic (requires `x-api-key` +
   `anthropic-version: 2023-06-01`) and Gemini (key passed as `?key=` query
   parameter). Without a fix, Anthropic/Gemini would be marked UNAVAILABLE by the
   health checker even with a valid key, and health-aware routing would never
   select them.

## 3. Enablement order

The three providers ship in this order, one at a time, each landing with its own
tests and gate (see §6). Ordering rationale across API compatibility, testing
difficulty, user value, and reliability:

| Rank | Provider | API compatibility | Testing difficulty | User value | Reliability | Why this rank |
|---|---|---|---|---|---|---|
| 1 | **Ollama** | High (OpenAI-shaped `/v1`-ish NDJSON, keyless) | Low (keyless, local; mock HTTP server covers everything; `test_lmstudio_real.py` is a live-template) | High (private, free, offline-capable local inference) | Local — depends on the user's own server | Smallest surface change, keyless (no secret handling), best coverage reuse; delivers private-local value first |
| 2 | **Anthropic** | Medium (native SSE, content-block events; standard `x-api-key`) | Medium (clean, well-documented API; mockable with no live key) | High (Claude quality, strong tool use) | High (mature cloud API) | Highest-reliability cloud provider; simpler than Gemini (no query-key/`models/`-prefix quirks); enabled only when a key is configured, so no default-route risk |
| 3 | **Gemini** | Lower (API key as query param, `:streamGenerateContent?alt=sse`, model id `models/` prefix stripping) | Higher (streaming parse + discovery quirks) | High (free tier, large context) | Medium-high (free-tier variability) | Most surface area / quirks, so last; safest to land after the enabling machinery is proven twice |

Rule applied: **a provider is only enabled after its full client surface
(§2.1–2.3), its health connectivity fix (§2.4), and its config fields (§4) land
together with tests.** No provider is added to `RUNTIME_READY` incrementally with
missing surface.

## 4. Per-provider work

Cross-cutting groundwork first (shared by all three, lands once):

- **C.1 Message-payload surface.** Add `chat_messages`, `chat_stream_messages`,
  `achat_messages`, `achat_stream_messages` to each native client, translating the
  OpenAI-compatible message payload to the native wire format. Implement as a small
  shared payload-builder module (`app/providers/message_payloads.py` or per-client
  private helpers) so each client keeps one translation path. The `/v1` candidate
  filter (`app/api/openai.py`) needs no change once this lands.
- **C.2 Sync chat surface.** Implement sync `chat`/`chat_stream` for each native
  client (either via `urllib3`/blocking `httpx` as in `OpenAICompatibleClient`, or
  by running the async path on a fresh loop thread — match the pattern already used
  by `OpenAICompatibleClient`). Removes `NotImplementedError` from the runtime
  surface.
- **C.3 Proxy support.** Implement `proxy_request_kwargs` on each native client,
  with behavior matching the OpenAI-compatible client.
- **C.4 Health connectivity auth.** Extend `HealthChecker` so connectivity probing
  uses the provider's key convention. Proposed: add an optional
  `client.connectivity_probe(provider)` used by `HealthChecker` when present,
  falling back to the current generic Bearer GET for OpenAI-compatible providers.
  Anthropic sends `x-api-key` + `anthropic-version`; Gemini sends `?key=`; Ollama
  stays keyless. Health model probing needs no change (`is_chat_testable` already
  gates it).
- **C.5 Config priority fields.** Add `anthropic_model_priority`,
  `gemini_model_priority`, `ollama_model_priority` settings (parse
  `ANTHROPIC_MODEL_PRIORITY`, `GEMINI_MODEL_PRIORITY`, `OLLAMA_MODEL_PRIORITY`) and
  wire the corresponding `priority_env` from the registry. This makes the
  `factory.py` priority override (`build_runtime_provider:56`) and reload
  `_load_providers` priority refresh effective for the new providers. (Priority
  `priority_attr` remains optional; `runtime_priority` suffices.)

Per-provider matrix:

| Item | Ollama | Anthropic | Gemini |
|---|---|---|---|
| Client work | C.1–C.3 | C.1–C.3 | C.1–C.3 |
| Config (exists) | `ollama_enabled`, `ollama_base_url` | `anthropic_enabled`, `anthropic_api_key`, `anthropic_base_url` | `gemini_enabled`, `gemini_api_key`, `gemini_base_url` |
| Config (new) | `ollama_model_priority` (C.5) | `anthropic_model_priority` (C.5) | `gemini_model_priority` (C.5) |
| Secrets | none (keyless) | `ANTHROPIC_API_KEY` (already wired, `key_attr`) | `GEMINI_API_KEY` (already wired, `key_attr`) |
| Streaming | `achat_stream` ✅ (NDJSON); `chat_stream`/`achat_stream_messages`/`chat_stream_messages` via C.1–C.2 | `achat_stream` ✅ (SSE content-block); rest via C.1–C.2 | `achat_stream` ✅ (`streamGenerateContent?alt=sse`); rest via C.1–C.2 |
| Discovery | `list_models`/`alist_models` ✅ (`/api/tags`) | ✅ (`/models`) | ✅ (`/models?key=…`, strips `models/` prefix) |
| Connectivity (health) | ✅ works today (keyless GET `/api/tags`) | ❌ needs C.4 (`x-api-key` + version header) | ❌ needs C.4 (`?key=` query param) |
| Default base URL | registry default | registry default | registry default |
| Tests | see §6 | see §6 | see §6 |
| Live smoke | optional `test_ollama_real.py` (mirror `test_lmstudio_real.py`, env-gated) | optional env-gated live test | optional env-gated live test |

Wizard note: the deferral messaging in `app/setup/wizard.py` is registry-driven
(`deferred = [pid for pid in provider_ids if pid not in RUNTIME_READY]`), so once a
provider's id is in `RUNTIME_READY` the "not wired yet" notice disappears for it
automatically — no wizard code change required. This should be verified per provider
in its gate.

## 5. API contracts and persistence stay unchanged

- **API contracts: no changes.** No endpoint, route, request/response schema, or
  error shape changes. `app/api/` is untouched by P4.2. Enabling a provider only
  adds a *candidate* to existing routing; the `/v1/chat/completions`, `/chat`,
  `/health`, `/providers`, `/models` surfaces behave identically. The one behavior
  change is observable-only: models from the newly enabled provider appear in model
  listings once discovered and enabled.
- **Persistence: no changes.** `state_store`/`state_flusher` and the persisted
  provider-state payloads are untouched. New provider entries that appear in
  persisted state use the existing display-name-keyed structure (same as
  nvidia/openai/lmstudio today). The §8 id-migration is explicitly out of scope
  (see scope note at the top).
- **No regression in existing providers.** Wire behavior, headers, and payloads for
  nvidia/openai/lmstudio remain byte-identical (they share the OpenAI-compatible
  client; P4.2 does not touch that client).

## 6. Acceptance gates

Shared baseline gate (must hold at the end of P4.2, matching the current baseline):

- Full test suite: **1226 passed, 7 skipped, 1 failed** — the single failure is the
  pre-existing TUI `DuplicateKey` in
  `test_ui_app.py::test_boots_to_dashboard_and_walks_all_tabs`
  (`app/ui/screens/diagnostics.py:71`). P4.2 must not add failures or change that
  pre-existing one.
- `git diff` against the P4.2 branch point touches **only** provider clients,
  health connectivity, config, and tests — nothing under `app/api/`,
  `app/core/state_store.py`, `app/core/state_flusher.py`, or the UI.

Per-provider gate (all required before that provider's id joins `RUNTIME_READY`):

- **Surface parity:** all of `chat`, `chat_stream`, `chat_messages`,
  `chat_stream_messages`, `achat`, `achat_stream`, `achat_messages`,
  `achat_stream_messages`, `list_models`, `alist_models`, `probe_model`,
  `aprobe_model`, `key_check`, `proxy_request_kwargs` implemented with no
  `NotImplementedError` on the routing surface.
- **Payload translation tests:** for each of `chat_messages`/`achat_messages` and
  both stream variants, a mock-HTTP test asserting the OpenAI-compatible message
  payload is translated to the native wire format and responses parse back (add to
  `tests/test_async_provider_clients.py` / a new sync-parity test module).
- **Sync/async parity:** the sync `chat*` methods produce the same result as
  `achat*` on mock responses (mirror existing parity test structure).
- **Proxy test:** `proxy_request_kwargs` forwards proxy headers like
  `OpenAICompatibleClient`.
- **Health connectivity test:** health checker reports the provider AVAILABLE with
  a mocked endpoint using the correct auth convention (C.4), and UNKNOWN/
  UNAVAILABLE without a key where one is required.
- **Config test:** new `*_model_priority` field parses and drives discovery
  priority via `factory.py`; reload auto-includes the provider's fields
  (`*_enabled`, `*_api_key`, `*_model_priority`).
- **Registry/route test:** adding the id to `RUNTIME_READY` yields a fully
  functional provider via `build_runtime_provider`, appears in
  `provider_manager.enabled()`, in `/v1` candidates for its discovered model ids,
  and in `/chat` sync + async routing (mock-based end-to-end).
- **Wizard test:** setup for the provider no longer emits the "not wired into chat
  routing yet" deferral.
- **Optional live smoke:** env-gated live test mirroring
  `tests/test_lmstudio_real.py` (Ollama is the natural first candidate since it is
  local and keyless).

## 7. Out of scope (explicitly)

- Display-name-keyed → registry-id migration of stores/API/persisted state
  (former P4.1 plan §8). Deferred; renumbered later.
- New providers beyond the six registry entries (e.g. OpenRouter, Groq).
- Changing the OpenAI-compatible `/v1` or relay hot-path wire behavior.
- Persisted-state schema changes.

## 8. Files expected to change during implementation

- `app/providers/{anthropic,gemini,ollama}_client.py` — full chat surface, proxy,
  connectivity probe.
- `app/services/health_checker.py` — C.4 connectivity auth (additive, fallback
  preserved).
- `app/core/config.py` — C.5 `*_model_priority` fields.
- `app/providers/registry.py` — promote ids into `RUNTIME_READY` (one per gate).
- `app/providers/message_payloads.py` (new) — shared native payload translation.
- `tests/` — parity, translation, proxy, connectivity, config, registry, wizard,
  and end-to-end tests; optional `test_ollama_real.py`.
- New/changed docs for the shipped phase; `PROJECT_LOG.md` updated only when the
  phase is implemented, not during planning.

## 9. Risks

- **Streaming event parse drift:** native SSE/NDJSON formats vary (Anthropic
  content-block deltas, Gemini `streamGenerateContent`, Ollama NDJSON). Mitigation:
  translation tests per format and reuse of the existing `achat_stream` parsing that
  is already covered in `test_async_provider_clients.py`.
- **Health-check auth regression:** C.4 must keep the existing Bearer path intact so
  OpenAI-compatible providers behave identically (covered by existing health tests).
- **`/v1` breakage if enabled with missing surface:** the per-provider gate requires
  the full messages surface before `RUNTIME_READY` promotion; the `/v1` candidate
  path is unchanged by design.
- **Priority ordering surprise:** Anthropic (`runtime_priority=8`) and Gemini (7)
  outrank OpenAI (5) once enabled. This is intended (higher quality defaults), but
  the `*_model_priority` fields give operators explicit control; call it out in the
  release notes.
