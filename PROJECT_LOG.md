# Relay Project Log

---

# Milestone 1 — Project Foundation

## Status
✅ Complete

## Completed

- Created Relay project structure
- Added configuration system
- Created Provider abstraction
- Implemented NVIDIA provider
- Implemented ProviderManager
- Created `/providers` endpoint
- Relay now owns ProviderManager

## Notes

- Relay is the application's main facade.
- Providers are registered during Relay startup.
- API layer should remain as thin as possible.

---

# Milestone 2 — Health System

## Status
✅ Complete

## Completed

- Added HealthChecker service
- Added ProviderHealth data model
- Added `/health` endpoint
- Relay now owns HealthChecker
- Moved health logic from API into Relay

## Notes

Current health check is simulated.

It measures latency locally and always reports:

- healthy

Next milestone will replace the simulated check with a real HTTP request to NVIDIA.

---

# Platform P0 — Packaging & distribution

## Status
✅ Complete

## Completed

- Versioned the package (`0.1.0`) via `app/__version__.py` (PEP 440), re-exported from `app/__init__.py`.
- Added `pyproject.toml` (setuptools, dynamic version, `relay` console script, `app*` package discovery). Existing `python -m app.cli` usage is unchanged.
- Added first-run setup-state mechanism (`app/services/setup_state.py`) with three states — `not_configured`, `configured`, `incomplete` — stored in `<state_dir>/state.json` (atomic write), independent of `.env` presence.
- `app/core/config.py`: `RELAY_ENV_FILE` override, cwd-first `.env` resolution, `RELAY_STATE_DIR`, `RELAY_HOST`, `RELAY_PORT`.
- `app/cli.py`: `--version`, `relay setup`, and no-args dispatch (configured → start server; otherwise → first-run/setup). Setup now prints `Installation complete. Type 'relay' to start Relay.`
- One-command installers `install.ps1` and `install.sh`.
- `.env.example`, `requirements-dev.txt`, `.gitignore`, README and configuration docs updated.
- P0 tests (`tests/test_packaging.py`, 15 tests): packaging metadata, CLI entry point, version command, first-run detection, configured startup path, and a wheel-build + installed-console-script smoke test.

## Notes

- Full suite after P0: **836 passed, 5 skipped** (821 baseline + 15 new).
- PyPI publishing and winget/choco are deferred to a later phase; the installers and console entry point are designed for that path.

---

# Platform P2 — Main Relay Terminal Interface

## Status
✅ Complete

## Completed

- **P2a — Scaffold, Dashboard, navigation, wiring.** `app/ui/` package (Textual 8.2.8 confined to `app/ui`), `ServiceFacade` view-model layer, `RelayApp` with 7 tabs (1–7), header/footer, `EmbeddedServer` (daemon thread, cooperative stop), `reload_settings()` fixing the stale-singleton handoff after setup, and CLI wiring: `relay` no-args → TUI when configured / wizard when not, `relay tui`, setup→TUI handoff, `relay serve` unchanged.
- **P2b — Chat.** Random (`choose_provider`) and specific-model chat, streaming rendered into the chat view, provider/model badge + latency + errors, inline availability probe (✓/⚠/✗). All sync service calls run off the UI thread via `asyncio.to_thread`.
- **P2c — Models + Providers.** Model availability/priority controls and provider enable/disable persisting through `config_store` + in-process `reload_config(relay)`; provider add/re-run setup behind the TUI `SetupAdapter` (password-masked key entry, validated before persist); per-provider rescan via `ScanEngine`. No full-key rendering anywhere.
- **P2d — Configuration, Applications, Diagnostics.** Configuration form (routing `TASK_*`, failover/retry live-applied; server/persistence/log read-only with restart warning; `DEFAULT_PROVIDER` informational) writing through `config_store` with dry-run validate → apply → revert. Applications panel (endpoint/auth status + metadata-only client activity bucketed by UA heuristics into Cline/OpenCode/Continue/Other). Diagnostics panel (ops tail, redacted file-log tail, provider health deep view, per-provider test connection, redacted snapshot export with atomic file write).
- **P2e — Windows, polish, docs, full gate.** TTY/ConPTY preflight (`app/core/terminal.py`, UI-free) — the TUI degrades to printed guidance (`relay serve` or a real terminal) instead of crashing in non-interactive contexts; Windows detection covers Windows Terminal/VS Code (ConPTY), conhost consoles, and non-console contexts. Screens now read everything through `ServiceFacade` (zero `app.core`/`app.providers` imports in `app/ui/screens/`, enforced by a boundary test). Docs: `docs/tui-guide.md` (user guide), README, `PROJECT_LOG.md`, `docs/configuration.md`, `.env.example`, plan statuses, and a manual Windows smoke checklist (`tests/test_ui_windows_smoke.md`).

## Notes

- Full suite after P2: **1046 passed, 5 skipped** (1041 at P2d + 5 new P2e tests: 4 preflight + 1 CLI guard, plus the screen-boundary rule).
- Textual is importable only from `app/ui`; `app/ui/data.py`, `app/ui/theme.py`, `app/ui/keymap.py` stay Textual-free and `app/cli` + `app/core` import without Textual at runtime (boundary tests enforce all three).

---

# Platform P3 — Async Provider Layer (Hot Path)

## Status
✅ Complete

## Completed

- **P3a — Async provider clients**. All six provider clients (NVIDIA, OpenAI, LM Studio via `OpenAICompatibleClient`; Anthropic, Gemini, Ollama) now implement async methods:
  - `achat`, `achat_stream` (legacy message string API)
  - `achat_messages`, `achat_stream_messages` (full-payload message API)
  - `alist_models`, `aprobe_model`
  Error mapping, timeout handling, metrics recording, and retry-after logic mirror sync implementations exactly.

- **P3b — Chat service refactor & AsyncChatService**. Extracted pure decision helpers to `app/services/chat_policy.py` (shared by sync and async). `AsyncChatService` implements:
  - `achat_across`, `achat_across_stream` (legacy string API)
  - `achat_across_messages`, `achat_across_stream_messages` (full-payload API)
  - Full retry/failover/cancellation parity with `ChatService` (14 parity tests pass)
  - Cancellation-safe: 8 cancellation tests pass (mid-await, retry sleep, stream iteration, provider call)

- **P3c — Async API hot path & OpenAI-compatible streaming**.
  - `Relay.achat` async facade mirroring `Relay.chat` (telemetry, correlation IDs, request logging, health feedback preserved).
  - `/chat` and `/v1/chat/completions` converted to `async def`.
  - OpenAI-compatible async SSE streaming:
    - `data: {...}\n\n` wire format, chunk ordering preserved
    - Usage chunk passthrough
    - Mid-stream provider errors forwarded as error chunks
    - Client disconnect closes provider generator cleanly
    - Empty-stream failover to next candidate
  - All 13 new streaming tests pass.

- **Test updates**: All FakeClients across 6 test files updated with async methods (`achat`, `achat_stream`, `achat_messages`, `achat_stream_messages`) mirroring sync patterns.

## Notes

- **Full test gate**: 1031 passed, 7 skipped (UI tests needing `rich`/`textual` dependencies)
- **Compile check**: `python -m compileall app tests` — clean
- **Secret scan**: Clean (only test placeholder false positives)
- **Sync compatibility**: Zero sync behavior removed; `ChatService` and sync API remain as fallback; terminal interface (`relay` TUI) continues to use sync path via `asyncio.to_thread`
- **Architecture**: Dual-path (sync fallback + async primary) with shared pure policy layer (`chat_policy.py`)

---

# Architecture Principles

1. Relay owns business logic.
2. API routers remain thin.
3. Services perform work.
4. Providers only know how to communicate with providers.
5. Never implement a feature that cannot be tested immediately.

---

# Next Milestone

- Platform P4 — Future async enhancements (see `docs/platform-implementation-roadmap.md`).