# P1 — First-run Setup Wizard & Provider Configuration System

Implementation plan (Phase 9 approved roadmap, P1 scope). Planning only —
no code has been written for this phase.

- Source: `docs/platform-implementation-roadmap.md` (P1 section), product
  spec (wizard flow), P0 validation findings.
- Regression gate (inherited): existing **836 tests stay green**; `/v1`
  wire behavior, `RELAY_API_KEY` bootstrap, `.env` compat, `python -m app.cli`
  / `python -m uvicorn app.main:app`, and the P0 console script keep working.

---

## 1. Scope

### In scope (this phase)
- Interactive **setup wizard** launched by bare `relay` (first run or
  incomplete) and by `relay setup` / `python -m app.cli setup`.
- **Provider configuration system**: a provider definition registry, a
  single config persistence facade, key entry + live validation, catalog
  discovery, availability scanning, model priority, and setup completion
  handoff to the server.
- Six providers: NVIDIA NIM, OpenAI, Anthropic, Google Gemini, LM Studio
  (local), Ollama (local).
- Interim persistence of availability results + documented target
  `relay.db` schema (migration plan only — database lands in P6).

### Out of scope (deferred — do not build here)
- Full chat clients / runtime routing for Anthropic, Gemini, Ollama
  (roadmap **P4**). P1 adds setup-capable clients only.
- Async-first provider clients (`alist_models`, `aprobe_model`) — P4; P1
  uses a bounded thread-pool scan with a seam that P4 swaps in.
- TUI main interface (`relay tui`) — P2.
- Secure upstream key storage (keyring/encrypted) — P5.
- `relay.db` tables, `model_status`, migrations, `relay migrate` — P6.
- Full subcommand surface (`keys`, `routing`, `apps`, `config`, `logs`,
  `test`) — owned by P5/P6/P7.
- Typer framework migration — see §2 Decision B (recommended: defer).

---

## 2. Decisions to confirm

### A. Command framework (recommended: keep argparse, add Rich only)
- **Recommended**: keep `app/cli.py` as the argparse entry (unchanged
  structure). Rich is used *only* for progress bars and status rendering
  inside the wizard. `_cmd_setup` becomes a thin delegate to
  `app/setup/wizard.run_setup(...)`.
  - Rationale: the user's P1 scope is the wizard; zero churn to P0 tests
    (they patch `cli._cmd_setup` / `cli._cmd_serve`); no risk to
    `python -m app.cli`.
- **Alternative (roadmap-literal)**: new `app/cli/` package + Typer +
  minimal read-only subcommands (`serve`, `status`, `providers`, `models`).
  Requires updating the P0 test patch targets; `app/cli.py` becomes a shim.
- Confirm which path before implementation.

### B. Setup-capable clients vs. runtime wiring
- P1 clients implement only what setup needs: `list_models`,
  `probe_model`, `validate_key` (chat stubs raise `NotImplementedError`).
- Providers configured by the wizard that are **not** yet in the server hot
  path (Anthropic, Gemini, Ollama) are marked configured but are **not
  registered at runtime** until P4. The wizard notes this in its summary.
  Consequence accepted: an Anthropic-only setup will start the server but
  `/chat` will have no routed provider until P4. Flagging this so it is a
  conscious boundary, not a surprise.

### C. Key storage for P1
- Wizard still writes upstream keys to `.env` (runtime compat — `Settings`
  reads env) but **only through the new `config_store`** single writer.
- This satisfies "do not store raw API keys in .env *permanently*": the
  permanent store is designed now (secret table + migration plan, §8) and
  implemented in P5/P6. Keys are masked everywhere and never echoed.
- Confirm the interim `.env` storage is acceptable until P5.

### D. Per-provider progress bar
- One global bar per provider scan (matches the spec's single-bar example).
  An aggregate bar spanning all providers is out of scope.

---

## 3. What P1 needs from P0 — gap review

| P0 deliverable | Reuse in P1 | Gap / action |
| --- | --- | --- |
| Setup-state marker (`app/services/setup_state.py`, `.relay/state.json`) | First-run/incomplete detection | Extend payload additively (§8); keep reading `schema=1` files |
| `state_dir` + env-file resolution (`app/core/config.py`) | Wizard write target | Reuse as-is |
| Console script `relay = app.cli:main`, `python -m app.cli` | Entry points | Unchanged |
| No-args dispatch (`main()`, `_config_configured`, `_first_run`) | Handoff into wizard | `_cmd_setup` now calls `run_setup` then serve on success |
| `_has_usable_provider()` | Configured-detection | **Hardcoded to 3 providers** — extend to the 6-provider registry (or the subset of configured ones) so a Gemini-only setup counts as configured |
| `Provider` dataclass + `apply_model_priority` | Provider model | Extend registration data via `ProviderDefinition` (keeps `Provider` unchanged) |
| `OpenAICompatibleClient` (+ NVIDIA/OpenAI/LM Studio subclasses) | Cloud/local clients | Reuse; add Anthropic/Gemini/Ollama clients |
| `HealthChecker._probe_models` status mapping | Availability classification | Extract the 200/429/529/timeout/else mapping into a shared helper for the scan engine |
| `_update_env`/`set_key` calls scattered in `_setup_provider` | Config writes | Centralize into `config_store` (single writer) |
| P0 packaging tests (monkeypatch `cli._cmd_setup`/`_cmd_serve`) | Regression | No changes under Decision A; updated targets under Decision B |

Nothing in P0 blocks P1. The only true gap is the hardcoded provider set in
`_has_usable_provider` and the missing setup-capable clients for the four
new providers.

---

## 4. Provider abstraction design

### 4.1 `ProviderDefinition` (new: `app/providers/registry.py`)

```python
@dataclass(frozen=True)
class ProviderDefinition:
    id: str                 # stable slug: "nvidia" | "openai" | "anthropic" | "gemini" | "lmstudio" | "ollama"
    display_name: str       # "NVIDIA NIM", "Google Gemini", "LM Studio (local)", ...
    kind: str               # "cloud" | "local"
    requires_api_key: bool
    key_env: str | None     # e.g. "NVIDIA_API_KEY"; None for keyless local
    enabled_env: str        # e.g. "NVIDIA_ENABLED"
    base_url_env: str       # env override; fall back to base_url_default
    base_url_default: str
    priority_env: str | None
    health_endpoint: str    # default "/models"
    client_factory: Callable   # -> object with list_models/probe_model/validate_key
    runtime_factory: Callable | None  # server-side create_provider (None until P4 for new ones)
```

`PROVIDER_REGISTRY: Dict[str, ProviderDefinition]` plus an ordered
`PROVIDER_MENU` tuple (menu order = the spec's `[1]..[6]`). The wizard
iterates the registry; the hardcoded `_PROVIDERS` list in `app/cli.py` is
replaced by registry lookups. Display-name map feeds the numbered menu.

### 4.2 Provider catalog (the six)

| id | display | kind | key | key_env | enabled_env | base URL (default) | client |
| --- | --- | --- | --- | --- | --- | --- | --- |
| nvidia | NVIDIA NIM | cloud | yes | `NVIDIA_API_KEY` | `NVIDIA_ENABLED` | `https://integrate.api.nvidia.com/v1` | `NvidiaClient` (exists) |
| openai | OpenAI | cloud | yes | `OPENAI_API_KEY` | `OPENAI_ENABLED` | `https://api.openai.com/v1` | `OpenAIClient` (exists) |
| anthropic | Anthropic | cloud | yes | `ANTHROPIC_API_KEY` (exists) | `ANTHROPIC_ENABLED` (new) | `https://api.anthropic.com/v1` | `AnthropicClient` (new) |
| gemini | Google Gemini | cloud | yes | `GEMINI_API_KEY` (new) | `GEMINI_ENABLED` (new) | `https://generativelanguage.googleapis.com/v1beta` | `GeminiClient` (new) |
| lmstudio | LM Studio (local) | local | no | `LMSTUDIO_API_KEY` | `LMSTUDIO_ENABLED` | `http://localhost:1234/v1` | `LMStudioClient` (exists) |
| ollama | Ollama (local) | local | no | — | `OLLAMA_ENABLED` (new) | `http://localhost:11434` | `OllamaClient` (new) |

### 4.3 New clients (setup-capable only)

Each mirrors the existing pattern: module-level `httpx.get/post` (so tests
monkeypatch them exactly like `test_openai_compat_client.py`), honoring
`proxy_request_kwargs`, raising `ProviderHTTPError`/`ProviderTimeout`.

- `app/providers/anthropic_client.py`
  - `list_models`: `GET /v1/models`, headers `x-api-key` + `anthropic-version: 2023-06-01` → `data[].id`.
  - `probe_model`: `POST /v1/messages` `{model, max_tokens:1, messages:[{role:"user",content:"ping"}]}` → `ModelProbe`.
  - `validate_key`: `GET /v1/models`.
- `app/providers/gemini_client.py`
  - `list_models`: `GET /v1beta/models?key=...` → `models[].name` (strip `models/`).
  - `probe_model`: `POST /v1beta/models/{model}:generateContent?key=...` `{contents:[{parts:[{text:"ping"}]}]}`.
  - `validate_key`: `GET /v1beta/models?key=...`.
- `app/providers/ollama_client.py`
  - `list_models`: `GET /api/tags` → `models[].name`.
  - `probe_model`: `POST /api/chat` `{model, messages:[...], stream:false}`.
  - no `validate_key` (local, keyless).
- Shared `validate_key` helper on `OpenAICompatibleClient`: `GET /models`
  with the supplied key (NVIDIA/OpenAI/LM Studio).

### 4.4 Key validation classification

`app/setup/key_validation.py`:

```python
@dataclass
class KeyValidation:
    ok: bool
    category: str   # "auth_error" | "expired" | "quota" | "unavailable" | "ok"
    message: str
```

Classification (driven by the shared status mapper):
- 200 → `ok`.
- 401/403 → `auth_error`; parse body for "expired"/"invalid"/"revoked" → `expired` where recognizable.
- 429/402, or body matching quota/insufficient_quota → `quota`.
- 5xx / network error / timeout → `unavailable`.
- UI on failure prints `✗ Invalid API key` + `Reason:` + category-specific
  copy, then a **R / S** retry-or-skip prompt (spec step 2). Loop on R.

### 4.5 Status mapper extraction

Move `HealthChecker._probe_models`'s classification into a shared helper
(`app/providers/availability.py`: `classify_probe(probe) -> "available" |
"overloaded" | "unavailable"`, mapping 200→available, 429/529/timeout→
overloaded, else unavailable). HealthChecker and the scan engine both use
it. `overloaded` renders as `⚠`, `unavailable` as `✗`, `available` as `✓`.

---

## 5. Async scanning architecture

Scanning must not block the wizard and must keep the console responsive
while rendering one progress bar.

### 5.1 `app/setup/scan.py`

```python
@dataclass
class ScanResult:
    model: str
    status: str          # available | overloaded | unavailable
    latency_ms: int
    status_code: int | None
    error: str

class ScanEngine:
    def __init__(self, concurrency: int = 8): ...
    def scan(self, client, provider, models, on_update=None) -> List[ScanResult]: ...
```

- Implementation: `ThreadPoolExecutor(max_workers=concurrency)` submitting
  `client.probe_model(provider, model)`; results collected in model order;
  `on_update(done, total, result)` fired per completion (called from the
  main thread via `executor.submit` + `as_completed`, not from worker
  threads — keep UI updates single-threaded).
- Concurrency default 8, overridable via `SETUP_SCAN_CONCURRENCY`.
- Per-model probe timeout is the existing 10 s; `Ctrl+C` cancels and
  returns partial results (already-scanned models still saved).
- **P4 seam**: the wizard only depends on `ScanEngine.scan(...)` and the
  `on_update` callback. P4 replaces the executor body with asyncio +
  `aprobe_model` behind the identical interface — the wizard and reporter
  do not change.

### 5.2 Data flow

```
provider selected + key validated
        │  client.list_models(provider)          (sync, single call, 30 s timeout)
        ▼
catalog = [models...]  →  "Found: 102 models"
        │  ScanEngine.scan(...)  with one global progress task
        ▼
ScanResult list  →  summary (Total/Available/Unavailable)  →  optional detailed list
        │
        ▼
persist availability snapshot  →  config_store / availability store (§8)
```

---

## 6. Progress reporting design

### 6.1 Reporter abstraction

`app/setup/reporting.py`:

```python
class ProgressReporter(Protocol):
    def begin_scan(self, total: int): ...
    def update(self, done: int, total: int, current: str, recent: list[tuple[str, str]]): ...
    def end_scan(self, summary: dict): ...
```

- `RichProgressReporter` (TTY): one `rich.progress.Progress` task per scan
  (`[██████████----------] 50%`); the task description carries the current
  model name ("Testing: deepseek-ai/deepseek-v4-flash"); a bounded
  rolling list of the last 5 results is rendered beneath the bar
  (`✓ google/gemma-3-12b-it`, `⚠ ... (overloaded)`, `✗ ...`), refreshed
  inside a single `rich.live.Live` block so only one bar exists at any time.
- `PlainProgressReporter` (non-TTY, tests, piped stdin): periodic lines +
  final summary; no ANSI. Tests assert against this.
- Reporter selection: TTY-aware (`sys.stdin.isatty()`), overridable by env
  (`NO_COLOR`, or `SETUP_NO_PROGRESS=1` for CI).

### 6.2 Summary rules (spec step 3)

- Print `Results:` then `Total models: N`, `Available: X`, `Unavailable: Y`
  (`Y = N - X`; overloaded counts as available-but-flagged, shown under
  `⚠`). **No model dump by default.**
- `View available models? (y/n)` → if yes, print the `✓`/`⚠`/`✗` list
  (available and overloaded first, unavailable last).

### 6.3 Testing hooks

- `PlainProgressReporter` records a transcript (`begin`, `N` updates, `end`)
  that tests assert on (exactly one `begin_scan`, updates monotonic,
  bounded `recent` window).
- A `RecordingReporter` (test-only) captures `(done,total,current)` tuples
  without any rendering.

---

## 7. Wizard architecture

### 7.1 `app/setup/` package

| File | Purpose |
| --- | --- |
| `app/setup/__init__.py` | exports `run_setup` |
| `app/setup/ui.py` | `UI` protocol + `TerminalUI` + `ScriptedUI` (test driver) |
| `app/setup/wizard.py` | `run_setup(ui, registry, store)` orchestration; the spec's 6 steps |
| `app/setup/key_validation.py` | key entry, live validation, retry/skip loop |
| `app/setup/scan.py` | `ScanEngine`, `ScanResult`, classify mapping |
| `app/setup/reporting.py` | progress/summary reporters |
| `app/setup/persistence.py` | availability snapshot store (`.relay/availability.json`) |
| `app/services/config_store.py` | single writer for provider config to `.env` |

### 7.2 `UI` protocol

```python
class UI(Protocol):
    def notice(self, text: str): ...          # plain informational lines
    def ask(self, prompt: str, default: str | None = None) -> str
    def ask_yes_no(self, prompt: str, default: bool) -> bool
    def menu(self, options: list[str], prompt: str) -> int | None  # 1-based index or skip
    def confirm(self, prompt: str, default: bool) -> bool
    def retry_or_skip(self, prompt: str) -> str   # "r" | "s"
    def progress(self) -> ProgressReporter
```

- `TerminalUI` wraps Rich console + `input()` (keeps existing
  EOFError/KeyboardInterrupt handling from `app/cli.py`).
- `ScriptedUI(script)` pops the next scripted answer; raises if the script
  is exhausted (a test bug fails loudly instead of hanging). Wizard tests
  drive every branch through `ScriptedUI`.

### 7.3 Wizard steps (spec 1–6)

1. **Welcome** — `Welcome to Relay!` (first run) or "Relay setup was not
   completed. Let's finish it." (incomplete), then numbered provider menu
   `[1]..[6]` (spec). Provider choice loops: after each provider the wizard
   returns to the menu so the user can enable several, skip, or stop.
2. **Per-provider key setup** — cloud: enter key → `Validating key...` →
   `✓ Authentication successful` or `✗ Invalid API key` + reason + `[R]etry/[S]kip`.
   Local: connectivity check (`GET /models` or `/api/tags`), no key prompt.
3. **Catalog discovery** — `Fetching model catalog...` → `Found: 102 models`.
   Availability scan (§5/§6). Summary + optional detailed list.
4. **Model priority** — menu over **available models only**:
   accept default order / customize order (search + numbered selection, reusing
   the existing `_select_models` interaction, restricted to available set) /
   skip. Persisted to `priority_env` via `config_store`.
5. **Save** — `config_store` writes provider toggles, keys, base URL
   overrides, priority (never echoes keys). Availability snapshot persisted.
6. **Completion** — `Relay setup complete.` → `Starting Relay...` → call
   `_cmd_serve()` (no "run relay again"). Setup-state marker written
   `configured` (or `incomplete` if zero usable providers). If the user
   configured only providers not yet runtime-wired (§2 B), note it in the
   summary.

### 7.4 CLI integration

- `app/cli.py` `_cmd_setup` becomes:

```python
def _cmd_setup(args) -> None:
    ui = TerminalUI()
    configured = run_setup(ui)
    if configured:
        print("Relay setup complete.")
        _cmd_serve()
```

- Bare `relay` no-args path is unchanged: `_config_configured()` →
  serve, else `_first_run()` → `_cmd_setup`.
- `_has_usable_provider()` extended over the registry: any cloud provider
  enabled with a key, or any local provider enabled.
- `_config_configured()` unchanged (marker + usable provider).

---

## 8. Database / state changes

### 8.1 Interim (implemented in P1)

| Store | Path | Contents |
| --- | --- | --- |
| Setup state | `.relay/state.json` (existing, `schema:1`) | Extended additively: `configured_providers: [ids]`, `last_setup_at`. Reader must still accept `schema:1` files without these fields (backward compat). |
| Config | `.env` via `config_store` | Runtime source of truth until P6. Single writer module. |
| Availability snapshot | `.relay/availability.json` (new, `schema:1`) | `{provider_id, generated_at, models:[{model,status,latency_ms,error,probed_at}]}`. Bounded: keep the latest snapshot per provider; raw history deferred to the P6 table. |
| Secrets (interim) | `.env` | Keys remain in `.env` for runtime compat; masked everywhere; only `config_store` writes them. |

### 8.2 Target `relay.db` schema (documented in `docs/platform-db-schema.md`, built in P6)

```
providers     (id TEXT PK, display_name, kind, enabled, base_url, priority,
               requires_key, created_at, updated_at)
secrets       (id PK, provider_id FK, secret_ref, label, updated_at)      # P5 keyring/encrypted ref
models        (id PK, provider_id FK, model_id, capability, catalog_ts, UNIQUE(provider_id, model_id))
availability  (id PK, provider_id FK, model_id, status, latency_ms, error, probed_at)   # -> model_status (P6)
priority      (provider_id FK, model_id, position, UNIQUE(provider_id, model_id))
api_keys      (P5)   request_log (P6)   events (P6)
```

`docs/platform-db-schema.md` records: table DDL sketches, column meaning,
privacy contract (metadata only; never prompts/keys), and the migration
timeline (`.env`/`availability.json` → `relay.db` with a `relay migrate`
step that imports the latest snapshots and existing `.env` config).

### 8.3 Config store facade

`config_store` is the **only** module in P1 that writes provider config to
`.env`. Signature:

```python
def set_provider_config(defn, *, enabled=None, api_key=None,
                        base_url=None, priority_models=None): ...
def get_provider_config(defn) -> dict    # from settings/env
```

- Writes via the existing `_update_env`/`set_key(env_file, ...)` path.
- The DB swap in P6 replaces this module's implementation; wizard code
  never touches dotenv directly.

### 8.4 Migration considerations

- Read both `.env` and `availability.json`; never write a config the old
  reader (`Settings`) cannot load.
- No silent key migration: moving a key out of `.env` only happens in P5
  with an explicit user action; the wizard masks but still uses `.env`.
- `state.json` schema stays readable for old files (additive fields only).
- Existing `.env`-only users (pre-P1 setups) must pass `_config_configured`
  unchanged — the extended `_has_usable_provider` is a superset of the old
  check.

---

## 9. Files to create

| File | Purpose |
| --- | --- |
| `app/providers/registry.py` | `ProviderDefinition`, `PROVIDER_REGISTRY`, `PROVIDER_MENU` |
| `app/providers/anthropic_client.py` | setup-capable Anthropic client |
| `app/providers/gemini_client.py` | setup-capable Gemini client |
| `app/providers/ollama_client.py` | setup-capable Ollama client |
| `app/providers/availability.py` | shared `classify_probe` status mapper |
| `app/services/config_store.py` | single-writer provider config persistence |
| `app/setup/__init__.py`, `app/setup/ui.py`, `app/setup/wizard.py` | wizard core |
| `app/setup/key_validation.py` | key validation + classification + retry/skip |
| `app/setup/scan.py` | `ScanEngine`, `ScanResult` |
| `app/setup/reporting.py` | `RichProgressReporter`, `PlainProgressReporter`, `RecordingReporter` |
| `app/setup/persistence.py` | `.relay/availability.json` snapshot store |
| `docs/platform-db-schema.md` | target `relay.db` schema + migration plan |
| `tests/test_provider_registry.py` | registry integrity |
| `tests/test_setup_wizard.py` | full wizard flows via `ScriptedUI` + fake clients |
| `tests/test_key_validation.py` | validation classification per category |
| `tests/test_scan.py` | scan engine: concurrency, ordering, status mapping, callbacks |
| `tests/test_config_store.py` | `.env` round-trip, single-writer, no key echo |
| `tests/test_setup_reporting.py` | progress/summary rendering + transcript |

## 10. Files to modify

| File | Change |
| --- | --- |
| `app/cli.py` | `_cmd_setup` → wizard delegate + serve handoff; `_has_usable_provider` over registry; replace `_PROVIDERS` with registry lookups |
| `app/core/config.py` | add `ANTHROPIC_ENABLED`, `GEMINI_API_KEY`, `GEMINI_ENABLED`, `OLLAMA_ENABLED` (+ Gemini base URL); keep existing parsed keys |
| `app/providers/base.py` | no functional change (verified); `validate_key` stays on clients |
| `app/services/setup_state.py` | additive fields (schema stays 1-readable) |
| `app/services/client_registry.py` | additive entries for new providers (runtime path unchanged until P4) |
| `pyproject.toml`, `requirements.txt` | add `rich` (+ `typer` only under Decision B) |
| `.env.example`, `docs/configuration.md` | new env vars |
| `README.md`, `PROJECT_LOG.md` | P1 entry; wizard UX summary |
| `tests/conftest.py` | session env defaults for new enabled flags (keep tests network-free) |
| `tests/test_packaging.py` | only under Decision B (patch targets move) |

## 11. Testing strategy

- **No new test framework.** Existing pytest + `httpx` monkeypatch pattern
  (FakeResponse in `test_openai_compat_client.py`). No `pytest-asyncio`
  needed (scan is thread-pool, not asyncio).
- **Wizard flows** (`test_setup_wizard.py`): `ScriptedUI` + `FakeRegistry`
  (canned `list_models`/`probe_model`/`validate_key`) covering:
  - first run → welcome → menu → skip-all → `incomplete`;
  - enable + valid key → catalog → scan summary → priority → `configured`;
  - invalid key → reason shown → retry → valid → continue;
  - invalid key → skip → next provider; quota/unavailable classification paths;
  - only-available-models priority restriction;
  - completion prints `Relay setup complete.` and calls serve.
- **Scan** (`test_scan.py`): in-flight work ≤ concurrency; results in model
  order; status mapping table (200/429/529/4xx/5xx/timeout/network); one
  `on_update` per model with monotonic `done`; partial results on cancel.
- **Key validation** (`test_key_validation.py`): per-category classification
  incl. expired-body detection; retry/skip loop termination.
- **Reporting** (`test_setup_reporting.py`): transcript has exactly one
  `begin_scan`; `recent` window bounded; summary math (`Total = Avail + Unavail`);
  no model dump unless requested; `✓/⚠/✗` list ordering.
- **Config store** (`test_config_store.py`): writes land in the patched
  `env_file`; round-trip through `Settings`; keys never printed; priority
  list round-trip; `state.json` schema-1 files still read.
- **Registry** (`test_provider_registry.py`): 6 entries, unique ids, valid
  URLs, correct env names, ordered menu.
- **CLI regression**: `relay --help`, `--version`, `python -m app.cli
  setup` (no-args setup via `ScriptedUI` monkeypatched in), plus all P0
  dispatch tests unchanged.
- **Gate**: full `pytest tests -q` green (836 baseline + new); packaging
  smoke still passes.

## 12. Migration considerations (summary)

Covered in §8.4. Key commitments: single writer (`config_store`), additive
state schema, read-both during transition, explicit user action for any
future key move, and a documented `relay.db` target so P6 is a swap, not a
rewrite.

## 13. Risks & boundaries

- **Runtime gap for new providers until P4** (§2 B) — the most important
  boundary; surfaced to the user in the wizard summary.
- Rich rendering in CI — mitigated by the plain/recording reporters.
- Long scans (100+ models × 10 s worst-case) — bounded concurrency +
  `Ctrl+C` partial-save; summary always reflects what was scanned.
- P0 test coupling to `cli._cmd_setup` — preserved under Decision A.
- Dependency addition (rich) — isolated to the wizard; the server hot path
  imports none of it.

## 14. Definition of done (P1)

- `relay` on a clean machine → welcome → wizard → providers configured →
  `Relay setup complete.` → `Starting Relay...` (server up, no second run).
- `relay setup` / `python -m app.cli setup` re-runnable; menu returns to
  manage additional providers.
- Key validation live, classified, retry/skip; catalog + one-bar scan +
  Total/Available/Unavailable summary (no default dump).
- Priority limited to available models; config persisted via `config_store`.
- Anthropic, Gemini, Ollama selectable in the wizard with setup-capable
  clients (chat deferred to P4, documented).
- Full suite green; P0 packaging/console-script tests green; README/docs/
  `.env.example` updated; `PROJECT_LOG.md` P1 entry.
