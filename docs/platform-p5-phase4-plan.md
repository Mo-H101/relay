# P5 — Phase 4 Plan: Store-Backed Auth + Admin Key Management

Status: **Phase-planning only. No code yet.** Approved design
(`docs/platform-p5-plan.md`), approved phase plan (`docs/platform-p5-phase-plan.md`
§Phase 4, lines 264-322), and completed foundations: Phase 1 KeyStore +
ProviderKeyStore (`23ee1fe`), Phase 2 keyring-first resolution (`329d2cb`), Phase 3
CLI key workflow (`29188ed`). This document is the concrete Phase 4
implementation plan. No code in this phase; no `PROJECT_LOG.md` changes; stop after
this document and wait for approval.

Phase 4 is the **single request-path change** of the P5 roadmap: `require_api_key`
gains a store-backed tier-2 lookup with scope enforcement, and the admin key
management API lands. The bootstrap `RELAY_API_KEY` path stays byte-identical.

---

## Architecture audit (current state)

### Request authentication flow

- A single global FastAPI dependency guards the whole app:
  `app = FastAPI(..., dependencies=[Depends(require_api_key)], ...)`
  (`app/main.py:55`). Every route inherits it; `/docs`, `/redoc`, `/openapi.json`
  are registered as real endpoints specifically so they inherit it too
  (`main.py:74-104`).
- `require_api_key` (`app/security/auth.py:86-131`) reads `settings.relay_api_key`
  **per request** (hot enable/rotate/disable without restart), compares in
  constant time over SHA-256 digests (`auth.py:56-63`), and returns HTTP 401
  `{"detail": "Unauthorized"}` with `WWW-Authenticate: Bearer` on failure. The
  expected key is never logged or echoed.
- Credential sources: `Authorization: Bearer <token>` or `X-Relay-API-Key`
  (`_extract_token`, `auth.py:66-83`). `PUBLIC_PATHS = {"/", "/health"}`
  (`auth.py:22`) are exempt when auth is enabled.
- When `RELAY_API_KEY` is empty, auth is disabled and all requests pass
  (`auth.py:95-99`) — enforced by `test_authentication_disabled_by_default`.
- `auth_scheme` (`auth.py:28-53`) labels the presented credential method
  (`"public"` / `"bearer"` / `"header"` / `"none"`) for the client tracker; it
  never compares values. The middleware computes its `auth_enabled` flag from
  `settings.relay_api_key` (`app/api/middleware.py:117`).

### Route inventory (for the scope map)

- Public: `/` (`main.py:107`), `/health` (`app/api/health.py:28`).
- Protected, no scope today: `/providers`, `/health/deep`, `/diagnostics`,
  `/metrics`, `/decision/explain`, `/docs`, `/redoc`, `/openapi.json`.
- Protected, chat/v1 in Phase 4: `/chat` (`app/api/chat.py:82`),
  `/v1/chat/completions` + `/v1/models` (`app/api/openai.py:188,372`),
  `/feedback` (`app/api/feedback.py:58`).
- Protected, admin in Phase 4: `/admin/reload` (`app/api/admin.py:26`) and the new
  `/admin/keys*`.

### Admin routes today

- Only `POST /admin/reload` (`app/api/admin.py:26-61`), guarded solely by the
  global dependency. Responses are bounded field sets; errors are redacted
  (`admin.py:51-53`). No key-management surface exists.

### Key stores (Phase 1-3)

- `KeyStore` (`app/services/key_store.py`): SQLite `relay_keys.db`, schema v1
  (`MIGRATIONS`, `PRAGMA user_version`). Persists only scrypt hashes
  (`SCRYPT_N=2^14`); raw keys are generated and returned exactly once by `create`
  (`key_store.py:175-218`). `verify` hashes every **active** row in constant time
  and records `last_used_at` on match (`key_store.py:299-336`); `classify`
  (Phase 3) scans **all** rows read-only and returns
  `ok/invalid/expired/revoked` (`key_store.py:299-341`). Single guarded connection,
  WAL, thread-safe. `state_dir` resolved in `app/core/config.py:69-80`.
- `ProviderKeyStore` (`app/services/provider_key_store.py`): OS-keyring wrapper
  (service `"relay"`, username = provider id). **Not** part of Phase 4.
- CLI (Phase 3): `relay keys add|list|remove|test` and
  `relay provider keys list|set|remove` write/read the same `relay_keys.db` and
  keyring. CLI-created keys are currently **inert** in the request path — Phase 4
  is what makes them live.

### Metrics, ops, redaction

- `relay_metrics.record_auth(enabled, granted, method, failure_reason="invalid")`
  (`app/services/metrics.py:582-597`) feeds `auth_enabled` gauge,
  `auth_success{method}` counter, `auth_failures{reason}` counter. UI reads
  `auth_success.value(method=...)` and `auth_failures.value(reason=...)`
  (`app/ui/data.py:752-760`), so those metric shapes must not change.
- `MetricsMiddleware` (`app/api/middleware.py:40-125`) records HTTP events and
  per-request client metadata (bucket, trimmed UA, auth-scheme label) into the
  bounded `ops_store` rolling window (`app/services/ops_store.py`, in-memory,
  metadata only; `OpsEvent` has no key identity today).
- `redaction` (`app/services/redaction.py`) masks `sk-`/`nvapi-`/bearer shapes and
  sensitive key names. The `rl_` shape is a **Phase 5** addition; Phase 4 never
  emits raw material, so it does not depend on it.
- `test_auth.py` (bootstrap assertions), `test_metrics.py`, `test_redaction.py`,
  `test_admin_reload.py`, `test_key_store.py`, `test_key_cli.py` are the existing
  regression anchors. Baseline: full suite **1709 passed / 9 skipped / 28
  pre-existing `test_rc_validation.py` failures**.

---

## 1. Authentication architecture

### Bootstrap flow (unchanged, byte-identical)

- `RELAY_API_KEY` set → current constant-time path (`auth.py:95-131`) is preserved
  verbatim: same comparison, same 401 shape, same `WWW-Authenticate` header, same
  metric reasons `missing`/`invalid`. Identity attached: `key_id="bootstrap"`,
  `label="bootstrap"`, `scopes=[]` (full access). A bootstrap key **always wins**
  and never triggers a store read — so the bootstrap path works even when the
  store is unavailable.
- `RELAY_API_KEY` empty + store auth off → auth disabled, all requests pass
  (exactly today's behavior; `test_auth.py` passes unchanged).

### New store-backed tier-2 lookup

- New opt-in setting `RELAY_AUTH_STORE` (default `false`) →
  `settings.relay_auth_store` (additive field in `app/core/config.py`, read per
  request like `relay_api_key`, so enabling/disabling is hot without a restart;
  not added to the `/admin/reload` allowlist).
- `auth_configured()` helper (`app/security/auth.py`):
  `bool(RELAY_API_KEY) or bool(RELAY_AUTH_STORE)`. Replaces the raw
  `settings.relay_api_key` boolean in `require_api_key` and in the middleware's
  `_record_client` auth-enabled flag, so `PUBLIC_PATHS` stay labeled `public` and
  exempt under store auth too.

### Precedence rules (per request, in order)

1. `auth_configured()` false → allow (auth disabled; gauge 0).
2. `path in PUBLIC_PATHS` → allow, record `public`.
3. No token → 401 `missing`.
4. `RELAY_API_KEY` set and `_constant_time_eq(token, expected)` → allow as
   `bootstrap` (full access). Tier 2 never consulted.
5. Store auth **off** and bootstrap mismatch → 401 `invalid` (today's behavior).
6. Store auth **on** → tier-2 lookup:
   - `verify(token)` returns meta → allow, attach identity, `verify` records
     `last_used_at`.
   - else `classify(token)` → reason `invalid` / `revoked` / `expired`.
   - store raises → reason `store_unavailable`.

### Fail-closed behavior

- With `RELAY_AUTH_STORE` on, every non-public request requires a valid key; no
  token, wrong token, revoked, expired, or store failure all yield **401**. There
  is no silent-open on store error.
- **Identity is never leaked**: all failures return the identical
  `401 {"detail": "Unauthorized"}` + `WWW-Authenticate: Bearer`; the specific
  reason (`invalid`/`revoked`/`expired`/`store_unavailable`) appears only in the
  metrics reason label and the ops audit event — never in the HTTP body, so
  callers cannot distinguish "store down" from "wrong key" (no oracle).
- Store lookup runs inside `try/except Exception` and is guarded by the KeyStore's
  own open/migration/backup logic; any exception → `store_unavailable`.

### Error states (summary)

| State | HTTP | Metric reason | Notes |
|---|---|---|---|
| No token | 401 | `missing` | unchanged |
| Bootstrap mismatch, store off | 401 | `invalid` | unchanged |
| Store key, no row match | 401 | `invalid` | |
| Revoked store key | 401 | `revoked` | via `classify` |
| Expired store key | 401 | `expired` | via `classify` |
| Key store unavailable | 401 | `store_unavailable` | fail closed, identical body |
| Authenticated, insufficient scope | 403 | — | `{"detail": "Forbidden"}` |

### Bootstrap byte-compatibility guarantee

- Tier 1 runs before any store I/O and is byte-identical to today (same
  comparison, same responses, same metrics). Existing clients sending
  `Authorization: Bearer $RELAY_API_KEY` or `X-Relay-API-Key: $RELAY_API_KEY`
  behave exactly as before, whether or not `RELAY_AUTH_STORE` is set. Store keys
  are additive: a second credential space, never a replacement for the bootstrap
  key.

---

## 2. Key model and permissions

### Stored model (schema v1 already exists — no schema change)

The Phase-1 `api_keys` table already carries every required field; Phase 4 only
consumes it:

- `id` — opaque uuid hex (32 chars), **not secret**; used as `key_id` everywhere.
- `key_hash` + `key_salt` + `kdf` — scrypt digest, per-row KDF parameters. Never
  exposed by any surface.
- `label` — human-readable.
- `scopes` — JSON array of `admin` / `chat` / `v1`.
- `created_at`, `expires_at`, `last_used_at`, `revoked_at` — unix timestamps or
  NULL.

Raw key material exists only at `create` time (returned once, never persisted).

### Scope semantics

- Scope set: `admin`, `chat`, `v1`. A key may hold any subset; `[]` (empty) means
  **full access** (a bootstrap-equivalent store key).
- Route → required-scope map (enforced in `require_api_key`, not per route):
  - `/admin` and `/admin/*` (incl. `/admin/reload`, `/admin/keys*`) → `{admin}`.
  - `/chat`, `/chat/*`, `/v1/*`, `/feedback` → `{chat, v1}` (satisfied by either).
  - all other protected routes (`/providers`, `/diagnostics`, `/metrics`,
    `/health/deep`, `/decision/*`, `/docs`, `/redoc`, `/openapi.json`) → no scope
    requirement (any authenticated key).
- Grant rule: `required = _scope_required(path)`; grant when `not required` OR
  `not key_scopes` (empty = full access) OR
  `not required.isdisjoint(key_scopes)`. Non-grant after successful
  authentication → **403** `{"detail": "Forbidden"}`.
- Bootstrap identity (`scopes=[]`) always grants (full access), unchanged.

### Constant-time verification preservation

- Success path uses the existing `verify()` loop: every active row is scrypt-hashed
  and compared with `hmac.compare_digest` in constant time, so the DB never leaks
  which key matched through timing. The request path reuses the exact Phase-1
  code; nothing about hashing or KDF semantics changes.
- Failure path calls the Phase-3 `classify()` (read-only, same constant-time loop)
  only to bucket the reason for metrics. Cost note: an unsuccessful attempt does
  two full scans (verify + classify); acceptable because attempts are rare, active
  row counts are small, and the alternative (reading state per row) would leak
  timing. A future hardening step may merge the two loops into one status-returning
  scan (Phase 5 candidate, not required here).
- Store reads are per-request via a module-level lazy `KeyStore` singleton in
  `auth.py` (guarded single connection, WAL — built for the request path by Phase
  1 design). `verify` updates `last_used_at` on success, so `GET /admin/keys`
  shows real usage without extra plumbing.

### Revoked/expired behavior

- `revoke` sets `revoked_at` (soft delete). A revoked key is skipped by `verify`
  and reported as `revoked` by `classify` → 401 `revoked`; it still appears in
  `list`/`inspect` with `revoked_at` set. `last_used_at` stops updating.
- An expired key (`expires_at <= now`) is skipped by `verify`, reported as
  `expired` → 401 `expired`; still listed. Revocation of an expired key is a no-op
  (already unusable).
- A **permanently deleted** key (see §4) is removed from the table entirely, so a
  later presentation is `invalid` (the row no longer exists to classify).

---

## 3. Request-path integration

### Files to change

| File | Change | Kind |
|---|---|---|
| `app/security/auth.py` | Tier-2 store lookup, scope map, `auth_configured()`, `request.state.key_*`, `_key_store()` test hook, fail-closed error reasons | Modified |
| `app/core/config.py` | Additive `relay_auth_store` (`RELAY_AUTH_STORE`, default `false`), read per request | Modified (additive only) |
| `app/api/keys.py` | New admin key-management router (`/admin/keys*`) | New |
| `app/main.py` | `include_router(keys_router)` in the existing block (`main.py:63-71`) | Modified |
| `app/services/metrics.py` | `record_auth` gains `key_id`; new `auth_by_key` + `key_admin` counters; keep `auth_success{method}` / `auth_failures{reason}` shapes | Modified (additive) |
| `app/api/middleware.py` | Pass `request.state.key_id` (via `scope["state"]`, opaque uuid) into ops events; `auth_enabled` via `auth_configured()` | Modified |
| `app/services/ops_store.py` | `OpsEvent` gains opaque `key_id` field; `record_http` accepts it; new `record_key_action` (kind `key_admin`) | Modified (additive, in-memory) |
| `app/services/key_store.py` | Additive `delete(key_id)` (permanent delete) for the delete endpoint | Modified (additive) |
| `tests/test_key_auth.py`, `tests/test_admin_keys.py` | New suites (§6) | New |

### Constraints honored

- **No API contract breaking changes**: all existing routes, request/response
  shapes, status codes, and `PUBLIC_PATHS` are untouched. New endpoints are purely
  additive.
- **Existing `RELAY_API_KEY` clients keep working**: Tier 1 is byte-identical
  (`test_auth.py` passes unchanged).
- **No provider changes**: factory, registry, clients, `provider_key_store`,
  `config_store` untouched.
- **No persistence/state-store changes**: `app/services/state_store.py`, the
  persistence subsystem, and `relay_keys.db` schema are untouched. `ops_store` is
  the in-memory diagnostics window (explicitly independent of persistence) and
  gains only an opaque metadata field.
- **No `PROJECT_LOG.md` changes**.
- `auth.py` was left untouched by Phases 1-3 precisely so this one-phase revert
  stays clean.

### Untouched (must remain byte-identical)

- `/v1/*`, `/chat`, `/health`, `/`, `/admin/reload` response shapes and status
  codes; `PUBLIC_PATHS`; docs/redoc/openapi gating; `tests/test_auth.py`
  assertions; provider/reload/config_store/keyring modules; the Phase-3 CLI
  commands and help text.

---

## 4. Admin API

All `/admin/keys*` routes are guarded by the global `require_api_key` dependency,
which already enforces the `admin` scope for `/admin/*` paths — no per-route auth
decorators needed. Unauthenticated → 401 (identical body to every other 401);
authenticated without the `admin` scope → 403.

Endpoints (mapping the five requested capabilities):

| Capability | Endpoint | Notes |
|---|---|---|
| create key | `POST /admin/keys` | raw key returned **exactly once** |
| list keys | `GET /admin/keys` | metadata only, never hash/raw |
| inspect key | `GET /admin/keys/{key_id}` | single metadata object |
| revoke key | `DELETE /admin/keys/{key_id}` | soft delete (phase-plan contract) |
| delete key | `DELETE /admin/keys/{key_id}?permanent=true` | hard delete (additive) |

### `POST /admin/keys`

Request: `{"label": str, "scopes": [str] = [], "expires_days": int | null}`.
Validation → 400: label required after strip; scopes must be a subset of
`{admin, chat, v1}` (duplicates deduped); `expires_days` a positive integer.
Malformed body → 422 (FastAPI). Delegates to `KeyStore.create`.

Response `201`:

```json
{
  "key_id": "<full uuid>",
  "label": "opencode",
  "scopes": ["chat", "v1"],
  "created_at": "2026-08-05T12:00:00+00:00",
  "expires_at": null,
  "api_key": "rl_<43 chars>"
}
```

`api_key` is the **only** place raw material ever appears on the admin surface.

### `GET /admin/keys`

Response `200`: `{"keys": [ <meta>, ... ]}` where each meta is:

```json
{
  "key_id": "...", "label": "...", "scopes": [...],
  "created_at": "iso", "expires_at": "iso|null",
  "last_used_at": "iso|null", "revoked_at": "iso|null"
}
```

Never `key_hash`, `key_salt`, or any raw value. Times are ISO 8601 UTC or `null`.

### `GET /admin/keys/{key_id}`

`200` single meta (same shape). `404` `{"detail": "Key not found"}` for unknown id
(exact-match only; no prefix resolution on the API).

### `DELETE /admin/keys/{key_id}` (revoke)

Soft delete via `KeyStore.revoke`. `200` `{"key_id": "...", "status": "revoked",
"already_revoked": bool}` (idempotent: already-revoked → `already_revoked: true`).
`404` for unknown id.

### `DELETE /admin/keys/{key_id}?permanent=true` (delete)

Hard delete via new additive `KeyStore.delete(key_id) -> bool`. `200`
`{"key_id": "...", "status": "deleted"}`. `404` for unknown id. Without
`permanent=true` the same route revokes (previous row) — the flag is explicit so a
scripted revoke can never accidentally destroy a row. A hard-deleted key is gone
from `list`/`inspect` and any future presentation is `invalid`.

### Redaction rules

- `key_id` is an opaque uuid (not secret) and is the only identity exposed.
- No endpoint returns `key_hash`/`key_salt`/raw, except `POST` create's `api_key`
  (one-time). `--json`-style machine output contains no hash.
- Error bodies are bounded: 400 messages name only the offending field
  (`"label is required"`, `"unknown scope 'foo'"`), never values; 401/403/404 are
  fixed strings; unexpected store exceptions map to 500 `{"detail": "Internal
  error"}` without exception text (consistent with `admin.py:51-53`).

---

## 5. Metrics and logging

### Metrics (additive; existing shapes preserved)

- `record_auth(enabled, granted, method, failure_reason="invalid", key_id="")`:
  - `auth_success{method}` and `auth_failures{reason}` counters unchanged — UI
    consumers (`app/ui/data.py:752-760`) and `test_metrics.py` keep working.
  - New failure reasons on the existing `auth_failures` counter: `revoked`,
    `expired`, `store_unavailable` (bounded set alongside `missing`/`invalid`).
  - New counter `relay_auth_by_key_total{key_id}` on success: `"bootstrap"` for
    Tier 1, the store key uuid for Tier 2, `"public"` for public paths. Bounded
    cardinality: one series per key plus the two fixed values.
- New counter `relay_key_admin_actions_total{action, outcome}`: `action` in
  `{create, list, get, revoke, delete}`, `outcome` in `{ok, not_found, forbidden,
  invalid}`. Records every `/admin/keys` call.
- Labels contain only uuids and fixed enum values — never tokens, hashes, or
  labels. Matches the registry's existing "fixed, bounded label sets" contract
  (`metrics.py:8-11`).

### Audit events

- `OpsEvent` gains an opaque `key_id` field (`ops_store.py`); the middleware passes
  `scope["state"].get("key_id", "")` into `record_http`, giving per-key request
  correlation in the diagnostics window. Bootstrap requests carry `"bootstrap"`;
  unauthenticated carry `""`.
- New `ops_store.record_key_action(action, key_id, outcome)` writes
  `kind="key_admin"` events (action, key_id, outcome, timestamp) for the admin key
  lifecycle. Same in-memory rolling window, same pruning bounds — no new storage,
  no persistence.

### Redaction / no-leak guarantees

- Auth never logs; tokens and hashes never appear in any metric label, ops event,
  or admin response (except the single `POST /admin/keys` `api_key`, which is the
  defined one-time creation surface).
- The middleware already stores only the auth-**scheme** label and trimmed UA
  (`middleware.py:104-125`); the Authorization header value is never stored. Phase
  4 only adds the opaque uuid.
- The `rl_` value shape is added to `redaction.py` in Phase 5 as defense-in-depth;
  Phase 4 does not rely on it because secrets are never emitted in the first place.

---

## 6. Tests

New suites `tests/test_key_auth.py` (request-path auth + scopes) and
`tests/test_admin_keys.py` (admin endpoints), using the `TestClient` pattern of
`test_auth.py`/`test_admin_reload.py`. Store injection via the `_key_store()` hook
in `auth.py` (temp-path `KeyStore`, or a raising stub for `store_unavailable`);
settings toggled with `monkeypatch.setattr(settings, "relay_auth_store", ...)`.

1. **Auth regression (existing suite passes unchanged)**: `test_auth.py` green with
   no edits — bootstrap constant-time path, disabled-by-default, public paths,
   bearer/header, no-leak bodies.
2. **Store-backed auth**: key created via `KeyStore.create` (or CLI) authenticates
   via both `Authorization: Bearer` and `X-Relay-API-Key`; wrong token → 401
   `invalid`; `request.state.key_id/label/scopes` populated on success; bootstrap
   key still accepted (full access) with `RELAY_AUTH_STORE` on.
3. **Scope enforcement**: `admin`-scoped key on `/admin/keys`; `chat`/`v1` key on
   `/chat` and `/v1/chat/completions` (and `/feedback`); empty-scope key has full
   access (including `/admin/keys`); authenticated-but-wrong-scope → 403; bootstrap
   unaffected; `/providers`-class routes accept any authenticated key.
4. **Expiry / revocation**: revoked key → 401 `revoked` (metric reason); expired
   key → 401 `expired`; `last_used_at` updates on success and stops after revoke;
   `list`/`inspect` reflect `revoked_at`/`expires_at`.
5. **Admin endpoints**: `GET /admin/keys` shape has no hash/raw; `POST /admin/keys`
   returns raw exactly once, it authenticates end-to-end against `/v1`, and the
   raw value never appears in a subsequent `GET`; `DELETE .../{id}` revokes and the
   revoked key subsequently returns 401; `DELETE .../{id}?permanent=true` removes
   the row (subsequent presentation → `invalid`); `GET /admin/keys/{id}` inspect +
   404; 401 unauthenticated; 403 non-admin scope; 400/422 validation (bad scopes,
   empty label, non-positive `expires_days`).
6. **Failure modes**: store unavailable (`_key_store` raises) → 401
   `store_unavailable` with an identical body to `invalid` (no oracle); missing
   token → 401 `missing`; store auth on with an empty store → every protected path
   401; bootstrap key still works while the store is down.
7. **Backwards compatibility**: `RELAY_AUTH_STORE=false` reproduces today exactly
   (bootstrap only, disabled-by-default); `RELAY_API_KEY` + store on → both work,
   `/admin/reload` and `/v1` response shapes unchanged; full suite green (1709 /
   9 / 28 pre-existing `test_rc_validation.py` failures, no new failures).

---

## 7. Migration and rollback

### Enabling / disabling store-backed auth

- **Enable**: set `RELAY_AUTH_STORE=true` (`.env` or environment) and restart, or
  rely on per-request reads for a hot flip. Existing CLI-created keys in
  `relay_keys.db` become live immediately — no migration, no rekey. The bootstrap
  key keeps working alongside. Admin scope must be granted at create time
  (`relay keys add --label "ops" --scopes admin`).
- **Disable**: set `RELAY_AUTH_STORE=false`. Store lookups stop; the API returns
  to bootstrap-only semantics; store keys become inert (they remain in the DB,
  listed by `relay keys list`, usable again on re-enable).
- **Order of operations for a staged rollout**: keep `RELAY_API_KEY` set as the
  out-of-band fallback, enable `RELAY_AUTH_STORE`, create scoped keys, then rotate
  workloads onto store keys before removing `RELAY_API_KEY`. Because Tier 1 runs
  first, the bootstrap key is always a safe escape hatch while it is set.

### Rollback safety

- Revert the Phase-4 commit (single commit, single request-path change): `auth.py`
  returns to the pure-bootstrap dependency, the `/admin/keys` router unmounts, the
  additive `delete`/metrics/ops changes revert. `test_auth.py` and every
  Phase 1-3 behavior are restored exactly.
- Keys created during testing remain in `relay_keys.db` (inert once auth reverts);
  provider keyring entries and `.env` values are unaffected (Phase 4 does not touch
  them). No data migration is involved in either direction.
- Because Phases 1-3 explicitly left `auth.py` untouched, this is the planned,
  clean single-commit rollback of the only request-path change.

### Interaction with Phase 1-3 components

- **KeyStore**: schema v1 and hashing unchanged; Phase 4 adds only the read path
  (`verify`/`classify`) and an additive `delete`. CLI-created keys and
  admin-created keys share one DB.
- **CLI (Phase 3)**: unchanged commands; `relay keys add --scopes admin` now has
  real meaning on the wire. `relay keys test` and `list` work against the same
  rows.
- **Provider keyring (Phases 1-2)**: untouched; auth is orthogonal to provider
  credential resolution.
- **Phase 5**: adds `rl_` redaction, permissions audit, docs, `.env.example`, and
  the `relay keys provider migrate` command — none of which are prerequisites for
  Phase 4.

---

## Scope

### Files expected to change

- New: `app/api/keys.py`, `tests/test_key_auth.py`, `tests/test_admin_keys.py`.
- Modified (additive, non-breaking): `app/security/auth.py`,
  `app/core/config.py`, `app/main.py`, `app/services/metrics.py`,
  `app/api/middleware.py`, `app/services/ops_store.py`,
  `app/services/key_store.py` (`delete` only).

### Untouched

- `/v1`, `/chat`, `/feedback`, `/health`, `/`, `/providers`, `/diagnostics`,
  `/metrics`, `/decision/*`, `/docs`, `/redoc`, `/openapi.json` response shapes and
  status codes; `PUBLIC_PATHS`; `tests/test_auth.py`; `app/api/admin.py`;
  `app/providers/*`; `app/services/reload.py`, `config_store.py`,
  `provider_key_store.py`, `state_store.py`, `persistence`; `app/cli/*`; `.env`,
  `.env.example`, `docs/*`, `PROJECT_LOG.md`.

### Risks

- **Scope-map fidelity**: route prefixes are matched in `require_api_key` via
  `request.url.path`; guarded by the scope tests and by keeping the map next to
  `PUBLIC_PATHS`.
- **Scrypt cost on the request path**: constant-time iteration over active rows per
  attempt is the Phase-1 timing-safety design; bounded by small key counts, and
  failure attempts only pay the extra `classify` pass. Phase 5 may merge the loops.
- **Fail-closed surprise**: enabling store auth with no keys locks everything
  except `PUBLIC_PATHS` — documented in the enable path and the failure-mode tests;
  the bootstrap key is the documented fallback.
- **Metric cardinality**: `auth_by_key{key_id}` grows one series per key; bounded
  per design (keys are user-created, few), matching the phase-plan's "one per key +
  bootstrap".
- **Permanent delete footgun**: `?permanent=true` is explicit and distinct from the
  default revoke; covered by tests and 404-on-unknown.

### Rollback strategy

- Revert the Phase-4 commit (see §7). No data migration; created keys stay inert;
  `auth.py` reverts to the pre-Phase-4 bootstrap-only dependency.

---

## Acceptance criteria

1. `relay keys add --label "opencode" --scopes chat,v1` → the returned key
   authenticates end-to-end against `/v1/chat/completions` and `/chat` (roadmap P5
   exit criterion), and the key is listed/revoked/deleted via `/admin/keys`.
2. Bootstrap `RELAY_API_KEY` path is byte-identical: `tests/test_auth.py` passes
   unchanged, and the key still authenticates with store auth on or off.
3. Store-backed auth fails closed (401) on missing/invalid/revoked/expired keys and
   on store unavailability, with identical 401 bodies (reason only in metrics) and
   403 for insufficient scope.
4. Raw keys appear exactly once on the admin surface (the `POST /admin/keys`
   response); `GET`/`DELETE`/inspect never expose hashes or raw material; metrics
   and ops labels carry only opaque uuids and fixed enum values.
5. Full suite green: 1709 passed / 9 skipped / 28 pre-existing
   `test_rc_validation.py` failures, no new failures.

Stop — no code, no commit until this plan is approved.
