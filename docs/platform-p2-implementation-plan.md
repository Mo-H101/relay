# P2 — Main Relay Terminal Interface — Implementation Plan

Date: 2026-08-03
Status: Approved — implementation in progress.
Phase source: `docs/platform-p2-design.md` (design spec) ·
`docs/platform-implementation-roadmap.md` (P2) · `docs/platform-p1-completion-report.md` (pre-P2 actions).

---

## 1. Verified seams (what the TUI consumes)

| Concern | Verified seam |
| --- | --- |
| Facade singleton | `relay = Relay()` at `app/core/relay.py:341`; imported only by server/TUI paths (never by `app.cli`) |
| Chat (random) | `relay.choose_provider()` + pick chat-testable model (`relay.py:177`) |
| Chat (specific + streaming) | `relay.chat_service.chat_across([(p, m)], msg)` / `chat_across_stream_messages(...)` — sync, wrapped via `asyncio.to_thread` |
| Health / deep probe | `relay.health(deep=True)` (`relay.py:302`); `relay.health_checker.check(provider, deep=True)` |
| Availability states | `relay.health_store.get(name).models` + `.relay/availability.json` via `app.setup.persistence` |
| Provider config writes | `config_store.set_provider_config(defn, enabled/api_key/priority_models)` (`config_store.py:35`); single writer |
| Config apply/reload | **`reload_config(relay)` in-process** (`app/services/reload.py:275`) — allowlist, provider side effects, routing refresh, rollback, redacted errors |
| Wizard reuse | `wizard._configure_provider(ui, defn, store)` + `run_setup(ui)` behind the `UI` protocol (`app/setup/ui.py:24`) |
| Key validation | `key_validation.resolve_cloud_key(ui, defn, client, provider, current_key)` |
| Model scan | `ScanEngine().scan(client, provider, models, on_update=...)` + `write_snapshot(defn.id, results)` |
| Diagnostics export | `DiagnosticsService().build_snapshot(relay)` → redacted `.relay/diagnostics-<ts>.json` |
| Activity (Apps panel) | `ops_store.record_http(...)` + `MetricsMiddleware` (`app/api/middleware.py:19`) — additive UA/auth-scheme fields |
| Settings surface | `app/core/config.py` hand-rolled `Settings`; `settings` singleton; add `RELAY_TUI_NO_EMBED` |
| Server lifecycle | `app.main.app` FastAPI instance + `lifespan` (starts refresher/flusher, final flush on stop) |

**Critical finding:** the pre-P2 setup→serve handoff served with *stale* settings —
`app.cli` imports `app.core.config.settings` at module load (pre-setup), and `.env`
writes never refresh the in-memory singleton (`load_dotenv` runs once, `dotenv.set_key`
does not touch `os.environ`). P1 tests masked this by monkeypatching `_cmd_serve`. P2
must fix it or the TUI shows a configured machine as empty. Fix: `reload_settings()`
(`app/core/config.py`) re-runs `load_dotenv(env_file, override=True)` and re-executes
`Settings.__init__` **in place** on the singleton before the TUI imports the `relay`
facade, so the first `Relay()` construction sees post-setup configuration.

---

## 2. Dependencies

- `requirements.txt` + `pyproject.toml`: `textual==8.2.8`
- `requirements-dev.txt`: `pytest-asyncio==1.4.0`
- Install into `.venv`.
- Note: the installed `textual-builder` skill documents the 6.6.x API; Textual is now
  8.2.8. Core concepts hold (App/Screen/compose/BINDINGS, DataTable, Input,
  ModalScreen, `run_test()`/`pilot`). Verify against the official 8.x docs during
  implementation.

---

## 3. Files

**New `app/ui/`** (the only package allowed to import Textual):

```
app/ui/__init__.py
app/ui/app.py              # RelayApp(App): header/footer, 7 tabs, global keymap, error surface, server hooks
app/ui/data.py             # Textual-free: view-model dataclasses + ServiceFacade (only thing screens call)
app/ui/theme.py            # palette object (P9 seam)
app/ui/keymap.py           # binding constants (P9 seam)
app/ui/setup_adapter.py    # TUI-backed UI implementing app.setup.ui.UI → modals/status bars (P2c)
app/ui/screens/{dashboard,chat,models,providers,configuration,applications,diagnostics}.py
app/ui/widgets/{status_panel,model_table,provider_table,chat_view,confirm_modal,key_entry_modal,log_view}.py
```

**New/modified core (UI-free):**

- `app/core/server.py` — `EmbeddedServer`: `uvicorn.Server(config)` in a daemon
  thread; `start()` (waits `server.started`), `stop()` (`should_exit=True`, join with
  timeout). `access_log=False`, `log_config=None` so server logs never corrupt the TUI
  screen. uvicorn's `capture_signals` no-ops off the main thread, so no signal
  handlers are installed (keeps Windows Ctrl+C clean).
- `app/core/config.py` — add `relay_tui_no_embed` (`RELAY_TUI_NO_EMBED`); add
  `reload_settings()` (see §1). Raises `ValueError` on invalid env.
- `app/api/middleware.py` + `app/services/ops_store.py` — **additive** `user_agent`
  (trimmed) and `auth_scheme` ("bearer"/"header"/"none") on `OpsEvent`; middleware
  reads the UA header and the Authorization *scheme* only (never the token, never
  bodies). Privacy contract documented + tested.
- `app/cli.py` — `_cmd_tui()`; `relay tui` subcommand; no-args configured → TUI;
  `relay setup` usable → `reload_settings()` then TUI; `relay serve` unchanged
  byte-for-byte.

**Tests:** `test_ui_boundary.py`, `test_ui_data.py`, `test_ui_app.py` (pytest-asyncio +
`textual.pilot`), `tests/test_ui_windows_smoke.md` (manual checklist, P2e).

---

## 4. Phases

### P2a — Scaffold, Dashboard, navigation, wiring

- `app/ui` scaffold, `ServiceFacade` + dashboard view-model, `RelayApp` with 7 tabs
  (1–7), header/footer, `EmbeddedServer`, `reload_settings()`, CLI wiring
  (`relay` no-args, `relay tui`, setup→TUI), README + `--help` documentation.
- Update P0 expectations: `test_packaging.py::test_configured_execution_path_serves`
  → asserts TUI; `test_setup_wizard.py::test_cli_hands_off_to_serve` → handoff to TUI.
  Add a regression test proving the singleton is fresh post-setup (the staleness fix).
- **Gate:** TUI boots headless; 7 screens mount; dashboard tiles populate from a fake
  facade; boundary + packaging + wizard suites green.

### P2b — Chat

- Random-mode (`choose_provider`), specific-model picker, streaming rendered into
  `chat_view` (chunks posted to the UI thread from a worker; all sync service calls
  via `asyncio.to_thread`), provider/model badge + latency + errors, inline
  availability test (`ScanEngine` single probe → `✓/⚠/✗`).
- **Gate:** random + specific + streaming + test-model flows green under `pilot`.

### P2c — Models + Providers

- Models: on-demand union list, `✓/⚠/✗` merged from health_store + availability
  snapshot, enable/disable + priority reorder via `config_store.set_provider_config`
  then `reload_config(relay)` (priority restricted to available models — P1 rule).
- Providers: add / re-run setup via `run_setup`/`_configure_provider` behind
  `setup_adapter`; masked key entry → `validate_key` → persist only on success;
  rescan via `ScanEngine`.
- **Gate:** enable/disable + priority persist and reload; provider flows with fake
  clients; no full-key rendering.

### P2d — Configuration + Applications + Diagnostics

- Configuration: routing `TASK_*`, failover/retry, and server settings form →
  `config_store` + in-process `reload_config(relay)`; `RELAY_HOST/PORT` read-only +
  restart warning; `DEFAULT_PROVIDER` informational only (no silent behavior change).
- Applications: endpoint/auth status + client-activity table (Cline/OpenCode/
  Continue/Other heuristics on trimmed UA).
- Diagnostics: tail of ops events/file log; provider health deep view; per-provider
  test connection; export redacted snapshot to file.
- **Gate:** config writes apply with reload report; apps bucketing; export writes +
  redacts.

### P2e — Windows, polish, docs, full gate

- ConPTY/non-TTY detection: degrade to printed guidance (`relay serve` or a real
  terminal) instead of a broken screen.
- theme/keymap polish; P9 seams documented; README, `PROJECT_LOG.md`, `.env.example`,
  `docs/configuration.md`; manual Windows smoke checklist.
- **Gate:** full `pytest tests -q` green; packaging + wizard + boundary suites green;
  DoD §13 items 1–9.

---

## 5. Key decisions

- **D-A:** `reload_settings()` after wizard completion fixes the stale-singleton
  handoff bug (§1) and keeps TUI + embedded server on one shared `relay` instance.
- **D-B:** embedded server runs `app.main.app` directly (same module → shared
  `relay`), logs disabled at the uvicorn layer, cooperative exit;
  `RELAY_TUI_NO_EMBED=1` escape hatch.
- **D-C:** activity capture extends `OpsEvent` additively (bounded store already
  exists) — metadata only; no auth values, no bodies; documented privacy delta vs.
  the v1.0.0 readiness report and covered by a test.
- **D-D:** Config panel uses in-process `reload_config(relay)` (not the HTTP
  `/admin/reload` path) since the TUI shares the process.
- **D-E:** `relay` no-args behavior change (server → TUI) is intentional per design
  §3.3; `relay serve` preserves the old path exactly; docs + P0 test updates land in
  P2a.
- **D-F:** Textual confined to `app/ui`; `data.py`/`theme.py` stay Textual-free;
  import-graph boundary test enforced at gate.
- **D-G:** No mascot, cursor-following animation, or personality code in P2 — the P9
  idea is deferred by design; `theme.py`/`keymap.py` exist only as swappable seams.

---

## 6. Risks & mitigations (P2-specific)

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Textual 8.2.8 API drift from skill docs | UI broken / wasted work | Verify against official 8.x docs; pilot smoke in gate |
| Embedded server shutdown hang | TUI exits leave orphan server | `should_exit` + join timeout + dedicated test; `RELAY_TUI_NO_EMBED=1` |
| Long scans/chat block UI | Frozen interface | All long ops via `asyncio.to_thread` |
| Singleton staleness after setup | TUI shows empty config | `reload_settings()` + fresh-construction ordering (TUI imports `app.core.relay` lazily) |
| Scope creep into P5/P6/P7 | Oversized phase | Explicit non-goals honored; interim panels labeled |

---

## 7. Commits

One commit per phase boundary (P2a → P2e), each with its gate green, ending with the
P2 completion report + `PROJECT_LOG.md` entry.
