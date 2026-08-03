# P2 — Main Relay Terminal Interface — Design Specification & Implementation Plan

Date: 2026-08-03
Status: Planning only — no code written.
Phase source: `docs/platform-implementation-roadmap.md` (P2) · product spec §0.4 · user P2 requirements (this doc).
Depends on: P1 (setup wizard, provider registry, availability scanning, config_store).

---

## 1. Purpose, scope, and non-goals

### Goal
Ship the main Relay user interface: a full-screen terminal UI giving the user a
Dashboard, Chat, Models, Providers, Configuration, Applications, and Diagnostics
— with `relay` as the primary command and an optional explicit `relay tui`.

### Hard boundary (non-negotiable)
**The UI layer is a presentation layer only. Core services, providers, routing,
keys, scanning, the setup wizard, and the API must never import or depend on the
UI package.** Dependencies flow one way: `app/ui → core/services/providers/api`.
This is enforced by an import-graph test (§10) and is the property that keeps P9
personality features additive rather than invasive.

### In scope (P2)
- `relay` (no args, configured) launches the TUI. `relay tui` explicit command.
- Setup → main interface handoff without restart.
- Dashboard, Chat, Models, Providers, Configuration, Applications, Diagnostics.
- Windows terminal support (ConPTY-aware, graceful degradation).

### Non-goals (deliberate deferrals — do not build here)
- **App-key CRUD / upstream keyring / scoped keys** — P5. P2 *reuses* the P1
  key flow (`config_store` + `key_validation`); keys stay masked, `.env` interim.
- **Durable `model_status` DB and app identity from `request_log`** — P6. P2 shows
  availability from in-memory health + `.relay/availability.json`; app detection is
  heuristic (see Applications panel).
- **Full `relay config` CLI commands** — P7. P2 ships the TUI Config panel that
  writes via `config_store` + the existing `/admin/reload` reload path.
- **Async hot path** — P3. The TUI calls existing sync services inside worker
  threads so the UI never blocks.
- **Mascot, cursor awareness, animated identity, assistant personality** — P9.
  The P2 architecture must make them possible, not implement them (§11).

---

## 2. Recommendation: Rich vs Textual

### What already exists
- `rich` is a runtime dependency (P1) and is correct where it already lives:
  wizard progress bars, CLI status, diagnostics rendering.
- `textual` is **not** installed. Python 3.12, Windows target.

### Evaluation
| Need of the main interface | Rich alone | Textual |
| --- | --- | --- |
| Full-screen multi-panel dashboard with live refresh | Manual `Live` re-rendering, no layout engine | Native dock/grid layouts + reactive re-render |
| Keyboard navigation, focus, keybindings | Not provided | First-class |
| Text input, forms, modal dialogs, menus | Not provided | Built-in `Input`, `ModalScreen`, selection widgets |
| Scrollable list/table with filter & selection | Rebuilt by hand | `DataTable`, `ListView`, `Select` |
| Streaming chat display | Possible, clunky | `RichLog`/`Markdown` widgets designed for it |
| Theming / accessibility / focus tracking | Not provided | CSS-based themes, screen readers, focus API |
| Automated UI testing | n/a | `App.run_test()` + `textual.pilot` |
| Windows (ConPTY) support | n/a (ANSI only) | Supported via Windows Terminal / VS Code / Win11 conhost |

### Verdict
**Use Textual for the main interface; keep Rich for the CLI/scripting layer.**
Building the required interface (input box + scrollback + streaming chat, model
tables with sort/filter, provider forms, config forms, modals, diagnostics log
viewer) in bare Rich means re-implementing a widget toolkit — high effort, high
bug surface, and a worse result. Textual is Rich's interactive sibling, so the
visual language stays consistent and the incremental dependency is small.

**Constraint:** Textual is confined to `app/ui/` (§3). Nothing else imports it.

---

## 3. Architecture

### 3.1 Package layout

```
app/
├── core/            # unchanged, plus ONE new module (no UI imports)
│   ├── server.py    # NEW: embedded uvicorn lifecycle (start/stop) for TUI runs
│   └── relay.py     # unchanged facade (chat, health, choose_provider, stores)
├── services/        # unchanged (read surfaces the TUI consumes)
├── providers/       # unchanged (registry, availability, clients)
├── setup/           # unchanged (P1 wizard functions REUSED by Providers panel)
├── api/             # unchanged, plus tiny additive hooks (§3.4)
└── ui/              # NEW — the ONLY package that may import Textual
    ├── __init__.py          # empty (data.py stays Textual-free for tests)
    ├── app.py               # RelayApp(Textual.App): header, tabs, footer, keymap
    ├── data.py              # Textual-free: view-model dataclasses + ServiceFacade
    ├── theme.py             # palette/theme object (P9 seam)
    ├── keymap.py            # binding constants (P9 seam)
    ├── screens/
    │   ├── dashboard.py
    │   ├── chat.py
    │   ├── models.py
    │   ├── providers.py
    │   ├── configuration.py
    │   ├── applications.py
    │   └── diagnostics.py
    └── widgets/
        ├── status_panel.py    # dashboard tiles
        ├── model_table.py     # availability-colored model list
        ├── provider_table.py
        ├── chat_view.py       # scrollback + streaming
        ├── confirm_modal.py
        ├── key_entry_modal.py # masked input, reuses key_validation
        └── log_view.py
```

### 3.2 The boundary invariant
- `app/ui/**` may import `app.core.*`, `app.services.*`, `app.providers.*`,
  `app.setup.*`, `app.api.*` — **read-only** from services, plus the specific
  operations listed in §3.5.
- **No module outside `app/ui` may import `app.ui`.** Verified by
  `tests/test_ui_boundary.py` (subprocess import-graph check, mirroring the P0
  packaging test pattern): importing `app.main`, `app.services`,
  `app.providers`, `app.api`, `app.setup`, `app.cli` must not load `app.ui`.
- `app/ui/data.py` and `app/ui/theme.py` are **Textual-free** so view-model logic
  is unit-testable without booting a TUI.

### 3.3 Process model — `relay` as the primary command

```
relay (no args)
 ├─ not configured  → wizard (unchanged) → on usable result → TUI
 ├─ incomplete      → wizard resume       → on usable result → TUI
 └─ configured      → TUI                 ← NEW (was: plain server)
relay tui           → ensure configured (wizard if needed) → TUI
relay setup         → wizard → on usable result → TUI       ← changed handoff
relay serve         → plain uvicorn server (UNCHANGED, headless path)
```

- **Embedded API server (recommended):** the TUI process runs the API server on a
  dedicated thread using the *already-constructed* `relay` singleton
  (`uvicorn.Server` + `uvicorn.Config(app.main.app, ...)` in a thread; exit via
  `server.should_exit = True`). Because `app.main` re-import resolves to the
  cached module, the server and the TUI share one `relay` instance — the
  Dashboard's "API endpoint status" and external clients (Cline/OpenCode/…) see
  the same live state. This satisfies spec req 9 (no restart after setup) and
  req 17 (status panel) without a second process.
- **Escape hatch:** `RELAY_TUI_NO_EMBED=1` → TUI runs UI-only and expects a
  separately running `relay serve` (e.g., managed by a service manager).
- Server thread lifecycle: `start()` → thread + `server.serve()`; TUI quit →
  `server.should_exit = True` + join. No signal handlers are installed in the
  thread (keeps Windows Ctrl+C clean); the lifespan start/stop hooks still run.
- **Behavior change notice:** `relay` no longer prints the server log line /
  blocks as a bare server. `relay serve` preserves that exactly. Documented in
  README and the TUI footer.

### 3.4 Additive API/middleware hooks (small, core-adjacent, UI-free)
- **Client-activity capture** for the Applications panel: extend
  `MetricsMiddleware` to record, per request, `path`, `method`, `user_agent`
  (trimmed), `auth_scheme`, `status_code`, `latency_ms` into a small bounded
  in-memory store (or `ops_store`). **Metadata only — never bodies or keys.**
  No UI dependency; the Applications panel reads it.
- No other endpoint changes required: P2 reads via services directly.

### 3.5 Service operations the TUI may call (existing seams)
| UI action | Existing seam |
| --- | --- |
| Random available model for Chat | `relay.choose_provider()` + pick from `provider.models` |
| Chat with specific model | `relay.chat_service.chat_across([(provider, model)], msg)` |
| Streaming chat | `relay.chat_service.chat_across_stream_messages(...)` |
| Model test / availability probe | `ScanEngine` single probe or `relay.health_checker.check(provider)` |
| Availability states `✓/⚠/✗` | `relay.health_store.get(name).models` + `.relay/availability.json` |
| Provider add / re-run setup / key update | `app.setup.wizard._configure_provider(ui, defn, config_store)` with a TUI-backed `UI` adapter |
| Key validation | `app.setup.key_validation.validate_key` / `resolve_cloud_key` |
| Rescan models | `app.setup.scan.ScanEngine.scan(...)` |
| Provider/model enable-disable, priority | `config_store.set_provider_config(...)` then `relay.routing.refresh()` + provider re-load note |
| Health, test connection | `relay.health(deep=True)`, `relay.health_checker.check(provider)` |
| Diagnostic export | `DiagnosticsService().build_snapshot(relay)` → write file |
| Logs | `logging` handlers + `RequestLogger` / `ops_store.events()` |
| Routing rules / failover / default model writes | `config_store` env write + existing `/admin/reload` (reload service) |

All long operations (scans, key validation, chat) run via `asyncio.to_thread`
inside the TUI so the event loop never blocks.

### 3.6 TUI→services adapter
`app/ui/data.py` defines:
- **View-model dataclasses** (DashboardStats, ModelRow, ProviderRow, AppActivity,
  ConfigSnapshot, DiagnosticReport, …) — pure data, `Textual`-free.
- **`ServiceFacade`** — assembles view-models from `relay`/`settings`/`setup`
  seams. Screens depend on `ServiceFacade`, never on services directly. Tests
  inject a fake facade; no TUI required for view-model tests.

---

## 4. Screen list

| # | Screen | Requirement source | Primary content |
| --- | --- | --- | --- |
| 1 | **Dashboard** | §0.4/17 | Relay status, connected providers, available-models count, routing status, API endpoint status, connected apps summary |
| 2 | **Chat** | §0.4/10–11 | Chat with random available model; pick specific model; provider/model badge; latency & errors; test model availability |
| 3 | **Models** | §0.4/14, 24 | Available models on demand; priority management; enable/disable; `✓/⚠/✗` availability |
| 4 | **Providers** | §0.4/12–13 | Add/remove; re-run setup; update keys; validate keys; rescan models |
| 5 | **Configuration** | §0.4/18 | Routing rules; default model; failover settings; server settings |
| 6 | **Applications** | §0.4/16 | Connected clients (Cline/OpenCode/Continue/other), connection status |
| 7 | **Diagnostics** | §0.4/17 | Logs; provider health; test connection; export diagnostic report |

---

## 5. Navigation flow

```
┌────────────────────────────── Header: Relay • <screen> • <status> ─────────────┐
│  Tabs (key 1..7 or Ctrl+1..7):                                                  │
│  [Dashboard] [Chat] [Models] [Providers] [Config] [Apps] [Diagnostics]          │
├──────────────────────────────────────────────────────────────────────────────────┤
│                                    active screen                                 │
├──────────────────────────────────────────────────────────────────────────────────┤
│  Footer: q quit · 1..7 screens · t test model · s scan · e export · F1 help      │
└──────────────────────────────────────────────────────────────────────────────────┘

Cross-screen flows
  Dashboard → (click/tab) any screen
  Chat      → "test model" → availability probe → result inline
  Models    → enable/disable → config_store → row refresh
  Models    → edit priority → modal reorder → config_store + routing.refresh()
  Providers → add / re-run setup → wizard flow (TUI-backed UI) → scan → refresh
  Providers → validate key / update key → key_entry_modal → live validation
  Config    → edit routing/default/failover → config_store + /admin/reload
  Apps      → endpoint status + activity refresh
  Diag      → test connection per provider; export report → file
```

Keymap is centralized in `app/ui/keymap.py` (P9 seam §11).

---

## 6. Per-screen data flows

### Dashboard
- **Relay status:** setup-state marker + uptime + `__version__`; running/server thread state.
- **Connected providers:** enabled per `settings` toggles (registry) vs. runtime-registered (`relay.provider_manager.all()`).
- **Available models count:** sum of provider model catalogs; refined by availability snapshot count.
- **Routing status:** `relay.routing.is_enabled()` + configured `TASK_*` refs.
- **API endpoint status:** local `GET /health` (public) → `ok/degraded/unavailable`; server thread alive.
- **Connected applications:** count + summary from §3.4 activity store.

### Chat
- Random mode: `relay.choose_provider()` → random chat-testable model from that provider.
- Specific mode: model picker (from Models data) → `chat_across([(provider, model)], ...)`.
- Streaming: `chat_across_stream_messages(...)` rendered into `chat_view`.
- Badge shows provider + model of the completed request; latency from attempt `latency_ms`; errors from `result["error"]` / `fallback_reason`.
- "Test model availability": single probe via ScanEngine → inline `✓/⚠/✗`.

### Models
- List: union of provider models (on demand, not at boot).
- Priority: reorder over **available** models only (P1 rule preserved) → `config_store.set_provider_config(priority_models=...)` + `relay.routing.refresh()`.
- Enable/disable: `config_store` toggle; row reflects after reload.
- Availability: merge `relay.health_store.get(name).models` status + `availability.json` snapshot → `✓/⚠/✗`; stale/unknown → `✗` with tooltip.

### Providers
- Add / remove / re-run setup: reuse `app.setup.wizard._configure_provider` behind a TUI-backed `UI` adapter (menu, key entry, notices render as modals/status).
- Update key: `key_entry_modal` (masked) → `validate_key` → persist via `config_store` only on success.
- Validate key: live `validate_key` per provider, classified reason on failure.
- Scan again: `ScanEngine.scan` on the provider's models → snapshot rewrite + health update.

### Configuration
- Routing rules: `TASK_CODING/VISION/REASONING/GENERAL/CREATIVE/TRANSLATION` + `TASK_ROUTING_ENABLED` → form → `config_store` + reload.
- Default model: `DEFAULT_PROVIDER` (currently parsed-but-unused) — surfaced as informational in P2 with a documented decision on activation (do not silently change routing behavior).
- Failover: `MAX_RETRIES`, retry/backoff/budget settings → form → `config_store` + reload.
- Server settings: `RELAY_HOST`/`RELAY_PORT` — read-only display + restart warning (no live rebind in P2).

### Applications
- Connection status: endpoint URL, auth mode (`RELAY_API_KEY` on/off), live `/health`.
- Client activity: bounded table from §3.4 (path, UA, scheme, count, last-seen). Heuristic labels for Cline/OpenCode/Continue UAs; "Other OpenAI-compatible" bucket. Note: full identity from labeled keys lands in P5/P6.

### Diagnostics
- Logs: tail of structured logs (RequestLogger/`ops_store`) + file log if configured.
- Provider health: `relay.health(deep=True)` rendering.
- Test connection: per-provider `health_checker.check`.
- Export: `DiagnosticsService().build_snapshot(relay)` → timestamped file (e.g., `.relay/diagnostics-<ts>.json`); masked/redacted by construction.

---

## 7. Component breakdown

| Component | Responsibility |
| --- | --- |
| `app/ui/app.py` | `RelayApp`: binds screens to tabs, header/footer, embedded-server start/stop hooks, global keymap, error surface |
| `app/ui/data.py` | View-model dataclasses + `ServiceFacade` (Textual-free) |
| `app/ui/screens/*` | One Textual `Screen` per tab; thin — all logic in `data.py` |
| `app/ui/widgets/*` | Reusable widgets (status tiles, model/provider tables, chat view, modals, log view) |
| `app/ui/theme.py`, `app/ui/keymap.py` | Swappable palette + bindings (P9 seams) |
| `app/core/server.py` | Embedded uvicorn thread lifecycle (no UI imports) |
| `app/api/middleware.py` | + client-activity metadata capture (additive) |
| `app/cli.py` | `relay` → TUI when configured; `relay tui`; setup → TUI handoff; `relay serve` unchanged |

---

## 8. Dependencies

| Dependency | Kind | Purpose | Notes |
| --- | --- | --- | --- |
| `textual` (pin at install) | runtime | TUI framework (pulls rich, already present) | Confined to `app/ui` |
| `pytest-asyncio` | dev | `App.run_test()` / `textual.pilot` async tests | Also needed by P3 |
| none else | — | chat, health, models, keys, scanning, config all reuse existing seams | no new HTTP/SDK deps |

Add `textual` to `requirements.txt` + `pyproject.toml`; `pytest-asyncio` to
`requirements-dev.txt`.

### Windows notes
- Requires a ConPTY terminal: Windows Terminal, VS Code integrated terminal, or
  Windows 11 conhost. Older legacy consoles degrade: `run_tui()` detects a
  non-TTY/unsupported terminal and prints guidance (use a real terminal or
  `relay serve`).
- `✓ ⚠ ✗` glyphs and streaming render correctly under Windows Terminal; theme
  avoids truecolor-only constructs where possible.
- Embedded server thread avoids signal handlers; exit is cooperative
  (`should_exit`), so Ctrl+C in the TUI stays clean.

---

## 9. Risks & mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Textual lifecycle/render churn on Windows | UI broken on some terminals | ConPTY detection + fallback; pilot smoke in CI; Windows manual smoke in DoD |
| Embedded uvicorn thread races (double lifespan, shutdown hangs) | TUI exits leave orphan server | `should_exit` join with timeout; `RELAY_TUI_NO_EMBED=1` escape hatch; dedicated test |
| Long operations block UI loop | Frozen interface during scans/chats | All ops via `asyncio.to_thread` (ScanEngine already thread-pool) |
| `relay` no-args behavior change (was plain server) | Surprise for script users | `relay serve` preserved + documented; TUI footer hint |
| Config writes mutate `.env` concurrently | Drift / lost writes | Single-writer `config_store` (P1) + reload; only one mutation surface in UI |
| Keys shown in UI | Secret leak | Masked always; no full-key rendering; masked in export |
| Scope creep into P5/P6/P7 | Oversized phase | Explicit non-goals (§1); interim panels clearly labeled |
| UI ↔ core coupling drifts | Boundary breaks | Import-graph test enforced at gate |

---

## 10. Testing strategy

No new framework beyond pytest + `pytest-asyncio`.

- **`tests/test_ui_boundary.py`** — subprocess import-graph check: importing
  `app.main`, `app.services`, `app.providers`, `app.api`, `app.setup`,
  `app.cli` must not load `app.ui` (P0-test pattern).
- **`tests/test_ui_data.py`** — `ServiceFacade`/view-model assembly against a
  fake `relay` (no Textual import; pure pytest).
- **`tests/test_ui_app.py`** (`pytest-asyncio` + `textual.pilot`):
  - boot `RelayApp` headless; assert header/tabs/footer render;
  - navigate every screen (keys 1–7) and assert content mounts;
  - dashboard tiles populate from a fake facade;
  - chat: random-model flow and specific-model flow with a canned provider;
    latency/error shown; streaming chunk-by-chunk;
  - models: enable/disable and priority reorder persist via `config_store` and
    row state updates;
  - providers: add/re-run/validate-key/scan flows with TUI-backed UI adapter;
  - diagnostics: export writes a file, connection test shows status.
- **`tests/test_ui_windows_smoke.md`** (manual checklist) + CI headless boot.
- **Regression gate:** full `pytest tests -q` green; P0 packaging/console-script
  tests green (`relay serve`, `--version`, `--help` unchanged); wizard tests
  green (handoff target updated to TUI).

---

## 11. P9 readiness — "Relay Identity / Interactive Terminal Experience"

Recorded, **not implemented** in P2. The P2 architecture keeps it additive:
- **`app/ui/keymap.py`** — all bindings in one swappable module (identity can
  remap/intercept keys without touching screens).
- **`app/ui/theme.py`** — palette/identity object passed to widgets; a mascot
  banner or animated footer is a widget addition, not a core change.
- **Presentation-only rule** — `app/core`, `app/services`, `app/providers`,
  `app/api`, `app/setup` must never learn about mascot/personality; the boundary
  test (§3.2/§10) guarantees it.
- Cursor awareness and interactive-assistant behavior would live in `app/ui`
  (widgets/screens) on top of `ServiceFacade`; no service changes required.

---

## 12. Sequencing within P2

| Step | Deliverable | Gate |
| --- | --- | --- |
| P2a | Scaffold `app/ui`, `app/core/server.py`, `relay tui` + `relay` wiring, Dashboard, navigation, boundary + pilot smoke tests | TUI boots; gate green |
| P2b | Chat screen (random + specific + streaming + latency/errors + model test) | Chat flows tested |
| P2c | Models + Providers screens (availability, priority, enable/disable, keys, rescan) | Persistence via config_store verified |
| P2d | Configuration + Applications + Diagnostics (interim sources) | All 7 screens functional |
| P2e | Windows smoke, theme/keymap polish, P9 seams documented, README/PROJECT_LOG, full gate | DoD |

---

## 13. Definition of done (P2)

1. `relay` on a configured machine opens the TUI; `relay tui` works; `relay serve`
   is byte-for-byte the old server behavior.
2. After setup completes, the TUI opens immediately (no restart); embedded server
   serves external clients; Dashboard "API endpoint status" reflects it.
3. All 7 screens mount and function per §6.
4. Chat works: random + specific model, streaming, provider/model badge, latency,
   errors, and inline availability test.
5. Models: on-demand list, `✓/⚠/✗`, enable/disable and priority persist via
   `config_store`; Providers: add/remove/re-run/update+validate keys/scan.
6. Configuration writes apply via `config_store` + reload (restart-warn for host/port).
7. Applications shows endpoint/auth status + client activity; Diagnostics exports a
   redacted report.
8. Boundary test green: no core → UI imports.
9. Full suite green (901 baseline + new); packaging + pilot smoke green; docs and
   PROJECT_LOG updated; P9 hooks (keymap/theme) present, no personality code.
