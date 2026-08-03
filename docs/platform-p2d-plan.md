# P2d Plan — Configuration, Applications, Diagnostics

Status: **plan only — no code yet**. Awaiting approval before implementation.

Scope per `docs/platform-p2-implementation-plan.md` §P2d and the DoD §13 item 3
(Configuration / Applications / Diagnostics panels). This document is the
implementation plan: exact scope, per-feature file lists, reused seams, data
flow, security considerations, and required tests.

---

## 1. Scope

### In scope (tab 5/6/7 of the TUI)

- **Configuration** — routing `TASK_*`, failover/retry, and server settings
  form; writes via `config_store` + in-process `reload_config(relay)`.
  `RELAY_HOST`/`RELAY_PORT` read-only with restart warning; `DEFAULT_PROVIDER`
  informational only.
- **Applications** — endpoint/auth status + client-activity table
  (Cline/OpenCode/Continue/Other heuristics on trimmed user-agent).
- **Diagnostics** — tail of ops events and file log; provider health deep
  view; per-provider test connection; redacted snapshot export to file.

### Non-goals (explicitly deferred)

- P3 async metrics, P4 async scanning, P5 keyring, P6 `relay.db`/`model_status`
  tables/durable `request_log`, P7 CLI config commands, P9 personality /
  cursor-following.
- No new HTTP endpoints in P2d (the surfaces are TUI panels that read existing
  seams; `POST /admin/reload` and `GET /diagnostics` already exist).
- No durable storage, DB, or migrations. All P2d state is bounded, in-memory,
  metadata-only (consistent with interim decisions D3/D4).
- No multi-user / per-app keys (P5).

---

## 2. Shared seams and principles

Every feature reuses the following without duplicating their logic:

| Seam | Role |
|---|---|
| `app/services/config_store.py` | The only writer of `.env` (`set_env`, `set_provider_config`, `get_env`). P2d Configuration writes through it, never dotenv directly. |
| `app/services/reload.py::reload_config` | Applies an explicit allowlist of fields to the in-process singleton + providers; returns a report. Secrets reported by field name only; validation/apply errors redacted by `_redact`. |
| `app/services/ops_store.py::ops_store` | Bounded in-memory rolling window of request metadata (`OpsEvent`: ts/kind/method/route/status/latency/endpoint/provider/model/stream/success/fallback/attempts). Never stores payloads/keys. `stats()` aggregates; `events()` tails. |
| `app/services/diagnostics.py::DiagnosticsService.build_snapshot` | Read-only, no network, no mutation; keys `generated_at, providers, learned_health, telemetry, operations, scoring, adaptive, quality, persistence`. Keys are booleans only (`has_api_key`). |
| `app/services/metrics.py::relay_metrics` | Auth counters (`auth_success{method}`, `auth_failures{reason}`, `auth_enabled`) — the "auth status" source. |
| `app/security/auth.py::require_api_key` | Global auth dependency; reads `settings.relay_api_key` per request. Scheme labels currently computed inline at the end (bearer/header). |
| `app/api/middleware.py::MetricsMiddleware` | Pure-ASGI; already records every HTTP request's method/route/status/latency into ops_store. Natural single capture point for UA/auth-scheme metadata. |
| `app/ui/data.py::ServiceFacade` | Textual-free view-model layer screens must use; the only allowed import path from screens besides theme/setup_adapter. |
| `app/ui/setup_adapter.py::PromptScreen` | Reusable masked-input modal for any secret entry (Configuration `RELAY_API_KEY`). |

### Invariants

1. **Metadata only.** No prompts, responses, bodies, API keys, authorization
   tokens, proxy credentials ever captured, rendered, or exported by these
   screens. Bools (`has_api_key`, `auth_enabled`) and field names only.
2. **Single writer.** Only `config_store` writes `.env`; `reload_config` is the
   only in-process mutator. Screens never touch dotenv or settings directly.
3. **Read-only panels unless explicitly an action.** Export and test-connection
   are explicit user actions; snapshot building never probes.
4. **Bounded.** Every new in-memory store has a cap consistent with
   `ops_window_seconds`/`ops_max_events` conventions (see D4).
5. **ServiceFacade stays Textual-free.** New projections are plain dataclasses
   + pure functions so they are headlessly testable.

---

## 3. Feature A — Configuration screen (tab 5)

### 3.1 Surface and field classification

Form rendered from `ServiceFacade.config_form_values()`, grouped:

**Live-reloadable** (all members of `reload_config` allowlist; applied
in-process on save, no restart). Group = routing, failover/retry, auth:

- Routing: `TASK_ROUTING_ENABLED`, `CROSS_PROVIDER_MODEL_SELECTION`,
  `TASK_CODING`, `TASK_VISION`, `TASK_REASONING`, `TASK_GENERAL`,
  `TASK_CREATIVE`, `TASK_TRANSLATION` (CSV of model refs), plus
  `HEALTH_AWARE_ROUTING` (same allowlist; keeps routing coherent).
- Failover/retry: `REQUEST_TIMEOUT`, `MAX_RETRIES`, `RETRY_HONOR_RETRY_AFTER`,
  `RETRY_AFTER_MAX_SECONDS`, `RETRY_BACKOFF_BASE_SECONDS`,
  `RETRY_BACKOFF_MAX_SECONDS`, `REQUEST_TIMEOUT_BUDGET_SECONDS`.
- Auth: `RELAY_API_KEY` (secret, always masked).

**Restart-required (read-only + note)** — reload never touches these per the
`reload.py` docstring (server bind, persistence enabling/path/flush timing,
logging, LM Studio base URL):

- `RELAY_HOST`, `RELAY_PORT` — read-only, "requires restart" warning shown.
- Informational read-only: `DEFAULT_PROVIDER` (no silent behavior change),
  `PERSISTENCE_ENABLED`, `PERSISTENCE_PATH`, `PERSISTENCE_FLUSH_INTERVAL_SECONDS`,
  `LOG_LEVEL`, `LOG_FILE`, `LMSTUDIO_BASE_URL`, `RELAY_ENV_FILE`, `RELAY_STATE_DIR`.

**Out of form scope (still editable via `.env` + `/admin/reload`):** the other
reloadable families (`HEALTH_*_TTL_*`, `HEALTH_FEEDBACK_*`,
`SCORING_*`, `ADAPTIVE_*`, `QUALITY_*`, `DECISION_*`, `TASK_CLASSIFICATION_*`,
`OPS_*`, proxy vars). Keeps the form focused per §P2d wording.

### 3.2 TASK_* semantics

- Each `TASK_*` is a comma-separated list of model refs: bare model id, or
  `ProviderName:model`. Empty value clears the preference.
- The routing engine re-reads these via `relay.routing.refresh()` on reload, so
  edits take effect in-process immediately (no restart, no provider rebuild).
- UI: one text input per category with a hint on the ref syntax; validated
  client-side for shape only (real validity is decided by `reload_config` +
  model availability, which may legitimately drop refs).

### 3.3 Data flow: save → validate → apply → revert

`ServiceFacade.save_config(changes)` (where `changes` is `{env_key: value}`):

1. **Snapshot** the current in-process values of every changed field.
2. **Write** each changed value via `config_store.set_env(key, value)` (single
   writer; `None`-omitted keys unchanged).
3. **Dry-run validate** — `reload_config(relay, dry_run=True,
   dotenv_path=str(env_file))`. This re-reads the written `.env` (the same
   overlay pattern `POST /admin/reload` uses), builds a fresh validated
   `Settings()`, and reports applied/unchanged without mutating.
4. **Apply** — if the dry run reports `reloaded: True` and no failures, call
   `reload_config(relay, dotenv_path=str(env_file))` to apply.
5. **Revert on failure** — if either step reports `reloaded: False` or has
   failures, restore the snapshot values with `config_store.set_env` so the
   file and the in-process state stay consistent (validation failure leaves
   `.env` as it was; apply failures are already rolled back in-process by
   `reload_config` and the file revert matches).

**Report → status line.** Show `reloaded`, `applied` (field names; secrets by
name only), `unchanged`, `failures`, and `error_kind`/`error` (already
redacted by `_redact`). Never echo values.

### 3.4 Security

- `RELAY_API_KEY` input is password-masked (reuse `PromptScreen` secret
  handling). When set, the field shows a masked placeholder, never the value.
- Reload report shows `relay_api_key` as a name only (`reload.py` already
  guarantees this via `_SECRET_FIELDS` + `_redact`).
- Validation errors are redacted by `_redact` (env-var name only).
- `require_api_key` reads settings per request, so a saved `RELAY_API_KEY`
  enables/rotates/disables auth without restart — call this out in the screen
  note.

### 3.5 Tests (`tests/test_ui_configuration.py`, extend `test_ui_data.py`)

- `config_form_values` returns current settings values + correct
  live/restart/informational classification.
- `save_config` happy path: patches `config_store.env_file` to `tmp_path`
  (existing `test_config_store` pattern) and monkeypatches
  `app.ui.data.reload_config`; asserts `.env` contents, report surfaced,
  applied list has no secret values.
- Validation-failure path: injected dry-run failure → `.env` reverted to the
  snapshot values; status shows redacted error.
- Apply-failure path: `error_kind="apply"` → file reverted.
- Masking: rendered form/status never contains the raw `RELAY_API_KEY`.
- Read-only fields cannot be submitted (facade rejects changes to
  restart-required/informational keys).
- TASK_* CSV: empty string clears; comma-separated list written quoted.

---

## 4. Feature B — Applications screen (tab 6)

### 4.1 Client detection (pure, unit-testable)

New `app/services/client_detection.py`:

```
classify_client(user_agent: str | None) -> str
```

- Normalize: `(user_agent or "").strip().lower()`, cap at 200 chars.
- Substring match in priority order → `"cline" | "opencode" | "continue"`.
- Otherwise (empty, unknown SDKs, relay's own conformance client) → `"other"`.

Bucket list is fixed (`cline/opencode/continue/other`) so the table and tests
stay deterministic. Heuristic-only by design; documented in the module docstring.

### 4.2 Client activity tracker (bounded, metadata-only)

New `app/services/client_tracking.py` — `ClientTracker`, thread-safe, modeled
on `ops_store`/`TelemetryStore` conventions:

- `record(bucket, route, status, auth_scheme)` at request completion.
- State keyed by `(bucket, route)`: `requests`, `successes` (status < 400),
  `failures`, `auth_schemes` (last-seen set, bounded), `last_seen`.
- Pruned by age (`ops_window_seconds`) and capped at a fixed max bucket count;
  `clear()` for tests/reset.
- `activity()` → snapshot list sorted by last-seen desc, plus totals.

**Never stores**: the UA beyond the trimmed bucket-matching input (only the
bucket persists), the `Authorization` header value, `X-Relay-API-Key`, request
bodies, or messages. Only the *label* of the auth scheme persists.

### 4.3 Auth-scheme labeling (shared helper)

Refactor the scheme computation currently inline at the end of
`auth.py::require_api_key` into a pure helper, e.g.
`auth_scheme(request) -> "public" | "none" | "bearer" | "header"`, used by both
`require_api_key` (unchanged behavior) and the Applications capture path:

- `public` — path in `PUBLIC_PATHS` and auth enabled.
- `none` — auth disabled, or no credential presented.
- `bearer` — `Authorization: Bearer …` present.
- `header` — `X-Relay-API-Key` present.

### 4.4 Capture point

Extend `MetricsMiddleware` (already the single per-request HTTP capture point;
already computes method/route/status/latency) to additionally:

1. Read `scope["headers"]` for `user-agent` + auth headers.
2. Bucket via `classify_client`.
3. Label scheme via `auth_scheme`-equivalent computed from headers + path +
   `settings.relay_api_key` (pure function; no key comparison needed).
4. `client_tracking.record(bucket, route_path, status, scheme)` in the same
   guarded `try` block that writes `ops_store` (never raises).

### 4.5 Screen surface

- **Auth status**: `auth_enabled` (gauge), auth successes by method + failures
  by reason from `relay_metrics` (same source `diagnostics._operations` uses).
- **Endpoint status**: `ops_store.stats()["endpoints"]` (+ totals).
- **Client activity table**: bucket | requests | successes | failures | auth
  scheme | last seen. Empty state when nothing recorded.
- Note line: metadata-only, in-memory, replaced by durable `apps`/`request_log`
  in P6.

### 4.6 Security

- `client_tracking` never persists header values; only bucket + route + status
  + scheme label. Add an explicit test that records a request with a real
  `Authorization: Bearer sk-secret` and asserts the tracker state contains no
  substring of the token and no raw header.
- Trimming UA to 200 chars keeps arbitrary UA payloads bounded.

### 4.7 Tests

- `tests/test_client_detection.py`: bucket table incl. case-insensitivity,
  trimmed UA, empty UA → `other`, Cline/OpenCode/Continue variants, unknown
  SDKs, 201-char UA truncation before matching.
- `tests/test_client_tracking.py`: bounded cap, age pruning, snapshot shape,
  `clear`, thread-safety, and the no-raw-auth test above.
- `tests/test_ui_applications.py` + `test_ui_data.py`: facade `client_activity`
  projection, `auth_status` totals, `endpoint_status`, screen render with fakes.
- `test_auth.py`: extend to assert `auth_scheme` labels for public/none/
  bearer/header (refactor safety net).

---

## 5. Feature C — Diagnostics screen (tab 7)

### 5.1 Surface

- Summary tiles from `DiagnosticsService.build_snapshot` (`operations` totals,
  auth failures, provider count, persistence status) — read-only, no probe.
- Ops tail table (recent `ops_store.events()`, metadata only).
- File-log tail (only when `LOG_FILE` is configured; otherwise a "log file not
  configured" note).
- Provider health deep view (per-model status, latency, learned marks).
- Actions: per-provider **test connection** (explicit, network I/O in a worker
  thread), **export snapshot** (explicit file write), refresh.

### 5.2 Ops tail + file log tail

- `ServiceFacade.ops_tail(limit=N)` → last N `ops_store.events()` (kind/method/
  route/status/latency/endpoint/provider/model/stream/success/fallback), newest
  first. Already metadata-only.
- `ServiceFacade.log_tail(limit=N)` → read last N lines of
  `settings.log_file`; parse each as the `JsonFormatter` shape
  (`ts, level, logger, event[, data]`). Lines that fail to parse or exceed a
  length cap are skipped (never dumped raw beyond a bounded preview). When the
  parsed event contains `data` keys that look secret (`api_key`, `token`,
  `password`, `authorization`, `key`), the value is replaced with
  `<redacted>`.

### 5.3 Provider health deep view

Reuse the Models-screen join: health report (`relay.health_store.get(name)` →
status/connectivity/healthy/degraded/unavailable/unsupported models) + the
availability snapshot (`app.setup.persistence`) + learned marks
(`health_store.export_learned_state()`). Read-only composition, no probes —
same data `build_snapshot`'s `providers`/`learned_health` already expose.

### 5.4 Per-provider test connection

Explicit user action only. Reuse the P1 single-probe path (`ScanEngine`
single probe → `classify_probe` → `✓/⚠/✗`, per `app/providers/availability.py`).
Runs in a worker thread (`asyncio.to_thread`) so the TUI stays responsive;
result shown in the status line. Never mutates state; does perform network I/O
(bounded by provider timeout/retry settings).

### 5.5 Export (exact contents + redaction)

`ServiceFacade.export_diagnostics(path)`:

- Contents = exactly `DiagnosticsService.build_snapshot(relay)` (keys above),
  pretty-printed JSON. Everything in it is already redacted by contract:
  provider/model names, aggregates, parameters, booleans (`has_api_key`) only.
- **Excluded by default**: raw ops events beyond the `operations` aggregates,
  and the file-log tail (may carry provider error text) — keeps export
  bounded and predictable.
- Write atomically: temp file in the same directory + `os.replace`, so a
  failure never leaves a partial export.
- Returns `{path, generated_at, bytes, ok, error}` for the status line.

### 5.6 Security

- Export test asserts the JSON string contains no `sk-` prefix, no `Bearer `,
  no `Authorization`, no key values, and that `has_api_key` is a boolean.
- `build_snapshot` never probes and never writes (existing contract); export
  adds nothing raw.
- Log tail redaction helper is the only place raw log text is touched, and it
  redacts secret-shaped `data` keys before display.

### 5.7 Tests

- `tests/test_ui_diagnostics.py`: screen renders snapshot tiles/ops tail/log
  tail with fakes; test-connection button triggers the probe path (fake client);
  export writes a file and returns the report.
- Extend `test_diagnostics.py` / new `tests/test_diagnostics_export.py`:
  export JSON contents + redaction assertions; atomic write leaves no partial
  file on injected failure; log-tail parser redacts secret-shaped keys and
  skips unparseable lines.
- `test_ui_data.py`: `ops_tail`, `log_tail`, `provider_health_deep`,
  `export_diagnostics` projections.

---

## 6. P5/P6 interaction review

- **P5 (keyring/encrypted store)** replaces only the `api_key` path inside
  `config_store`. P2d Configuration writes `RELAY_API_KEY` through
  `config_store.set_env` (single-writer seam), so when P5 lands the facade's
  save path redirects to the keyring/encrypted store with no screen changes.
  P2d already renders the key masked and shows only `has_api_key`-style
  booleans, matching P5's "no key material in plaintext/UI" contract.
- **P6 (`relay.db`)** replaces the `config_store` implementation and adds
  durable `request_log`, `model_status`, `events`, and `apps` (labeled keys ×
  `request_log`). P2d's `client_tracking` is the interim in-memory
  "connected applications" surface; it is a new isolated module (write-only
  tracker + read projection, not woven into routing/health), so P6 swaps it
  out for `apps`/`request_log` locally. The Diagnostics ops tail stays
  in-memory per the `ops_store` contract; P6 gives the durable tail later.
- P2d adds **no** DB, migrations, or durable state; the D3 (plaintext `.env`
  until P5) and D4 (bounded in-memory stores until P6) decisions remain
  untouched. `_persistence` snapshot already reports `schema_version`, so the
  P6 swap is observable in the existing surface.

---

## 7. Deferred

- P9 personality/cursor-following: not in P2d (documented seams only, as in
  P2e).
- P3/P4/P7: async metrics, async scanning, CLI config commands — untouched.
- Multi-user/per-app keys, durable request log, `model_status` tables: P5/P6.

---

## 8. File inventory

### New

| File | Purpose |
|---|---|
| `app/services/client_detection.py` | Pure UA → bucket classifier (`classify_client`). |
| `app/services/client_tracking.py` | Bounded metadata-only `ClientTracker` + `activity()`. |
| `app/ui/screens/configuration.py` | `ConfigurationScreen` (form + save/report + masked key). |
| `app/ui/screens/applications.py` | `ApplicationsScreen` (auth/endpoint status + activity table). |
| `app/ui/screens/diagnostics.py` | `DiagnosticsScreen` (tiles, ops/log tails, deep view, actions). |
| `tests/test_client_detection.py` | Bucket table tests. |
| `tests/test_client_tracking.py` | Boundedness, privacy, snapshot. |
| `tests/test_ui_configuration.py` | Configuration screen + facade save/revert/mask. |
| `tests/test_ui_applications.py` | Applications screen render/projections. |
| `tests/test_ui_diagnostics.py` | Diagnostics screen render + actions. |
| `tests/test_diagnostics_export.py` | Export contents/redaction/atomicity + log-tail parser. |

### Modified

| File | Change |
|---|---|
| `app/ui/app.py` | Swap `PlaceholderScreen` → real screens for tabs 5/6/7 (BINDINGS/NOTES already present). |
| `app/ui/screens/__init__.py` | Export the three new screens. |
| `app/ui/data.py` | `ServiceFacade`: `config_form_values`, `save_config`, `auth_status`, `endpoint_status`, `client_activity`, `ops_tail`, `log_tail`, `provider_health_deep`, `test_connection`, `export_diagnostics` (+ config field-group classification helpers). |
| `app/security/auth.py` | Extract `auth_scheme` helper (behavior-neutral refactor). |
| `app/api/middleware.py` | Record client bucket + auth-scheme label into `client_tracking`. |
| `tests/ui_fakes.py` | Fakes for tracker, probe, reload, log tail. |
| `tests/test_ui_data.py`, `tests/test_ui_app.py`, `tests/test_auth.py` | New projections, tab wiring, auth-scheme labels. |

No changes to `app/core/*` or routing/health stores.

---

## 9. Gate criteria

1. Configuration: save applies with the reload report shown; validation failure
   reverts `.env` and shows a redacted error; restart-required fields are
   read-only; `RELAY_API_KEY` never rendered unmasked.
2. Applications: client bucketing unit tests green; a request with a real
   `Authorization: Bearer <token>` leaves no token substring in tracker state;
   tracker bounded + pruned.
3. Diagnostics: export writes the redacted snapshot atomically (no `sk-` /
   `Bearer` / `Authorization` / key values); log tail redacts secret-shaped
   keys; test connection runs in a worker thread and never probes via snapshot.
4. `ServiceFacade` stays Textual-free (boundary test still green).
5. Full `pytest tests -q` green; packaging + wizard + boundary suites green.
