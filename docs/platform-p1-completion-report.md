# Relay — P1 Completion Report (First-run Setup Wizard & Provider Configuration System)

Date: 2026-08-03
Status: ✅ Complete — regression gate green (901 passed, 5 skipped)

Phase source: `docs/platform-p1-plan.md` · roadmap: `docs/platform-implementation-roadmap.md` (P1) ·
target design: `docs/platform-missing-components-report.md` (§0, §1).

---

## 1. Files created and modified during P1

### Created (new modules & tests)

| File | Purpose |
| --- | --- |
| `app/setup/__init__.py` | Package exports: `run_setup`, `SetupResult` |
| `app/setup/ui.py` | `UI` protocol + `TerminalUI` + `ScriptedUI` (test driver); TTY-aware reporter selection |
| `app/setup/wizard.py` | Six-step wizard orchestration; `SetupResult`; `RUNTIME_READY` boundary |
| `app/setup/key_validation.py` | Key entry, live validation, classification (`auth_error`/`expired`/`quota`/`unavailable`/`ok`), retry/skip loop |
| `app/setup/scan.py` | `ScanEngine` (bounded `ThreadPoolExecutor`, P4 async seam), `ScanResult`, status mapping |
| `app/setup/reporting.py` | `RichProgressReporter`, `PlainProgressReporter`, `RecordingReporter`, `summarize`/`detail_order`/`result_line` |
| `app/setup/persistence.py` | `.relay/availability.json` snapshot store (schema 1, atomic write, latest-per-provider) |
| `app/providers/registry.py` | `ProviderDefinition`, `PROVIDER_REGISTRY`, `PROVIDER_MENU` (six providers, ordered) |
| `app/providers/availability.py` | Shared `classify_probe` status mapper + `AVAILABLE`/`OVERLOADED`/`UNAVAILABLE` + `GLYPH` |
| `app/providers/anthropic_client.py` | Setup-capable Anthropic client (`list_models`/`probe_model`/`validate_key`) |
| `app/providers/gemini_client.py` | Setup-capable Gemini client |
| `app/providers/ollama_client.py` | Setup-capable Ollama client (keyless) |
| `app/services/config_store.py` | Single-writer provider config persistence to `.env` (only module allowed to write it) |
| `tests/test_provider_registry.py` | Registry integrity (10 tests) |
| `tests/test_setup_wizard.py` | Full wizard flows via `ScriptedUI` + fake clients (14 tests) |
| `tests/test_key_validation.py` | Classification + retry/skip loop (18 tests) |
| `tests/test_scan.py` | Scan engine: concurrency, ordering, mapping, callbacks, partial results (7 tests) |
| `tests/test_config_store.py` | `.env` round-trip, single-writer, no key echo (8 tests) |
| `tests/test_setup_reporting.py` | Progress/summary rendering + transcript (8 tests) |

### Modified

| File | Change |
| --- | --- |
| `app/cli.py` | `_cmd_setup` → thin delegate to `run_setup` then `_cmd_serve()` on success (serve handoff, no second command); wizard imported lazily; `_has_usable_provider` extended over the 6-provider registry; `_PROVIDERS` hardcode replaced by registry lookups |
| `app/core/config.py` | New env: `ANTHROPIC_ENABLED`, `GEMINI_API_KEY`, `GEMINI_ENABLED`, `OLLAMA_ENABLED` (+ Gemini base URL); existing parsed keys kept |
| `app/providers/openai_compat_client.py` | Shared `validate_key` helper (`GET /models` with supplied key) for NVIDIA/OpenAI/LM Studio |
| `app/services/health_checker.py` | Probe classification refactored onto the shared `availability.classify_probe` mapper |
| `app/services/client_registry.py` | Additive entries for the new providers (runtime path unchanged until P4) |
| `app/services/setup_state.py` | Additive fields `configured_providers`, `last_setup_at` (schema stays 1-readable) |
| `requirements.txt`, `pyproject.toml` | Added `rich==15.0.0` |
| `.env.example`, `docs/configuration.md` | New provider env vars |
| `README.md`, `PROJECT_LOG.md` | P1 entries |
| `tests/conftest.py` | Session env defaults for new enabled flags (tests stay network-free) |

### Not produced (planned deliverable, see §5 debt)

- `docs/platform-db-schema.md` (plan §9) — target `relay.db` schema + migration plan. Referenced by
  `persistence.py`; deferred with the database work.

---

## 2. Current architecture after P1

```
app/cli.py (argparse) ── bare `relay` no-args ──┬─ _config_configured()?  → _cmd_serve()
                                                  └─ else / `relay setup` → _cmd_setup()
                                                                             │  (wizard imported lazily)
                                                                             ▼
                                          app/setup/wizard.run_setup(ui) ──▶ serve handoff on success
                                                                            │
        ┌───────────────────────────────────────────────┬───────────────────┴───────────┬───────────────────┐
        ▼                                               ▼                               ▼                   ▼
app/setup/ui.py                               app/setup/key_validation.py      app/setup/scan.py   app/setup/reporting.py
UI protocol / TerminalUI / ScriptedUI         classify() + validate_key()      ScanEngine (thread-   Plain / Recording /
progress() → reporter selection               + resolve_cloud_key (R/S)       pool, concurrency 8)   Rich reporters
        │                                                                              │                    │
        ▼                                                                              ▼                    ▼
app/providers/registry.py (6 defs, menu) ── client_factory ──▶ clients (nvidia/openai/lmstudio/openai_compat,
                                                                anthropic/gemini/ollama setup-capable)
app/providers/availability.py ── classify_probe() ──◀── shared by health_checker + scan
        │
        ▼
app/services/config_store.py (single writer → .env)   app/setup/persistence.py (.relay/availability.json)
app/services/setup_state.py   (.relay/state.json, schema 1)
```

Key seams:
- **Wizard/UI decoupling** — the wizard only talks to the `UI` protocol; tests drive every branch through
  `ScriptedUI` (raises loudly on script exhaustion). Reporters are chosen by TTY-ness via `ui.progress()`.
- **Provider registry** — one source of truth (`ProviderDefinition` with `client_factory` +
  `runtime_factory: None` seam). New providers are setup-capable now, runtime-wired in P4.
- **Scan engine seam** — the wizard depends only on `ScanEngine.scan(...)` + `on_update`; P4 replaces the
  executor body with asyncio/`aprobe_model` behind the identical interface (reporters don't change).
- **Single writer** — wizard never touches dotenv; only `config_store` writes `.env`; availability results
  only through `persistence.write_snapshot`.
- **CLI** — argparse retained (Decision A). `_cmd_setup` runs the wizard and hands off straight to the server;
  no "run relay again" step.

---

## 3. Original Relay target design — completeness after P0+P1

Legend: ✅ complete · ⚠️ partial · ❌ missing (requirement numbers from `platform-missing-components-report.md` §0/§1)

| # | Requirement | Status | Where |
| --- | --- | --- | --- |
| 1 | One-command GitHub install | ✅ | P0 packaging; GitHub-specific `pip install git+` not yet exercised end-to-end (wheel/console-script smoke only) |
| 2 | Post-install "type `relay`" | ✅ | P0/P1 |
| 3 | First launch auto-detects config → setup | ✅ | P1 bare `relay` dispatch |
| 4 | Never edit config files | ⚠️ | Wizard-managed `.env`; full config surface deferred to P7 |
| 5 | One-by-one provider selection | ✅ | P1 wizard menu |
| 6 | Key validation, classified errors, retry/skip | ✅ | P1 `key_validation.py` |
| 7 | Catalog + totals + **one global progress bar** + dynamic model name | ✅ | P1 (bounded thread-pool scan; async-first clients in P4) |
| 8 | Totals + optional `✓/⚠/✗` list, **no default dump** | ✅ | P1 (durable `model_status` in P6) |
| 9 | Setup → main interface seamlessly | ⚠️ | P1 hands off to the **server** (`_cmd_serve`); TUI handoff completes in P2 |
| 10 | Chat with random available model | ❌ | P2 |
| 11 | Select model and test it | ⚠️ | Probing exists (scan); UI surface in P2 |
| 12 | Manage providers | ⚠️ | Wizard enable/disable; `relay providers` + TUI in P2/P4 |
| 13 | Add/remove API keys | ❌ | P5 |
| 14 | Change model priority | ⚠️ | Setup-time only (`priority_env`); re-prioritize in P2 |
| 15 | Manage routing rules | ⚠️ | `TASK_*` envs only; rules surface in P2/P7 |
| 16 | View connected applications | ❌ | P5/P6 |
| 17 | System status | ⚠️ | `/health` exists; `relay status` + TUI in P2 |
| 18 | Manage configuration | ⚠️ | Wizard-managed `.env`; `relay config` in P7 |
| 19 | OpenAI-compatible clients, minimal setup | ✅ | `/v1` endpoint exists; per-client guides in P8 |
| 20 | API-key handling abstracted by Relay | ⚠️ | Upstream keys interim in `.env`; per-app keys + keyring in P5 |
| 21 | Provider abstraction layer | ✅ | P1 registry + base + clients |
| 22 | Secure API key storage | ⚠️ | `.env` interim (masked, single-writer); keyring/encrypted in P5 |
| 23 | Persistent DB/config storage | ⚠️ | `state.json` + `availability.json` interim; `relay.db` in P6 |
| 24 | Model availability tracking | ⚠️ | Snapshot + in-memory health; durable `model_status` in P6 |
| 25 | Provider health monitoring | ✅ | Existing health checker, now sharing `classify_probe` |
| 26 | Async operations for slow scans | ⚠️ | Bounded thread-pool with P4 async seam; hot path sync until P3 |
| 27 | CLI/TUI interface | ⚠️ | argparse CLI + wizard; Textual TUI in P2 |
| 28 | Packaging/distribution flow | ✅ | P0 |

**Net:** 11 fully complete, 14 partial, 3 missing (10, 13, 16). P1 lifted the two weakest UX areas — first-run
auto-setup (3) and the spec's wizard UX (5–8) — from ❌/⚠️ to ✅.

---

## 4. Remaining requirements (P2–P8)

### P2 — Terminal UI, the main interface (Textual)
Screens for Chat (random available model), Model test, Providers, Keys, Model priority, Routing rules,
Connected applications, System status, Configuration. Read-only panels first, interactive controls as P5/P6
surfaces land. `relay tui` entry; **setup → TUI handoff** (requirement 9); `textual.pilot` smoke tests.

### P3 — Async provider layer (hot path)
`ChatService` async failover + streaming; `/v1/chat/completions`, `/v1/models`, `/chat` → `async def`;
lock-safe stores; `pytest-asyncio`. (Requirement 26 completion on the hot path.)

### P4 — Provider integrations (async-first)
De-string-named provider ids; `achat`/`alist_models`/`aprobe_model`/`achat_stream` (swaps the P1 scan seam
and unblocks P2); Anthropic (Messages), OpenRouter, Groq, Ollama, custom OpenAI-compatible wired at runtime;
`relay providers`. **Closes the P1 runtime gap** for anthropic/gemini/ollama.

### P5 — API-key security (security gate)
`api_keys` table (scrypt/pbkdf2, salt, scopes, expiry, rotation, revoke); `relay keys`; admin API;
constant-time compare; `RELAY_API_KEY` bootstrap retained; upstream keys out of plaintext `.env` into
keyring/encrypted store (`.env` stays compat); client integration keys (`relay keys create --label …`).
(Requirements 13, 20, 22.)

### P6 — Platform database + model availability + usage
Migrations on `platform.db`; `api_keys` (from P5), `request_log` (metadata-only privacy contract),
`model_status`, `events`; durable `✓/⚠/✗` availability fed by setup probes + health + learned feedback;
connected applications (`apps` = labeled keys × `request_log`); retention. Replaces `availability.json`.
(Requirements 16, 23, 24.)

### P7 — Configuration management
`relay config show/validate/reload/diff`, secret masking, TUI config panel; full config reachable without
editing files. (Requirements 4, 18.)

### P8 — Client guides + quality gate & CI
Cline / OpenCode / Continue setup guides (generic OpenAI-compatible section); `[tool.pytest]`; async/CLI/TUI/
install-smoke suites; optional ruff/mypy; GH Actions. (Requirement 19 completion.)

### Cross-cutting
Update `PROJECT_LOG.md`, README, docs at each boundary; `python-observability` on metrics/logs;
`security-best-practices` gate before P5.

---

## 5. Technical debt, decisions, and risks

### Decisions (deliberate)
- **D1 — argparse retained (Decision A), wizard in `app/setup/`** (not the roadmap's `app/cli/` + Typer).
  Rationale: zero churn to P0 tests and `python -m app.cli`. Consequence: P2 needs a `relay tui` subcommand
  added to `app/cli.py`; Typer migration can stay deferred.
- **D2 — rich is now module-level in `reporting.py`.** The original plan claim (§6.1, §13) that "the server
  hot path imports none of [rich]" was **disproven during P1 validation**: `httpx 0.28.1` imports `_main`
  → `click` → `pygments` → `rich`, and `httpx` is a hard dependency of every client, the CLI (`cli.py:5`
  imports the registry) and the server. Rich loads regardless of laziness. Resolution: honest module-level
  import + corrected docstrings; the impossible "rich never imported" test was replaced with an end-to-end
  `RichProgressReporter` test.
- **D3 — interim plaintext `.env` keys until P5** (plan §2 C). Single writer (`config_store`), masked
  everywhere, never echoed. Permanent store designed but not built.
- **D4 — interim bounded stores** (`state.json`, `availability.json`, latest-per-provider) until P6
  `relay.db` swap; raw history deliberately not kept.
- **D5 — sync thread-pool scan with an explicit P4 async seam** (plan §5.1). Hot path remains sync until P3.

### Debt
- **`docs/platform-db-schema.md` not produced** (plan §9 deliverable). `persistence.py` references it.
  Required before P6 (recommend producing it as part of the P5/P6 planning so the migration story is written
  down, not carried in memory).
- Priority is persisted **at setup time only** (`priority_env`); no runtime re-prioritization until P2/P7.
- All P0+P1 work is **uncommitted** (repo has a single baseline commit `6fc3dea`). Recommend committing P0+P1
  as a clean boundary before starting P2.
- GitHub one-command install path (`pip install git+…`) not yet exercised end-to-end (wheel + console-script
  smoke only).

### Risks (carried)
- **P4 runtime gap (plan §2 B):** `RUNTIME_READY = {nvidia, openai, lmstudio}`. Anthropic/Gemini/Ollama are
  wizard-configurable but not routed; an Anthropic-only setup starts the server with **no `/chat` provider**
  until P4. The wizard surfaces this in its summary; conscious boundary.
- rich's transitive import adds startup cost to every CLI invocation (accepted; revisit only if CLI startup
  latency becomes a user-facing issue).
- Long scans (100+ models) bounded by `SETUP_SCAN_CONCURRENCY` (default 8) + `Ctrl+C` partial-save; summary
  reflects what was scanned.

---

## 6. Ready for P2?

**Yes.** The regression gate is green (**901 passed, 5 skipped**; 65 new P1 tests), the wizard → server
handoff is proven end to end, and every read surface P2 needs already exists (provider registry, health
checker, availability snapshots, config). P1 also validated the key architectural assumptions P2 builds on:
`UI`-protocol decoupling and the scan/seam design.

Recommended pre-P2 actions (small, non-blocking):
1. Commit P0+P1 (single baseline + one phase commit).
2. Add the `relay tui` entry point to `app/cli.py` (subcommand + handoff hook).
3. (Optional) Write `docs/platform-db-schema.md` while P6 shape is fresh.

---

## 7. Future idea (do not implement) — P9 "Relay Identity / Interactive Terminal Experience"

Recorded for later, **not part of P2–P8**: a mascot, cursor-following animation, and personality UI.
Architecture is already positioned to absorb this without a core rewrite:

- The wizard is fully decoupled behind the `UI` protocol; reporters are swappable — personality is a
  presentation concern, not a service concern.
- The P2 TUI will be a separate presentation surface over the same services (ChatService, routing,
  providers, config) — a "Relay identity" layer would decorate that surface only.
- `app.core`, `app.services`, and `app.providers` must never learn about mascot/personality; the boundary
  stays: services are observable, the UI layer renders.

Add to the roadmap as a post-P8/polish item (P9), gated on the P2 TUI existing.
