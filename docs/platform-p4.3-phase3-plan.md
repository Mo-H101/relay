# P4.3 Phase 3 — Implementation Plan: `connectivity_probe` Interface Symmetry (G3)

Status: **Phase-3 planning only. No code yet.** Detailed implementation plan for the
approved G3 work item. Implementation starts only after this plan is approved.

Source: `docs/platform-p4.3-plan.md` §2.2 G3 (Focus A). Approved decisions applied:
G3 approve additive `connectivity_probe` symmetry across all runtime providers;
keep `check_model` and the legacy shims; changes incremental with a commit per
logical phase; no `app/api/`, persistence/state, or `PROJECT_LOG.md` changes.

## 1. Current connectivity behavior matrix

Connectivity probing flows through `HealthChecker._check_connectivity`
(`app/services/health_checker.py:165-206`): it resolves the provider's client and,
if that client exposes `connectivity_probe`, calls it; otherwise it falls back to a
generic Bearer GET.

| Provider | `connectivity_probe` today? | Who probes today | Auth convention | Endpoint | timeout | Return |
|---|---|---|---|---|---|---|
| nvidia | ❌ no | health_checker fallback | `Authorization: Bearer` iff key present | `base_url` + `/models` | 10 | `(ok, details, ms)` |
| openai | ❌ no | health_checker fallback | `Authorization: Bearer` iff key present | `base_url` + `/models` | 10 | `(ok, details, ms)` |
| lmstudio | ❌ no | health_checker fallback | `Authorization: Bearer` iff key present | `base_url` + `/models` | 10 | `(ok, details, ms)` |
| ollama | ❌ no | health_checker fallback | none (keyless) | `base_url` + `/api/tags` | 10 | `(ok, details, ms)` |
| anthropic | ✅ yes | client probe (`anthropic_client.py:486-509`) | `x-api-key` + `anthropic-version: 2023-06-01` headers | `base_url` + `/models` | 10 | `(ok, details, ms)` |
| gemini | ✅ yes | client probe (`gemini_client.py:613-638`) | `?key=<api_key>` query param, no headers | `base_url` + `/models` + `?key=` | 10 | `(ok, details, ms)` |

Shared semantics across the fallback and both native probes (verified):

- URL: `provider.base_url.rstrip("/") + provider.health_endpoint` (fallback and
  Anthropic/Gemini all do this; registry `health_endpoint` is `/models` for five
  providers and `/api/tags` for ollama — `registry.py:91-181`).
- `ok` is True **only** for HTTP 200.
- `details` = `f"HTTP {status_code}"` for any response; `str(exception)` on failure.
- `latency_ms` = integer elapsed milliseconds.
- Fixed `timeout=10`, and `**proxy_request_kwargs(provider, url)` on every request.
- Every exception path returns `(False, str(exc), ms)` — probes never raise.
- Keys and proxy URLs never appear in `details`/logs.

Key findings:

1. The generic fallback is **already correct** for the three OpenAI-compatible
   providers and for ollama (keyless). There is no behavior gap today.
2. The gap is **interface-shape only**: `connectivity_probe` is not exposed by
   `OpenAICompatibleClient` (nvidia/openai/lmstudio) or `OllamaClient`, so the
   auth-convention knowledge is split between the two native probes and
   `health_checker._check_connectivity`.
3. Dispatch is via `getattr(self._client_for(provider), "connectivity_probe", None)`
   (`health_checker.py:175`), so adding the method to the two remaining client
   types makes the checker call the client probe automatically — **no
   health_checker change is needed**.
4. The native Anthropic/Gemini probes have **no direct unit tests** (repo grep:
   `connectivity_probe` appears in tests only in docs); the health tests
   (`tests/test_health.py`) monkeypatch `_check_connectivity`. G3 fills that test
   gap too.

## 2. Proposed common interface

Required method signature (uniform across all six clients):

```python
def connectivity_probe(self, provider) -> tuple[bool, str, int]:
    ...
```

Contract (identical to today's fallback and native probes — a lift, not a change):

- **Return format:** `(ok: bool, details: str, latency_ms: int)`.
- **URL:** `provider.base_url.rstrip("/") + provider.health_endpoint` plus the
  provider's auth convention.
- **Ok rule:** `ok = response.status_code == 200`; `details = f"HTTP {code}"`.
- **Error handling:** catch all exceptions; return `(False, str(exc), latency_ms)`.
  Never raise.
- **Timeout:** fixed `timeout=10` (httpx), matching the fallback and both native
  probes.
- **Proxy:** `**proxy_request_kwargs(provider, url)` on every request.
- **Auth:** per provider convention (Bearer iff key for OpenAI-compatible, none for
  ollama, headers for Anthropic, query key for Gemini). Never log keys.

## 3. Provider-specific implementation strategy

- **NVIDIA / OpenAI / LM Studio** (`OpenAICompatibleClient`, inherited by all three
  subclasses): add `connectivity_probe` replicating the generic fallback
  byte-for-byte — Bearer header **iff** `provider.has_api_key()`, GET
  `base_url.rstrip("/") + provider.health_endpoint`, `timeout=10`,
  `**proxy_request_kwargs(...)`, tuple return with the same exception/`HTTP`/ms
  semantics. Explicitly **not** reusing `key_check`
  (`openai_compat_client.py:500-525`): it hardcodes `f"{base_url}/models"` (no
  `rstrip("/")`, ignores `health_endpoint`) and returns a different shape.
- **Ollama** (`OllamaClient`): add a keyless probe — GET
  `base_url.rstrip("/") + provider.health_endpoint` (registry `/api/tags`), no auth
  headers, `timeout=10`, proxy kwargs, same tuple semantics.
- **Anthropic** (`AnthropicClient`): existing probe is conformant — unchanged. Add
  direct tests.
- **Gemini** (`GeminiClient`): existing probe is conformant — unchanged. Add direct
  tests.
- **`health_checker.py`: no change.** Dispatch already selects the client probe when
  present; the fallback stays for legacy/unregistered providers. Because the new
  probes produce the same output as the fallback for the same inputs, `ProviderHealth`
  is unchanged.

## 4. Files affected

Change:

- `app/providers/openai_compat_client.py` — add `connectivity_probe` (additive).
- `app/providers/ollama_client.py` — add `connectivity_probe` (additive).

Tests:

- `tests/test_openai_compat_client.py` — probe auth/semantics tests.
- `tests/test_ollama_runtime.py` — keyless probe tests.
- `tests/test_anthropic_runtime.py`, `tests/test_gemini_runtime.py` — direct probe
  tests for the existing methods (fills the current test gap).
- `tests/test_provider_factory.py` — extend the six-provider surface-symmetry test
  (added in P4.3.2) to also assert `connectivity_probe` presence/callability/3-tuple.
- `tests/test_health.py` — `connectivity_probe` dispatch + fallback-preserved +
  behavior-identity tests (see §5).

Explicitly **not** changed: `app/services/health_checker.py`,
`app/providers/{base,registry,factory}.py`, `app/providers/anthropic_client.py`,
`app/providers/gemini_client.py` (code), `app/core/config.py`, `app/api/`,
persistence/state, `PROJECT_LOG.md`.

## 5. Test strategy

New tests (all mock-based, no network):

1. **Surface symmetry** — extend the existing parametrized test in
   `tests/test_provider_factory.py` (covers all six `RUNTIME_READY` ids): assert
   `hasattr(client, "connectivity_probe")`, `callable(...)`, and that calling it
   returns a 3-tuple of `(bool, str, int)`.
2. **Auth tests:**
   - OpenAI-compatible with key → captured `httpx.get` receives
     `Authorization: Bearer <key>`.
   - OpenAI-compatible without key (keyless path, e.g. LM Studio) → **no**
     Authorization header.
   - Ollama → no auth headers ever.
   - Anthropic → `x-api-key` + `anthropic-version` present (direct probe test).
   - Gemini → `?key=` present in the request URL and no Authorization header
     (direct probe test).
3. **Failure handling** — scripted responses:
   - HTTP 200 → `(True, "HTTP 200", ms)`.
   - HTTP 500/401/404 → `(False, "HTTP <code>", ms)`.
   - Connection exception → `(False, "<exc>", ms)`; assert the probe never raises.
4. **Timeout handling** — assert `timeout=10` is passed through to `httpx.get`
   (capture kwargs); simulate `httpx.TimeoutException` → `ok is False`,
   `details` is the exception text.
5. **Health checker integration** (`tests/test_health.py`):
   - Dispatch: a registered client exposing `connectivity_probe` → the probe is
     called and its result used (not the fallback).
   - Fallback preserved: an unregistered/legacy provider (no client) → the generic
     Bearer GET fallback still runs (backward-compat proof).
   - Behavior identity: for the same mocked response, the new
     `OpenAICompatibleClient.connectivity_probe` result equals the fallback result
     — proving the dispatch switch is behavior-neutral.
   - Existing health/health-refresher tests must pass unchanged.

Verification commands:

1. `python -m pytest tests/test_openai_compat_client.py tests/test_ollama_runtime.py tests/test_anthropic_runtime.py tests/test_gemini_runtime.py -q`
2. `python -m pytest tests/test_provider_factory.py tests/test_health.py -q`
3. Full suite: `python -m pytest tests -q` → previous 1377 passed, 7 skipped, plus
   the new passing tests, **0 failed**.

## 6. Risks and compatibility concerns

- **Dispatch-switch drift (primary risk):** once the method exists on the shared
  clients, the health checker stops using the fallback for those providers. This is
  safe only if the probes replicate the fallback exactly (URL `rstrip("/")` join,
  Bearer-only-if-key, `timeout=10`, proxy kwargs, 200-only, same tuple). Mitigation:
  the behavior-identity test (fallback vs probe on identical mocked responses) plus
  the existing health/health-refresher regression suite.
- **Keyless edge (lmstudio/ollama):** a probe must not send an Authorization header
  when the provider has no key — matching the fallback and preserving keyless
  semantics. Covered by auth tests.
- **URL join:** must use `rstrip("/") + health_endpoint` like the fallback; reusing
  `key_check`'s `f"{base_url}/models"` would change URL construction for base URLs
  with trailing slashes — the plan explicitly avoids it.
- **Gemini empty-key edge:** the existing probe already sends `?key=` when no key is
  set; unchanged, not in scope.
- **Legacy providers:** the fallback stays in `health_checker.py` for unregistered
  clients, so hand-built legacy providers behave exactly as today.
- **Scope-creep guard:** no health_checker refactor, no timeout-constant changes, no
  edits to the Anthropic/Gemini probe code, no API/persistence/UI changes, and
  `PROJECT_LOG.md` untouched.

## 7. Acceptance criteria

1. All six runtime clients expose `connectivity_probe(provider)` returning
   `(ok: bool, details: str, latency_ms: int)` and never raising.
2. Health behavior byte-identical: client probe == fallback for identical mocked
   responses; dispatch and fallback tests pass; all existing health/refresher tests
   pass unchanged.
3. Full suite green: **0 failed**, 7 skipped, 1377 + new passing tests.
4. `git diff` limited to `app/providers/{openai_compat_client,ollama_client}.py` and
   the five test files (§4); no `PROJECT_LOG.md`, `app/api/`, or
   persistence/state changes.

## 8. Commit

Single logical commit after all acceptance criteria hold:

`refactor: add connectivity_probe method to OpenAI-compatible and Ollama clients (P4.3.3)`

Staged files only: the two client files and the five test files listed in §4.
`docs/platform-p4.2-plan.md`, `docs/platform-p4.3-phase1-plan.md`, and
`docs/platform-p4.3-phase2-plan.md` remain untracked as before.
