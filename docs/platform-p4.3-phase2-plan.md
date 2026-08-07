# P4.3 Phase 2 — Implementation Plan: `proxy_request_kwargs` Interface Symmetry (G4)

Status: **Phase-2 planning only. No code yet.** Detailed implementation plan for the
approved G4 work item. Implementation starts only after this plan is approved.

Source: `docs/platform-p4.3-plan.md` §2.2 G4 (Focus A). Approved decisions applied:
G4 approve `proxy_request_kwargs` interface symmetry; keep `check_model` and the
legacy shims; changes incremental with a commit per logical phase; no `app/api/`,
persistence/state, or `PROJECT_LOG.md` changes.

## 1. Current proxy support matrix (all six `RUNTIME_READY` providers)

Proxy *behavior* is already byte-identical across every provider — all requests
route through the single module-level function
`proxy_request_kwargs(provider, url)` in `app/providers/openai_compat_client.py:146`
(behavior matrix in its docstring):

- `provider.proxy` is a URL → force that proxy, `trust_env=False`.
- `provider.proxy` is `""` → explicitly bypass, `trust_env=False, proxy=None`.
- `provider.proxy` is `None` and `PROXY_ENABLED=true` → scheme-specific
  `HTTP_PROXY`/`HTTPS_PROXY` selection from settings (env fallback), `NO_PROXY`
  honored (`_matches_no_proxy`), else httpx default `trust_env=True`.
- `provider.proxy` is `None` and `PROXY_ENABLED=false` → no proxy.

| Provider | Class | Instance `proxy_request_kwargs`? | How its requests get proxy kwargs |
|---|---|---|---|
| nvidia | `NvidiaClient(OpenAICompatibleClient)` | ❌ no — module fn only | `**proxy_request_kwargs(...)` at 17 call sites in the shared client |
| openai | `OpenAIClient(OpenAICompatibleClient)` | ❌ no — module fn only | same shared call sites |
| lmstudio | `LMStudioClient(OpenAICompatibleClient)` | ❌ no — module fn only | same shared call sites |
| ollama | `OllamaClient` | ✅ method → delegates to module fn | method + module fn |
| anthropic | `AnthropicClient` | ✅ method → delegates to module fn | method + module fn |
| gemini | `GeminiClient` | ✅ method → delegates to module fn | method + module fn |

## 2. Differences between providers

1. **Interface shape only.** The three native clients expose
   `proxy_request_kwargs(self, provider, url) -> dict` as an instance method
   (each a one-line delegation to the shared module function:
   `anthropic_client.py:480-484`, `gemini_client.py:607-611`,
   `ollama_client.py:321-325`). The three OpenAI-compatible providers expose **no
   instance method** — the shared class only uses the module function directly.
2. **No live caller depends on the method today** (verified by repo-wide grep):
   every request path in all four client files and `app/services/health_checker.py`
   uses the module function directly; there is **no** `hasattr(client,
   "proxy_request_kwargs")` duck-typing gate anywhere in `app/`.
3. **Consequence of the gap:** the P4.3 provider conformance suite (later phase)
   asserts `proxy_request_kwargs` as part of the uniform runtime surface, and the
   provider-authoring guide documents it as a required method. Duck-typed callers
   written in the future (or the conformance suite) would fail on the three
   OpenAI-compatible clients.
4. Signature is identical everywhere it exists: `(self, provider, url: str) -> dict`.

## 3. Proposed common interface

Add a thin instance method to `OpenAICompatibleClient` that mirrors the exact
pattern already proven in the three native clients:

```python
    def proxy_request_kwargs(self, provider: Provider, url: str) -> dict:
        """
        Compute httpx proxy kwargs, matching the OpenAI-compatible client.
        """
        return proxy_request_kwargs(provider, url)
```

Details:

- **Delegation, not duplication.** Inside the method body the name
  `proxy_request_kwargs` resolves to the module-level function (global scope), not
  the method — identical to how the three native clients already work. No recursion
  risk.
- **Inheritance covers all three.** `NvidiaClient`, `OpenAIClient`,
  `LMStudioClient` define no proxy method of their own, so they inherit the method
  automatically. All six runtime clients then expose the identical surface with
  identical results.
- **No existing call sites change.** The 17 internal `**proxy_request_kwargs(...)`
  call sites in `openai_compat_client.py` and the `health_checker.py` module import
  keep working unchanged. Minimal diff, zero behavior change.
- **`Provider` typing** matches the module function signature; `base.py` `proxy`
  field (default `None`) is untouched.

Rejected alternatives:

- **Replacing the module function with the method everywhere** — churns ~30 call
  sites across four client files plus `health_checker.py` and tests for no behavior
  benefit; higher regression surface.
- **Renaming either form** — breaks the existing public/tested function and the
  native methods for cosmetic symmetry.
- **Static/class method** — the module function already IS the shared stateless
  implementation; a classmethod wrapper adds indirection without value.

## 4. Files affected

Change:

- `app/providers/openai_compat_client.py` — add the one delegating method on
  `OpenAICompatibleClient` (additive; module function and all call sites untouched).

Tests:

- `tests/test_openai_compat_client.py` — add parity + delegation spot tests (see §5).
- `tests/test_provider_factory.py` (or a small parametrized block in
  `tests/test_proxy.py`) — add a six-provider surface-symmetry assertion.

Explicitly **not** changed: `app/providers/{base,registry,factory}.py`,
`app/services/health_checker.py`, the three native clients, `app/core/config.py`,
`app/api/`, persistence/state, `PROJECT_LOG.md`, and all existing tests.

## 5. Test strategy

New tests (all mock-based, no network):

1. **Parity test** (`tests/test_openai_compat_client.py`, mirroring the native
   `TestProxySupport::test_proxy_request_kwargs_matches_openai_compatible`):
   `OpenAICompatibleClient().proxy_request_kwargs(provider, url) ==
   proxy_request_kwargs(provider, url)`.
2. **Delegation spot tests** through the method, not just the module fn:
   - forced proxy (`provider.proxy = URL`) → `proxy == URL`, `trust_env is False`;
   - empty override (`provider.proxy = ""`) → `proxy is None`, `trust_env is False`;
   - `NO_PROXY` exact/suffix matching still honored through the method.
3. **Six-provider surface symmetry test** — parametrize over
   `PROVIDER_REGISTRY`/`RUNTIME_READY` ids, build each client via
   `build_runtime_provider` (or the registry's client factory), and assert:
   `hasattr(client, "proxy_request_kwargs")`, `callable(...)`, and that the result
   equals the module function for a sample `https://` URL. This locks the invariant
   the conformance suite will re-assert in a later phase.
4. **Backward-compat net (no edits):** `tests/test_proxy.py` (full behavior
   matrix), the native `TestProxySupport` classes, and the existing call-site tests
   must pass unchanged — proof that the change is behavior-neutral.

Verification commands:

1. `python -m pytest tests/test_openai_compat_client.py tests/test_proxy.py tests/test_provider_factory.py -q` — new + existing proxy tests green.
2. New symmetry test alone: `python -m pytest tests/test_provider_factory.py -q -k proxy` — pass.
3. Full suite: `python -m pytest tests -q` → previous 1367 passed, 7 skipped, plus
   the new passing tests, **0 failed**.

## 6. Risks and compatibility concerns

- **Method/function name shadowing:** `return proxy_request_kwargs(provider, url)`
  inside the method resolves to the module-level function (global scope). This exact
  pattern already ships in the three native clients, so the pattern is proven;
  a recursion test is unnecessary but the parity test guards delegation.
- **Behavioral drift:** delegation means zero logic duplication; the parity test
  (`method result == module fn result`) makes any future divergence a test failure.
- **Subclass interference:** none of the three OpenAI-compatible subclasses defines
  its own proxy method, so inheritance is uncontested. (Verified: no overrides.)
- **Regression surface:** the change is purely additive; all existing proxy matrix,
  call-site, and native proxy tests are the regression net and must pass untouched.
- **Out-of-scope creep guard:** this phase only adds the method. The conformance
  suite and docs (authoring guide / capability matrix) remain later phases; G3
  `connectivity_probe` symmetry is a separate phase.

## 7. Acceptance criteria

1. All six runtime providers expose a callable, inherited-or-native
   `proxy_request_kwargs(provider, url)` returning the module function's result.
2. Zero behavior change: proxy behavior matrix and all call-site tests pass
   unchanged.
3. Full suite green: **0 failed**, 7 skipped, 1367 + new passing tests.
4. `git diff` limited to `app/providers/openai_compat_client.py` and the two test
   files (§4); no `PROJECT_LOG.md`, `app/api/`, or persistence/state changes.

## 8. Commit

Single logical commit after all acceptance criteria hold:

`refactor: add proxy_request_kwargs method to OpenAI-compatible client (P4.3.2)`

Staged files only: `app/providers/openai_compat_client.py`,
`tests/test_openai_compat_client.py`, `tests/test_provider_factory.py`.
`docs/platform-p4.2-plan.md` and `docs/platform-p4.3-phase1-plan.md` remain
untracked as before.
