# Relay — Implementation Roadmap (Phase 9, Deliverable 3, rev 2)

Date: 2026-08-03 (rev 2 — updated after requirements validation pass).
Analysis only — no code changed yet.

Depends on: `docs/platform-architecture-report.md`,
`docs/platform-missing-components-report.md` (§1 validation matrix).
Sequence rationale: `docs/platform-recommended-order.md`.

Regression gate for every phase: existing **821 tests stay green**; `/v1`
wire behavior, `RELAY_API_KEY` bootstrap, `.env` compat, and
`python -m app.cli` / `python -m uvicorn app.main:app` entry points keep
working.

---

## P0 — Packaging & distribution

- `pyproject.toml` (build backend + `[project]` metadata + pinned deps);
  `[project.scripts] relay = "app.cli:main"`; single version source
  (`app/__version__`).
- **One-command GitHub install** verified: `pip install git+https://github.
  com/<org>/<repo>`; README + install hook tell the user to **type `relay`**
  afterwards.
- `requirements.txt` → generated pin list mirroring `pyproject`.
- Tests: packaging smoke (build wheel/sdist, install into a temp venv, run
  `relay --help`).
- Exit: `relay` is a real command after a single install.

## P1 — First-run experience & CLI (Typer + Rich)

- New `app/cli/` package; `app/cli.py` kept as shim for
  `python -m app.cli`.
- **Bare `relay` (no args)**: config-completeness check → if incomplete,
  auto-start the setup wizard; if complete, launch the main interface.
- **Setup wizard (product spec §0.2):**
  - providers offered **one by one**, each skippable;
  - API key entry with **live validation**; failures classified
    (`auth_error` / `connectivity` / `quota`) with clear messages; prompt
    **retry or skip**;
  - on success: fetch catalog → print **total discovered**;
  - progressive availability test with **one global progress bar** and the
    **current model name rendered dynamically beneath it** (never one bar
    per model); async catalog/probe scans;
  - final summary **Total / Available / Unavailable**; optional expansion
    of the detailed `✓`/`⚠`/`✗` list; **no model dump by default**;
  - results persisted into `model_status` (P6) and the handoff point into
    the main interface recorded.
- Subcommands: `serve`, `status`, `providers`, `models`, `keys`, `routing`,
  `apps`, `config`, `logs`, `test`.
- Tests: `typer.testing.CliRunner` per subcommand; wizard flow tests
  (mock catalog/probes); progress-bar/UX assertions.
- Exit: `relay` → wizard → summary; config completeness detected; every
  subcommand functional.

## P2 — Terminal UI — the main interface (Textual)

- Screens matching §0.4: **Chat (random available model)**, **Model test**,
  Providers, Keys, **Model priority**, **Routing rules**, **Connected
  applications**, **System status**, Configuration.
- Read-only panels first; interactive controls as P5/P6 data surfaces land.
- **Setup → main interface handoff** (requirement 9): after the wizard
  completes, Relay launches the TUI directly — no second command.
- `relay tui` entry; `textual.pilot` smoke tests (boot, render, exit).
- Exit: live dashboard with all panels; chat works against a running Relay.

## P3 — Async provider layer (hot path)

- `ChatService` async failover loop; streaming async generator; endpoints
  `/v1/chat/completions`, `/v1/models`, `/chat` → `async def`.
- Health/refresh: async in-loop probes or sync background thread
  (decision point); stores stay lock-safe; no blocking calls on the loop.
- Tests: async integration, stream, failover (`pytest-asyncio`).
- Exit: `/v1` latency/long-stream parity or better; threadpool contention
  gone. **Note:** async-first provider clients already land in P4, so this
  phase migrates the hot path (service + endpoints) only.

## P4 — Provider integrations (async-first)

- Provider system **de-string-named**: stable id slugs, registry/factories
  keyed by id; reload path updated.
- **Async-first clients**: `achat`, `alist_models`, `aprobe_model`,
  `achat_stream` built now — this satisfies the "async operations for slow
  provider scans" requirement and unblocks P1 wizard + P2 panels.
- Add Anthropic (Messages), OpenRouter, Groq, Ollama, custom
  OpenAI-compatible via config; wire unused parsed keys
  (`ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`,
  `OLLAMA_BASE_URL`); update `.env.example`, `configuration.md`,
  `relay providers`.
- Tests: per-provider fixtures; mock HTTP.
- Exit: every provider selectable/routable; scans are async.

## P5 — API-key security

- **App keys**: `api_keys` table (scrypt/pbkdf2, per-key salt, label,
  scopes, expiry, rotation, revoke); `relay keys`; admin API
  (`POST/GET /admin/keys`, `DELETE /admin/keys/{id}`); `auth.py`
  store-backed lookup with constant-time compare; `RELAY_API_KEY` bootstrap
  retained; per-key correlation into metrics/`request_log`.
- **Upstream provider keys**: move out of plaintext `.env` into OS keyring
  (`keyring`) or encrypted store with local master key; `.env` stays a
  supported bootstrap/compat path (decision documented).
- **Client integration keys**: `relay keys create --label "opencode"` flow
  so Cline/OpenCode/Continue get a scoped Relay key (requirement 20).
- Tests: hash/verify, expiry, revoke, scope enforcement, constant-time,
  privacy (no key material/prompts persisted), keyring round-trip.
- Exit: `relay keys create` key works end-to-end against `/v1`.

## P6 — Platform database + model availability + usage

- Migrations framework on `platform.db`; tables: `api_keys` (from P5),
  `request_log` (metadata only — privacy contract), **`model_status`**,
  `events`.
- **Model availability tracking** (requirement 24): durable 3-state status
  (`✓` available / `⚠` overloaded / `✗` unavailable) fed by setup probes,
  health reports, and learned feedback; health-band → UI-status mapping.
- **Connected applications** (requirement 16): apps = labeled keys ×
  `request_log.api_key_id`; `relay apps` view.
- Durable request log + retention; readers for CLI/TUI.
- Tests: migration up/down, pruning, metadata-only guarantee,
  model_status transitions.
- Exit: status/usage/model availability survive restarts; privacy tests
  green.

## P7 — Configuration management

- `relay config show/validate/reload/diff`; secret masking; TUI config
  panel; all config reachable without editing files (requirement 4).
- Tests: CLI + reload parity.
- Exit: full config surface via commands/TUI.

## P8 — Client integration guides + quality gate & CI

- **Client setup guides** (requirement 19): Cline, OpenCode, Continue —
  point the app at Relay's local endpoint, optional per-app key; plus a
  generic OpenAI-compatible section.
- `[tool.pytest]` config; async/CLI/TUI/install-smoke suites wired; optional
  ruff/mypy; GH Actions workflow (lint, full suite, packaging smoke, TUI
  boot).
- Exit: one command runs the whole suite; pipeline green; integration docs
  complete.

---

*Cross-cutting:* `python-observability` guidance on metrics/logs;
`security-best-practices` gate before P5; update `PROJECT_LOG.md`, README,
and docs at each phase boundary.
