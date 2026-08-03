# Relay — Missing Components Report (Phase 9)

Date: 2026-08-03 (rev 2 — updated after requirements validation pass)
Prepared for: Phase 9 — platform transformation (analysis only, no code changed)

Companion docs: `docs/platform-architecture-report.md` (what exists),
`docs/platform-implementation-roadmap.md`, `docs/platform-recommended-order.md`.

---

## 0. Target design (inferred from mission + requirements — approved)

Authoritative requirements (user-supplied, Rev 2): **Relay is a
zero-friction AI gateway platform.**

### 0.1 User experience
1. One-command install from GitHub.
2. After installation, the terminal tells the user to type `relay`.
3. First launch auto-detects missing configuration and starts setup.
4. The user **never manually edits config files**.

### 0.2 First-run setup
5. Interactive provider selection, providers chosen one by one.
6. API key entry with validation; invalid keys clearly explain the error
   and allow retry or skip.
7. After a successful provider connection:
   - fetch the provider model catalog,
   - show the total discovered models,
   - test availability **progressively**,
   - use **one global progress bar**,
   - show the currently-tested model name dynamically under the bar,
   - **no separate progress bar per model**.
8. Final results: Total / Available / Unavailable; optional detailed status
   list (`✓` available, `⚠` overloaded, `✗` unavailable). Models are
   **not** dumped by default.

### 0.3 After setup
9. Relay immediately continues into the main terminal interface — no
   second `relay` command.

### 0.4 Main Relay interface
10. Chat with a random available model.
11. Select a specific model and test it.
12. Manage providers.
13. Add/remove API keys.
14. Change model priority.
15. Manage routing rules.
16. View connected applications.
17. Show system status.
18. Manage Relay configuration.

### 0.5 Application integration
19. OpenAI-compatible endpoint; Cline, OpenCode, Continue, and other
    OpenAI-compatible clients connect with minimal setup — point the app at
    Relay's local endpoint.
20. API-key handling abstracted by Relay (Relay owns upstream provider
    keys; the client only needs Relay).

### 0.6 Architecture expectations
21. Provider abstraction layer.
22. Secure API key storage.
23. Persistent database/config storage.
24. Model availability tracking.
25. Provider health monitoring.
26. Async operations for slow provider scans.
27. CLI/TUI interface.
28. Packaging/distribution flow.

---

## 1. Requirements validation matrix

Legend: ✅ satisfied · ⚠️ partial · ❌ missing · N/A not applicable

| # | Requirement | Current | Gap | Roadmap |
| --- | --- | --- | --- | --- |
| 1 | One-command GitHub install | ❌ no packaging at all | `pip install git+<repo>` or installer; post-install guidance | P0 |
| 2 | Post-install "type `relay`" | ❌ | install hook / README messaging; bare `relay` must exist | P0/P1 |
| 3 | First launch auto-setup | ⚠️ bare `relay` prints help; `setup` is separate | no-args config-detect → auto-start wizard | P1 |
| 4 | Never edit config files | ⚠️ wizard writes `.env`, but no edit-free guarantee | all config via wizard/CLI/TUI; config files are wizard-managed only | P1/P7 |
| 5 | One-by-one provider selection | ✅ `_setup_provider` iterates providers | — | P1 |
| 6 | Key validation + retry/skip, clear errors | ⚠️ fetch errors printed raw, no retry loop | classified errors (auth/connect), retry or skip | P1 |
| 7 | Catalog fetch + totals + one progress bar + dynamic model name | ⚠️ totals only; probing limited to one model | progressive async availability testing, single global bar, live model line | P1 (+P4 async clients) |
| 8 | Totals + optional ✓/⚠/✗ detail, no dump | ⚠️ 3-state map not modeled; inline list dumped | availability summary + optional detailed list; default hides list | P1/P6 |
| 9 | Setup → main interface seamlessly | ❌ setup ends and requires `relay serve`/restart | setup hands off into TUI | P1/P2 |
| 10 | Chat with random available model | ❌ no chat surface | TUI Chat screen + random-available picker | P2 |
| 11 | Select model and test it | ⚠️ probe exists in `_test_provider`, not a UI | TUI/CLI model select + probe | P1/P2 |
| 12 | Manage providers | ⚠️ enable/disable env only | `relay providers` + TUI panel | P1/P2/P4 |
| 13 | Add/remove API keys | ⚠️ single global `RELAY_API_KEY` | per-app keys CRUD (hashed, scoped) | P5 |
| 14 | Change model priority | ⚠️ priority envs set at setup only | `relay models`/TUI re-prioritize | P1/P2 |
| 15 | Manage routing rules | ⚠️ `TASK_*` envs at setup only | routing-rules surface (task ↔ models, rules) | P1/P2/P7 |
| 16 | View connected applications | ❌ no notion of apps | apps = labeled keys × request_log correlation | P5/P6/P2 |
| 17 | System status | ⚠️ no status command | `relay status` + TUI panel | P1/P2 |
| 18 | Manage configuration | ⚠️ `.env` editing | `relay config` show/validate/reload/diff + TUI | P7 |
| 19 | OpenAI-compatible clients, minimal setup | ✅ `/v1` endpoint exists | per-client guides (Cline/OpenCode/Continue) | P8 |
| 20 | API-key handling abstracted | ⚠️ `/v1` needs a key; upstream keys in `.env` plaintext | per-app keys; upstream keys out of plaintext | P5 |
| 21 | Provider abstraction layer | ✅ provider base + clients | de-string-named registry | P4 |
| 22 | Secure API key storage | ⚠️ app key plaintext env; upstream keys plaintext `.env` | hash relay keys; keyring/encrypted store for upstream | P5 |
| 23 | Persistent DB/config storage | ⚠️ state-only SQLite | platform DB (migrations) | P6 |
| 24 | Model availability tracking | ⚠️ in-memory health/learned state | durable 3-state model status (✓/⚠/✗) | P6 |
| 25 | Provider health monitoring | ✅ health checker (sync thread) | keep; feed model_status | P3/P6 |
| 26 | Async operations for slow scans | ❌ sync clients | async-first provider clients for catalog/probe scans | P4 (async-first) |
| 27 | CLI/TUI interface | ⚠️ argparse CLI only | Typer CLI + Textual TUI | P1/P2 |
| 28 | Packaging/distribution | ❌ | `pyproject`, console script, one-command install | P0 |

**Net result:** 9 requirements fully missing, 12 partial, 7 satisfied.
The gaps drive the roadmap updates in rev 2.

---

## 2. Gap matrix — component level

| Target capability | Current | Missing / to build | Refactor needed | New dependencies |
| --- | --- | --- | --- | --- |
| Packaging + console script + one-command GitHub install | ❌ | `pyproject.toml`, `[project.scripts] relay=app.cli:main`, version module, wheel/sdist, `pip install git+<repo>` smoke, post-install message | none | `hatchling` or `setuptools` (build) |
| First-launch auto-detect | ❌ | bare `relay`: config-completeness check → auto-launch wizard | `app/cli.py` split into `app/cli/` | `typer`, `rich` |
| Setup wizard (product spec) | ⚠️ | one-by-one providers; key validation w/ classified errors + retry/skip; async catalog fetch; single global progress bar + dynamic model line; totals; optional ✓/⚠/✗ detail; no dump | rewrite `_cmd_setup` | `rich` progress |
| Setup → TUI handoff | ❌ | wizard completes → launch main interface | — | — |
| Main interface (TUI) | ❌ | Chat (random available), Model test, Providers, Keys, Model priority, Routing rules, Connected apps, Status, Config | — | `textual` |
| Async-first provider clients | ⚠️ sync | `achat`, `alist_models`, `aprobe_model`, `achat_stream` | client base + factories | none (httpx is async) |
| Async tests | ⚠️ sync | `pytest-asyncio` fixtures; async tests | — | `pytest-asyncio` |
| More providers | ⚠️ 3 wired; 5 keys unused | Anthropic (Messages), OpenRouter, Groq, Ollama, custom OpenAI-compatible | de-string-name provider system | `anthropic` SDK or hand-rolled client |
| App API-key security | ⚠️ single global key | keys table (scrypt/pbkdf2), scopes/expiry/rotation/revoke, CLI `keys`, admin API, constant-time compare, per-key correlation | `auth.py` | none (stdlib) |
| Upstream key storage | ⚠️ `.env` plaintext | keyring/encrypted store (OS keyring or encrypted file w/ master key); `.env` kept as bootstrap/compat | settings wiring | `keyring` (optional) |
| Platform SQLite DB | ⚠️ state-only v3 | migrations; `api_keys`, `request_log` (metadata-only), `model_status`, `events`; retention | `platform_store.py` alongside `state_store.py` | none |
| Model availability tracking | ⚠️ in-memory | durable 3-state model status (✓ available / ⚠ overloaded / ✗ unavailable); fed by setup probes + health + learned feedback | health band → UI status mapping | none |
| Connected applications | ❌ | apps = labeled keys × request_log correlation; CLI/TUI view | — | none |
| Routing rules management | ⚠️ `TASK_*` envs | `relay routing` surface + TUI panel: task↔model rules, enable/disable, priority | expose routing config model | none |
| Config management | ⚠️ `.env` + reload | `relay config show/validate/reload/diff`, masking, TUI panel | thin commands over settings/reload | none |
| Client integration guides | ❌ | Cline / OpenCode / Continue setup docs (point to local endpoint) | — | — |
| CI/quality gate | ⚠️ pytest, no config | `[tool.pytest]`, async/CLI/TUI/install-smoke suites, GH Actions | — | `pytest-asyncio`; optional `ruff`, `mypy`, `respx` |

---

## 3. Component-level detail (rev 2 additions)

### 3.1 First-launch flow (`relay` no-args)
- Config-completeness check over `Settings` (which providers have keys,
  whether routing/platform DB initialized). If incomplete → print guidance
  and start the wizard. If complete → launch the main interface (TUI once
  P2 lands; a minimal status/menu before then).

### 3.2 Setup wizard UX spec
- Providers presented one at a time; each step skippable.
- Key entry with **live validation**: attempt a lightweight probe /
  catalog fetch; classify failures (`auth_error` vs `connectivity` vs
  `quota`) and print a clear message; prompt **retry** or **skip**.
- On success: fetch catalog → show total discovered.
- Progressive availability test: **one global progress bar** advancing over
  the discovered models; the **current model name renders beneath the bar**
  and updates per model (a single `rich` progress task with a live caption,
  or equivalent; never one bar per model).
- Final summary: **Total / Available / Unavailable**. Then offer to expand
  the detailed per-model list (`✓` available, `⚠` overloaded, `✗`
  unavailable). **Do not print all models by default.**
- Availability states map from probe/health signals: healthy → `✓`; 429 /
  rate-limit / timeout-but-reachable → `⚠` overloaded; auth/connect/other →
  `✗` unavailable. Results persist into `model_status` (P6).

### 3.3 Main interface (TUI) — required panels
Chat (random available model picker) · Model test · Providers · Keys ·
Model priority · Routing rules · Connected applications · System status ·
Configuration. Read-only panels first, interactive controls as data
surfaces (P5/P6) land.

### 3.4 Connected applications
Identity comes from the **app key the user creates per client**
(`relay keys create --label "opencode"`); "connected apps" = label ×
`request_log.api_key_id` activity (metadata only). No client sniffing
required.

### 3.5 Upstream key storage decision (P5)
Move provider keys out of plaintext `.env` into the OS keyring (optional
`keyring` dependency) or an encrypted store with a local master key; keep
`.env` as a supported bootstrap/compat path. Document the tradeoff.

---

## 4. Dependencies required (summary)

| Dependency | Kind | Used for | Effort |
| --- | --- | --- | --- |
| `typer` | runtime | CLI framework (subcommands, prompts) | low |
| `rich` | runtime | CLI rendering, **single global progress bar** | low |
| `textual` | runtime | Terminal UI main interface | medium |
| `anthropic` SDK (or hand-rolled client) | runtime | Anthropic Messages provider | low-medium |
| `keyring` (optional) | runtime | upstream provider-key storage | low |
| `pytest-asyncio` | dev | async test fixtures | low |
| `hatchling`/`setuptools` | build | packaging | low |
| `ruff`/`mypy` (optional) | dev | lint/type gate | low |
| `respx` (optional) | dev | async HTTP mocking | low |

No new dep for: async `httpx`, `secrets`/`hashlib`, `sqlite3`, `pydantic`,
FastAPI.

## 5. Skills / tools

- Enabled: `fastapi`, `security-best-practices` (built-in);
  `python-testing-patterns`, `async-python-patterns`, `python-packaging`
  (installed in `.agents/skills`).
- Rich/Textual, generic CLI, provider integrations, SQLite design, config
  management: no reputable installable skill; covered directly.
- `python-observability` applies to metrics/logs/request-log work.

## 6. Risks and constraints

- Async migration remains the highest-risk item (see roadmap P3).
- **Privacy contract is non-negotiable**: `request_log`/`model_status`
  store metadata only — never prompts, responses, or key material.
- Single-process/single-writer SQLite inherited by the platform DB.
- Backward compatibility: `/v1` wire behavior, `RELAY_API_KEY` bootstrap,
  `.env` compat, `python -m app.cli` / `python -m uvicorn app.main:app`
  entry points.
- No code changed in this phase.

---

*Deliverable 2 of the Phase 9 analysis (rev 2).*
