# P6.5 — Platform usage & consolidation completion (analysis)

Status: **draft — analysis only, no code changed, no commit, `PROJECT_LOG.md` untouched**.
Depends on: P6.4 complete (commit `d344116`). Prior intent: `docs/platform-implementation-roadmap.md` (P6), `docs/platform-p6-plan.md` (§7.1 P6.4 scope), `docs/platform-p6-phase4-plan.md` (Tier-2 items F2, F3), `docs/roadmap-release-alignment-audit.md`.

Method: repository audited at commit `d344116` (working tree clean except untracked plan docs). Verified against source: `app/cli/__init__.py` (subcommand surface), `app/services/platform_store.py` (`SCHEMA_VERSION = 5`, no `request_log` table), `app/api/middleware.py` + `app/services/client_tracking.py` (in-memory apps surface), `app/setup/persistence.py` (live `availability.json` writes), `app/services/config_store.py` (still `.env`-backed). Roadmap docs re-read: `platform-implementation-roadmap.md`, `platform-recommended-order.md`, `platform-missing-components-report.md`, `platform-p6-plan.md`, `platform-p6-phase4-plan.md`.

---

## 1. Current implementation status

| Roadmap phase | Status | Evidence |
| --- | --- | --- |
| P0 Packaging & distribution | Complete | `pyproject.toml`, `relay` console script, packaging smoke tests, CI packaging job |
| P1 First-run & CLI | Partial | Wizard + bare-`relay`→TUI handoff work; subcommands `setup/tui/serve/keys/provider keys/migrate/events` exist; **`status/providers/models/routing/apps/config/logs/test` missing** |
| P2 TUI | Substantially complete | 7 tabs (Dashboard/Chat/Models/Providers/Configuration/Applications/Diagnostics); setup→TUI handoff (req 9) |
| P3 Async hot path | Complete | `async_chat_service`, async `/v1`/`/chat` endpoints |
| P4 Provider integrations | Partial | Anthropic/Gemini/Ollama/custom wired; **OpenRouter/Groq reserved-but-unwired** (P6 Decision H) |
| P5 API-key security | Complete | Store-backed scrypt keys, scopes, rotation, lifecycle, keyring-first upstream keys |
| P6 Platform DB + availability + usage | **Partial** | `platform.db` v1–v5 (`api_keys`, state tables, `model_status`, `events`), `relay migrate`, `relay events`, 3-state availability. **Missing: durable `request_log`, connected-applications projection, `relay apps`** (F2), **`availability.json` write retirement** (F3) |
| P7 Configuration management | Not started | No `relay config`; config swap (F1) still open; `config_store` is `.env`-backed |
| P8 Client guides + quality gate | Partial | CI workflow landed in P6.4; Cline/OpenCode/Continue guides missing (req 19) |

Baseline suite: **1916 passed / 18 skipped / 0 failed** at `d344116`.

---

## 2. Completed milestones and commits relevant to the decision

| Commit | Content | Roadmap phase |
| --- | --- | --- |
| `d2c5f8c` → `d60545b` + `bc76afb` | Registry-driven provider wiring, async clients, conformance suite (P4.1–P4.3) | P4 |
| `23ee1fe` → `11a68ac` | Secure key storage, keyring-first provider keys, store-backed auth + scopes, key lifecycle, migration (P5.1–P5.5) | P5 |
| `0f49419` | `platform.db` consolidation + `relay migrate` (P6.1) | P6 |
| `0474ce0` | Key lifecycle + security event logging (P6.2) | P6 |
| `aee18f2` | Technical-debt cleanup + RC validation-suite repair (P6.3) | P6 |
| `d344116` | v1.0 release hardening + CI + env/doc audit (P6.4) — **repurposed from the planned "usage/apps" scope to release hardening** | P6 |

The original P6.4 scope (`platform-p6-plan.md` §7.1: `app/api/apps.py`, `app/cli/apps.py`, `tests/test_apps.py`; retire `client_tracking`) was **deferred** during P6.4 planning (`platform-p6-phase4-plan.md` §2, items **F2** / **F3** / **F4** / **F1**). M3 milestone (`platform-recommended-order.md`: after P4+P5+P6) remains unmet — its marker requires `relay apps` to show a routed chat **after restart**.

---

## 3. Remaining roadmap gaps

1. **P6 remainder — usage & apps (F2):** durable request log; connected-applications projection (labeled keys × `request_log`); `relay apps` CLI; retire in-memory `client_tracking`. Blocks the M3 marker.
2. **P6 remainder — consolidation (F3):** `availability.json` is still written by wizard/UI scans; persist solely via `model_status`.
3. **P6 remainder — config swap (F1):** `providers` table + runtime `config_store` swap (storage seam for P7).
4. **P1 — CLI completeness (F4):** missing `status/providers/models/routing/apps/config/logs/test` subcommands.
5. **P4 — provider closure:** OpenRouter/Groq wiring, or formal drop of the reserved parsed keys (`OPENROUTER_API_KEY`, `GROQ_API_KEY`).
6. **P7 — configuration maturity:** `relay config show/validate/reload/diff`, secret masking, "all config reachable without editing files" (req 4/18).
7. **P8 — client integration guides:** Cline / OpenCode / Continue (req 19). CI already green.
8. **M5 tail:** package version (`0.1.0`) + tag alignment at M5-equivalent (deferred per the release-alignment audit).

---

## 4. Recommended next phase and rationale

**Recommended: P6.5 — Platform usage & consolidation completion** (the P6 remainder, items F2 + F3). P7, P8, and the P4 closure are explicitly **not** the next step.

Rationale:

1. **It is committed, incomplete scope.** P6 is treated as complete but its roadmap exit ("status/usage/model availability survive restarts") and the M3 marker are unmet. Finishing P6 restores the original plan's integrity before any new phase starts.
2. **It unblocks the first milestone gate.** M3 (platform core, after P4+P5+P6) is the roadmap's earliest cut-line; its only unmet requirement is the apps/request_log surface.
3. **It is the storage seam for P7.** P7's config commands and the config swap read the same `platform.db` seams this phase completes; the swap itself (F1) is deliberately excluded so the seam lands before P7, not with it.
4. **Lowest-risk available work.** Recommended-order places P6 in Track A ("additive surface", low risk). This phase touches no hot-path routing, no provider runtime, no API wire contracts, and no config semantics — unlike P7 (config precedence/reload behavior) or P4 closure (new provider integrations).
5. **Sequencing matches the roadmap.** Recommended-order: Track A is `P0 → P4 → P5 → P6 → P7`; the master plan's own final P6 sub-phase is "usage/apps" before P7.

P7, P8, and P4-closure are candidates for the phases after P6.5, in that order.

---

## 5. Detailed scope

**5.1 Durable `request_log` (F2 storage half)**
- Migration **v6** in `app/services/platform_store.py`: `request_log` table (id, ts, route, method, status, latency_ms, key_id opaque-nullable, client_bucket, ua, auth_scheme) + `idx_request_log_ts` index; `SCHEMA_VERSION = 6`.
- New `app/services/request_log.py` — `RequestLogStore` mirroring `KeyStore`/`StateStore` conventions (guarded connection delegating to `PlatformStore`, WAL/`0600`, corrupt-file backup/reopen): bounded in-memory buffer + background flush, metadata-only insert, config-driven retention prune, readers (route / bucket / key_id / time window).
- `app/api/middleware.py` — replace the `client_tracking.record(...)` call with a non-blocking buffer write (same metadata, same never-raise invariant).
- New settings in `app/core/config.py`: `REQUEST_LOG_RETENTION_DAYS`, `REQUEST_LOG_FLUSH_INTERVAL_SECONDS` (defaults 30 / 5), documented in `.env.example` + `docs/configuration.md`.

**5.2 Connected applications projection + CLI (F2 surface half)**
- New `app/services/apps_projection.py` — derived view: labeled `api_keys` × `request_log.key_id`, plus `none`/`other` buckets for unauthenticated traffic.
- New `app/cli/apps.py` + registration in `app/cli/__init__.py` — `relay apps`: label, opaque key id, route/bucket, requests, successes, failures, last-seen. No secret material.
- Closes the **M3 marker**: `relay keys create --label <client>` → routed chat → `relay apps` shows it after restart.

**5.3 Retire `client_tracking` (F2 cleanup)**
- Delete `app/services/client_tracking.py` + `tests/test_client_tracking.py`.
- `app/ui/data.py` — `client_activity()` / `auth_totals()` read from the `request_log` projection. The Applications screen (`app/ui/screens/applications.py`) reads only via the facade → unchanged.

**5.4 Retire `availability.json` writes (F3)**
- `app/setup/persistence.py` — scan results persist to `model_status`; remove live `write_snapshot` file writes; keep `iter_model_status` for the one-shot legacy import in `relay migrate` (decision B).
- `app/setup/wizard.py` — scan write path → `model_status` (no snapshot-file write).
- `app/ui/data.py` — models-availability merge reads `model_status` directly.
- `docs/platform-db-schema.md` — add v6 DDL; fix the stale "future P6.3/P6.4 tables" timeline (events v5 already landed).

---

## 6. Files expected to change

**New:**
- `app/services/request_log.py` — `RequestLogStore` (buffer + flush + retention + readers)
- `app/services/apps_projection.py` — connected-applications derived view
- `app/cli/apps.py` — `relay apps` command
- `tests/test_request_log.py`, `tests/test_apps.py`

**Modified:**
- `app/services/platform_store.py` — migration v6 (`request_log` + index), `SCHEMA_VERSION = 6`
- `app/api/middleware.py` — buffer write instead of `client_tracking.record`
- `app/ui/data.py` — applications reads from projection; models merge from `model_status`
- `app/setup/persistence.py` — scan writes → `model_status`
- `app/setup/wizard.py` — scan write path
- `app/core/config.py` — new retention/flush settings
- `.env.example`, `docs/configuration.md` — new settings (if added)
- `docs/platform-db-schema.md` — v6 DDL + timeline fix
- Tests updated: `tests/test_metrics.py`, `tests/test_ui_applications.py`, `tests/test_ui_data.py`, `tests/test_setup_wizard.py`, `tests/test_ui_providers.py`, `tests/test_packaging.py`

**Deleted:**
- `app/services/client_tracking.py`, `tests/test_client_tracking.py`

---

## 7. Files that must remain untouched

- `PROJECT_LOG.md` — never (standing instruction).
- **Hot path / routing:** `app/services/chat_service.py`, `async_chat_service.py`, `routing.py`, `scoring.py`, `decision_engine.py`, `candidate_builder.py`, `health_checker.py`, `health_refresher.py`, `health_store.py` — candidate ordering, failover, health-band invariants unchanged.
- **Provider runtime:** `app/providers/base.py`, `availability.py`, `*_client.py`, `registry.py`, `factory.py` — no provider behavior or OpenAI wire compatibility changes; **no new provider integrations** (OpenRouter/Groq deferred).
- **API wire behavior:** `app/api/chat.py`, `openai.py`, `feedback.py`, `decision.py`, `metrics.py`, `health.py`, `diagnostics.py`, `keys.py`, `admin.py`, `providers.py` — no request/response contract changes.
- **Auth contract:** `app/security/auth.py` behavior (path allowlist, bootstrap precedence, fail-closed) unchanged.
- **Settings bootstrap / config semantics:** `.env` still read at boot; `reload_settings` unchanged — the config swap (F1) is P7, not here.
- **Server/TUI screens:** `app/core/server.py`, `app/main.py`, `app/ui/screens/*` (Applications screen reads only via the facade).
- **Tests:** `tests/bench_nvidia_models.py`, `tests/test_lmstudio_real.py` — never touched.
- **Provider shims** `app/providers/nvidia.py|openai.py|lmstudio.py` — kept through v1.0.0 (approved P6.3 decision).

---

## 8. Security considerations

- **Privacy contract:** `request_log` stores metadata only — ts, route, method, status, latency, opaque `key_id`, client bucket, trimmed UA, auth-scheme label. **Never** prompts, bodies, responses, raw keys, provider keys, or correlation ids. Existing privacy tests must stay green; new metadata-only guarantees for `request_log`.
- **No new secrets:** no new credentials, no plaintext key material, no new secrets in env. `api_keys` keeps scrypt hashes only; the `apps` projection exposes opaque key ids + labels only.
- **Auth surface unchanged:** `auth.py` behavior, path allowlist, bootstrap precedence, and fail-closed semantics untouched; `relay apps` reads stored metadata, never the upstream provider keys.
- **Middleware robustness:** the new write path must preserve the middleware's never-raise invariant (store outage degrades the log, never the request).
- **File hygiene:** `request_log` lives in `platform.db`, which already enforces WAL + `0600` on the DB and sidecars (POSIX) via `PlatformStore`; retention bounds growth so a long-running gateway cannot fill disk with request metadata.

---

## 9. Migration/data considerations

- **Migration v6 is additive** (`request_log` + index), idempotent, guarded by `PRAGMA user_version`, no ALTER on existing tables. `api_keys`, state tables, `model_status`, and `events` are untouched by the v6 DDL.
- **Downgrade caveat:** an app at `SCHEMA_VERSION = 5` refuses to open a v6 file (`migrate()` "newer than supported"). Rollback therefore restores `platform.db` from backup (see §11); the only loss is recent `request_log` rows, which are non-critical metadata.
- **Hot-path data flow:** no synchronous SQLite insert on the event loop. Capture goes to a bounded in-memory buffer flushed by a background writer (the `StateFlusher` pattern), so P3's "no blocking calls on the loop" holds.
- **Retention:** prune by age on flush/access (config-driven `REQUEST_LOG_RETENTION_DAYS`), matching existing store conventions; optional row cap.
- **F3 data impact:** `model_status` is already seeded from availability snapshots at migrate time; this phase routes live scan writes to it. Pre-existing legacy `availability.json` files remain importable by `relay migrate` (decision B); nothing on disk is deleted during P6.5.
- **Existing v6 precedent:** migration v5 (`events`) already landed without a schema-doc update; §5.4 fixes `docs/platform-db-schema.md` so the doc matches the implemented DDL (P6 plan gate criterion 4).

---

## 10. Testing strategy

- **New tests:**
  - `tests/test_request_log.py` — insert + read-back; metadata-only guarantee (no key/prompt/correlation material); retention prune; durability across store reopen; migration v6 up from a v5 fixture (`PRAGMA user_version = 6`); corrupt-file backup/reopen.
  - `tests/test_apps.py` — projection from labeled keys × `request_log`; unauth'd `none`/`other` bucketing; `relay apps` CLI output (no secret material); **M3 marker scenario** (`relay keys create` → routed chat → `relay apps` after reopen).
  - `tests/test_middleware.py` (extend) — every routed `/v1`/`/chat` request lands a metadata row via the buffer; middleware never raises on store failure.
- **Updated tests:** `tests/test_metrics.py`, `tests/test_ui_applications.py` (seed via `request_log` instead of `client_tracking`), `tests/test_ui_data.py`, `tests/test_setup_wizard.py`, `tests/test_ui_providers.py`, `tests/test_packaging.py` (F3 snapshot-write behavior).
- **Unchanged gates to keep green:** conformance suite, provider parity, failover, privacy, security-hardening.
- **Gate:** `pytest tests -q` → **0 failures**, reviewed skips (baseline 1916 / 18); CI green on ubuntu + windows; `relay --version` and all CLI entry points keep working.

---

## 11. Rollback strategy

| Layer | Rollback |
| --- | --- |
| Code | Checkout the previous artifact per `docs/rollback-procedure.md`. P6.5 is additive; no in-place v6→v5 downgrade migration is provided |
| Data | Restore `platform.db` from `state_dir/backups/<ts>/` (created by `relay migrate`); loses only recent `request_log` rows — `api_keys`, state, `model_status`, `events` unaffected by v6 DDL |
| Config | None — no config semantics change in P6.5 (the config swap is P7) |
| Behavior | `client_tracking` removal is safe: middleware never raises; if the `request_log` store is unavailable the facade falls back to an empty Applications view |
| Availability | Legacy `availability.json` remains importable by `relay migrate` (decision B); no file deletion during P6.5 |

---

## 12. Acceptance criteria

1. Migration **v6** applies idempotently to an existing v5 `platform.db`; `PRAGMA user_version = 6`; corrupt-file backup-and-reopen and the D6 legacy guard still pass.
2. Every routed `/v1` and `/chat` request writes a **metadata-only** row to `request_log` via a non-blocking path, and rows **survive a restart**.
3. `relay apps` lists connected applications (labeled-key traffic + unauth'd buckets) with request/success/failure counts and last-seen; the **M3 marker passes** (`relay keys create` → routed chat → `relay apps` shows it after restart).
4. `client_tracking` fully retired: no imports, no middleware writes; the TUI Applications tab renders from the `request_log` projection with **no screen changes**.
5. Setup scans (wizard and TUI Providers scan) persist to `model_status`; `availability.json` is no longer written by live flows; `relay migrate` still imports a pre-existing legacy file.
6. Privacy tests green; no raw keys, prompts, responses, or correlation ids in `request_log`.
7. Full suite **0 failures**, reviewed skips; CI green on ubuntu + windows; conformance suite green.
8. `PROJECT_LOG.md` untouched; only the files in §6 changed; no commit until explicit approval.

---

## 13. Decisions required from the reviewer

| # | Decision | Recommended |
| --- | --- | --- |
| A | `request_log` capture design | **Bounded in-memory buffer + background flush** (StateFlusher pattern) to keep the loop non-blocking |
| B | F3 legacy import path | **Stop live writes, keep one-shot import of an already-existing `availability.json` in `relay migrate`** (safer for upgraded installs; deviates from F3's literal "remove the legacy import path") |
| C | `client_tracking` module | **Delete** (facade + middleware are the only consumers); no shim needed |
| D | `request_log` retention default | **30 days** (`REQUEST_LOG_RETENTION_DAYS`) + optional row cap |
| E | `relay apps` output | Label + opaque key id + route/bucket + counts + last-seen; no raw keys, no UA dump |
| F | F4 DX commands in this phase | **No** — keep P6.5 focused; `relay status`/`relay providers list`/`relay models` go to the P1-completion milestone |

**Recommended follow-on order (after P6.5, for later planning):** P7 configuration maturity (F1 config swap + `relay config` commands) → P1 CLI completion (F4) → P4 closure (OpenRouter/Groq decision) → P8 client guides → M5 version/tag alignment.
