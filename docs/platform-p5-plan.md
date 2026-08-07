# P5 — Implementation Plan: API-Key Security & Secret Management

Status: **Planning only. No code yet.** Detailed implementation plan for the approved P5
work item (API-key security and secret management). Implementation starts only after this
plan is approved.

Source: `docs/platform-implementation-roadmap.md` §P5 (lines 93-107). Approved decisions
applied: `RELAY_API_KEY` bootstrap retained; `.env` stays a supported bootstrap/compat
path; `api_keys` table introduced in P5 and folded into `platform.db` in P6; per-key
correlation into metrics/`request_log`; `security-best-practices` review gate before
implementation (roadmap cross-cutting note, line 146).

Constraints (user-mandated): **no code in this phase — plan only**; no `PROJECT_LOG.md`;
no code at all; stop after the plan and wait for approval.

---

## 1. Current secret handling (inspection findings)

### 1.1 Where secrets live today

- **Provider API keys** are parsed by `Settings` straight from the environment
  (`app/core/config.py:269,275,281,297,303,319,333`): `nvidia_api_key`,
  `openai_api_key`, `anthropic_api_key`, `openrouter_api_key` (parsed, unused),
  `gemini_api_key`, `groq_api_key` (parsed, unused), `lmstudio_api_key`. The active
  definitions and their env var names come from the registry
  (`app/providers/registry.py:77-207`, `key_env`/`key_attr`).
- **They are persisted in plaintext in `.env`.** The env file is resolved at
  `config.py:37-59`: source checkout → cwd `.env` then project-root `.env`; installed →
  `<user data dir>/.env`; `RELAY_ENV_FILE` overrides. Keys are written there by
  `app/services/config_store.py` (docstring: *the only module allowed to write provider
  configuration*) via `set_env`/`set_provider_config` (`config_store.py:21-27,51-77`).
- **Flow into the runtime**: `app/providers/factory.py:41`
  (`build_runtime_provider`) reads `settings.<key_attr>` and hot reload applies key
  changes at `app/services/reload.py:239-241`
  (`provider.api_key = getattr(env, f"{prefix}_api_key")`).
- **Relay's own inbound key** is `RELAY_API_KEY` (`config.py:406`), read on every
  request in `app/security/auth.py:95`. It gates the whole app via the global dependency
  (`app/main.py:55`, `dependencies=[Depends(require_api_key)]`). Public allowlist:
  `PUBLIC_PATHS = {"/", "/health"}` (`auth.py:22`).

### 1.2 Existing secret-handling infrastructure (reuse, do not redo)

- **Constant-time comparison** already exists: `_constant_time_eq` hashes both sides with
  SHA-256 and compares digests with `hmac.compare_digest` (`auth.py:56-63`). Per-request
  freshness: the expected value is re-read from settings on every request
  (`auth.py:95`), so enabling/rotating/disabling takes effect without restart.
- **Header parsing**: `_extract_token` accepts `Authorization: Bearer <key>` or
  `X-Relay-API-Key: <key>` (`auth.py:24-25,66-83`); 401s never reveal the expected key
  and include `WWW-Authenticate: Bearer` (`auth.py:110-122`).
- **Redaction layer** (`app/services/redaction.py`): masks by key name
  (`SENSITIVE_KEYS`, `redaction.py:23-35`) and by known value shapes
  (`sk-…`, `nvapi-…`, `Bearer …`, `authorization`/`x-relay-api-key` header values,
  `redaction.py:42-48,67-75`). Applied to every Diagnostics export and rendered log
  metadata. Provider error bodies are additionally scrubbed by `_safe_provider_body` /
  `safe_error_body` (key stripped).
- **Reload redaction**: `reload.py` reports secrets by field name only
  (`_SECRET_FIELDS`, `reload.py:97-102`) and strips offending values out of validation
  errors (`_redact`, `reload.py:161-169`). Conformance suite pins this
  (`test_provider_conformance.py`).
- **Wizard masking**: `mask_key` renders keys as `********abcd`
  (`app/setup/key_validation.py:54-60`); keys are validated live and never echoed.
- **Metrics/observability**: `record_auth` labels only the credential method
  (`"bearer"`/`"header"`/`"public"`/`"none"`), never values (`app/services/metrics.py:582-597`);
  `client_tracking` never stores `Authorization` header values or keys
  (`app/services/client_tracking.py:1-11,104-138`).
- **Config store single-writer invariant**: only `config_store` touches dotenv; the
  wizard routes all writes through it (`app/setup/wizard.py:12-14`).

### 1.3 Weaknesses P5 closes

1. **Provider keys sit in plaintext `.env`** — world-readable risk on shared systems, no
   per-provider rotation/removal surface, and the wizard writes them there even when a
   secure OS keyring is available.
2. **One shared `RELAY_API_KEY` for every client** — no scoping, no per-app identity, no
   expiry, no revocation, no rotation. Cline/OpenCode/Continue all share one credential.
3. **No key lifecycle surface** — there is no `relay keys` subcommand
   (`app/cli.py:125-156` exposes only `setup`/`tui`/`serve`) and the only admin route is
   `POST /admin/reload` (`app/api/admin.py:26`).
4. **No per-key correlation** — auth metrics and the client tracker know the credential
   method and client bucket, but not *which* key was used, so per-app usage/audit is
   impossible until P6's `request_log`.
5. **Bootstrap-only auth path** — `auth.py:95` compares against a single plaintext
   settings value; the store-backed path must preserve constant-time comparison,
   per-request freshness, and fail-closed behavior.

---

## 2. P5 goals (roadmap scope, verified against lines 93-107)

- **App keys**: `api_keys` table (scrypt, per-key salt, label, scopes, expiry, rotation,
  revoke); `relay keys` CLI; admin API (`POST/GET /admin/keys`,
  `DELETE /admin/keys/{id}`); `auth.py` store-backed lookup with constant-time compare;
  `RELAY_API_KEY` bootstrap retained; per-key correlation into metrics (full
  `request_log` integration lands in P6).
- **Upstream provider keys**: moved out of plaintext `.env` into the OS keyring
  (`keyring`); `.env` stays a supported bootstrap/compat path (documented decision).
- **Client integration keys**: `relay keys create --label "opencode"` flow so
  Cline/OpenCode/Continue get a scoped Relay key (requirement 20).
- **Tests**: hash/verify, expiry, revoke, scope enforcement, constant-time, privacy (no
  key material/prompts persisted), keyring round-trip.
- **Exit criterion**: `relay keys create` key works end-to-end against `/v1`.

---

## 3. Architecture

Proposed decisions are labeled **D1..D10**; each is a recommendation for approval.

### 3.1 Storage model

**App keys — D1: new `KeyStore` SQLite database.** New module
`app/services/key_store.py` owning `relay_keys.db` under `state_dir`
(`config.py:69-80`; `.relay/` in a source checkout, per-user data dir when installed).
It reuses the internal migration pattern already proven by `StateStore` (`MIGRATIONS`
dict + `PRAGMA user_version`, `state_store.py:30-110,618-639`) so the `api_keys` table
can be folded into `platform.db` unchanged in P6.

`api_keys` table (schema v1):

| column | type | notes |
|---|---|---|
| `id` | TEXT PK | opaque uuid4 hex; never the raw key |
| `key_hash` | BLOB | scrypt digest (32 B) — **never indexed** |
| `key_salt` | BLOB | 16 B random, per key |
| `kdf` | TEXT | algorithm + params, e.g. `"scrypt|16384|8|1"` — per-row for future upgrades |
| `label` | TEXT | e.g. `"opencode"` |
| `scopes` | TEXT | JSON array; empty `[]` = full access (bootstrap parity) |
| `expires_at` | REAL NULL | epoch; NULL = never |
| `created_at` | REAL | epoch |
| `last_used_at` | REAL NULL | updated on successful auth |
| `revoked_at` | REAL NULL | NULL = active |

Only hashes are persisted — raw keys never touch disk. `key_hash` deliberately has no
index: verification iterates active rows with a constant-time compare so the DB cannot
leak key identity through timing (see 3.4).

**Provider keys — D2: OS keyring backend.** New module
`app/services/provider_key_store.py` wrapping the `keyring` package: service name
`"relay"`, username = provider id (`"nvidia"`, `"openai"`, `"anthropic"`, `"gemini"`,
`"lmstudio"`). Surface: `get(provider_id)`, `set(provider_id, value)`,
`remove(provider_id)`. An optional `RELAY_KEYRING` env toggle gates the feature, and
`RELAY_KEYRING_BACKEND` overrides the backend for tests and headless Linux
(credential-manager caveat documented in 7.3). No plaintext-file fallback in P5 (D9).

### 3.2 Encryption approach

- **App keys — D3: `hashlib.scrypt`** (stdlib, no new crypto dependency), memory-hard per
  the roadmap. Params `N=2**14, r=8, p=1`, `dklen=32`, fresh 16 B salt per key; params
  stored in the `kdf` column so a future upgrade can re-hash without breaking existing
  rows. Verification uses `hmac.compare_digest`.
- **Raw key format — D4: `rl_` + 43 base62 chars (256-bit entropy).** The prefix gives
  the redaction layer a cheap, reliable shape to mask everywhere
  (GitHub/Stripe-style one-time display). The raw key is shown exactly once, at creation.
- **Provider keys — D5: encrypted by the OS keyring backend itself.** Relay never
  handles a master key. The roadmap's alternative ("encrypted store with local master
  key") is documented as a deferred fallback (7.3), not built in P5.

### 3.3 Key lifecycle

- **create**: generate raw key → scrypt-hash → insert row → return raw key **once**
  (CLI prints masked label + one-time raw key; admin `POST /admin/keys` returns the raw
  key once in the response body).
- **list**: id, label, scopes, expires_at, created_at, last_used_at, revoked_at — never
  hash or raw (`GET /admin/keys`, `relay keys list`).
- **revoke**: set `revoked_at`; rejected at next request (`DELETE /admin/keys/{id}`
  does this, `relay keys revoke <id>`).
- **rotate**: create a new key and revoke the old (app keys); for provider keys, set the
  new value in the keyring, re-verify, then remove the old value.
- **expiry**: `expires_at` enforced at verification; an expired key behaves like
  revoked (401) with a distinct failure reason for metrics.
- **CLI — D6: `relay keys` argparse subcommand tree** matching the existing
  `app/cli.py:125-156` style: `create --label --scopes --expires-days`, `list`,
  `revoke <id>`, and `provider set/get/remove <provider-id>` for the keyring-backed
  upstream keys.

### 3.4 Access boundaries

**D7: `require_api_key` resolves in two tiers, bootstrap first.**

1. If `RELAY_API_KEY` is set: behavior is byte-identical to today
   (`auth.py:95-131`) — constant-time compare against the settings value, key identity
   `"bootstrap"`, full access. This keeps `tests/test_auth.py` green unchanged.
2. Otherwise: extract the token, iterate **active** (not revoked, not expired) `api_keys`
   rows, scrypt-verify each with constant-time semantics, then attach
   `request.state.key_id` / `key_label` / `key_scopes`. Store read failure **fails
   closed** (401, reason `store_unavailable`).

**Scope enforcement (D8).** An endpoint→scope map: `/admin/*` requires the `admin`
scope; `/chat`, `/v1/*`, and `/feedback` require `chat`/`v1`; `PUBLIC_PATHS` unchanged.
Empty `scopes` on a key = full access (matches bootstrap semantics). A bootstrap key
always has full access. Admin key management routes are only reachable with `admin`
scope or the bootstrap key.

**Per-key correlation (D10).** `record_auth` gains a `key_id` label (bounded by the
number of keys); the key id (opaque, non-secret) is also carried into ops events. The
full `request_log` correlation is P6.

### 3.5 Runtime provider integration

- **Resolution order (D2 continuation)**: provider key = `ProviderKeyStore.get(id)`
  first; `.env`/env value is the fallback when the keyring has no entry. Applied in the
  two places keys enter the runtime: `factory.build_runtime_provider`
  (`factory.py:41`) and reload (`reload.py:240`).
- **Single-writer invariant preserved**: `config_store.set_provider_config` routes its
  `api_key` argument to the provider key store when `RELAY_KEYRING` is on, and keeps the
  `.env` path otherwise. Non-key paths (`enabled`, `base_url`, `priority`) are untouched
  (`config_store.py:66-77`). The wizard keeps calling `config_store` unchanged
  (`wizard.py:12-14`).
- **Client flow (requirement 20)**: `relay keys create --label "opencode"` →
  OpenCode/Cline/Continue point at Relay's base URL with the returned scoped key; docs
  updated in `docs/deployment.md` + README.

### 3.6 Reload behavior

- `_SECRET_FIELDS` semantics unchanged — secrets reported by field name only, never
  echoed in responses, logs, or exceptions (`reload.py:97-102,161-169`).
- Provider key changes on reload re-read the provider key store; failures stay
  best-effort/non-fatal exactly as today (`reload.py:205-261`).
- Redaction extended for the `rl_` shape (3.2) so new app keys are masked everywhere the
  existing `sk-`/`nvapi-` shapes are (see 3.4 security requirements).

### 3.7 Failure handling

- **Keyring unavailable at write time** (CLI/wizard): fail with clear guidance
  (set `RELAY_KEYRING` / install a backend). **At request time**: never crashes — key
  resolution falls back to the env value (compat path).
- **Key store unreadable/corrupt at auth time**: fail closed (401,
  `store_unavailable`); DB corruption recovery reuses the `StateStore._backup_corrupt`
  pattern (`state_store.py:641-661`) then recreates an empty store.
- **Revoked/expired**: 401 with distinct metric reasons (`revoked` / `expired`) so
  operators can distinguish them from wrong keys (`invalid`).

---

## 4. Security requirements

- **No plaintext key material in any normal state file.** `relay_keys.db` stores only
  scrypt hashes; provider keys live only in the OS keyring (encrypted by the backend).
- **Redaction everywhere**: extend `redaction.py` shape patterns with `rl_…` and keep the
  existing key-name masking; diagnostics exports, log rendering, provider error bodies,
  reload reports, and CLI output all pass through it.
- **One-time display**: CLI and admin API print the raw key exactly once, at creation;
  `list`/`revoke`/`rotate` never print raw keys or hashes.
- **Constant-time for every comparison** (`_constant_time_eq` + `hmac.compare_digest` on
  scrypt digests); no indexed equality lookup that leaks which key matched.
- **Permissions model**: `relay_keys.db` and the keyring sit in per-user locations
  (`state_dir` / OS credential store); no world-readable writes. `state_dir` already
  defaults to user-scoped paths (`config.py:69-80`).
- **Local-first / offline**: key creation, verification, and provider-key storage make no
  network calls and work with no upstream connectivity.
- **Fail-closed auth**: any key-store read error denies access (401); the only
  downgrade is the documented `.env` bootstrap/compat path for provider keys.

---

## 5. Backward compatibility

- **`.env` continues to work.** Provider keys already present in `.env` still load via
  the env fallback (3.5); nothing is silently removed. Precedence (documented in
  `docs/configuration.md`): a keyring-stored key wins over `.env` when present.
- **`RELAY_API_KEY` bootstrap is retained byte-identical** — tier-1 path, full access,
  per-request freshness; `tests/test_auth.py` keeps passing unchanged.
- **Existing provider configs are not broken.** Only the `api_key` write path in
  `config_store` migrates; `enabled`/`base_url`/`priority` handling and the wizard flow
  are untouched.
- **No API contract changes to existing endpoints.** New `relay keys` CLI and
  `POST/GET /admin/keys`, `DELETE /admin/keys/{id}` are additive. `/v1`, `/chat`,
  `/admin/reload`, metrics, and diagnostics responses are unchanged.
- **Regression gate**: the full baseline suite (1620 passed, 10 skipped, 0 failed at
  P4.3.4) stays green; `tests/test_provider_conformance.py` (0.63s) and
  `tests/test_redaction.py` are extended, not altered.

---

## 6. Testing strategy

| Area | Coverage |
|---|---|
| **Key store** | hash/verify, per-key salt, KDF params round-trip, constant-time, expiry, revoke, rotate, scopes persistence, tamper (bit-flip → reject), corrupt-DB recovery, `memory_counts`. |
| **Auth (store-backed)** | bearer + `X-Relay-API-Key` against a created key; revoked/expired → 401 with distinct reasons; store-unavailable → 401 fail-closed; bootstrap key still accepted unchanged; `request.state.key_*` populated. |
| **Scopes** | admin routes require `admin`; chat/v1 routes require `chat`/`v1`; empty scopes = full access; bootstrap unaffected. |
| **CLI** | `relay keys create/list/revoke` and `provider set/get/remove` via the argparse entry (`app/cli.py` pattern); create prints raw once; list never prints raw/hash. |
| **Admin API** | `POST/GET /admin/keys`, `DELETE /admin/keys/{id}`; auth enforced; create response carries the raw key exactly once. |
| **Provider keyring** | fake-keyring backend fixture round-trip (get/set/remove); factory and reload pick up the stored key over env; env fallback when unset; `config_store` writes to the backend when enabled. |
| **Redaction** | new `rl_…` shape masked in text and dicts (`test_redaction.py` extension). |
| **Privacy** | no key material in logs, metrics labels, ops events, or exports; `key_id` (opaque) is the only identity recorded. |
| **Regression** | full baseline suite + conformance suite stay green; no wire-contract changes. |

---

## 7. Scope

### 7.1 Files

**New**
- `app/services/key_store.py` — `api_keys` table + scrypt hash/verify + lifecycle (D1/D3).
- `app/services/provider_key_store.py` — keyring backend for provider keys (D2).
- `app/cli/keys.py` — `relay keys` subcommands (D6).
- `app/api/keys.py` — `GET/POST /admin/keys`, `DELETE /admin/keys/{id}`.
- Tests: `tests/test_key_store.py`, `tests/test_key_auth.py`,
  `tests/test_admin_keys.py`, `tests/test_provider_key_store.py`,
  `tests/test_key_cli.py`.

**Modified**
- `app/security/auth.py` — store-backed tier-2 lookup + scopes + `request.state` (D7/D8).
- `app/services/config_store.py` — `api_key` path routed to keyring (3.5).
- `app/services/reload.py` — provider keys re-read from keyring; `rl_` redaction.
- `app/services/redaction.py` — `rl_…` shape patterns.
- `app/providers/factory.py` — provider key resolution order (3.5).
- `app/services/metrics.py` — `record_auth` `key_id` label (D10).
- `app/cli.py` — register `keys` subparser.
- `app/main.py` — register keys router.
- `pyproject.toml`, `requirements.txt`, `requirements-dev.txt` — add `keyring`.
- Docs: `docs/configuration.md`, `docs/deployment.md`, `.env.example`, README (client
  key flow, precedence, headless caveats).

### 7.2 Migrations

- New `relay_keys.db` schema v1 (`api_keys` table) using the `PRAGMA user_version`
  pattern; **no change to `relay_state.db`** (`StateStore` untouched). P6 folds
  `api_keys` into `platform.db` via its migrations framework unchanged.

### 7.3 Risks

- **Keyring availability** on headless Linux/CI/WSL and Windows credential-manager
  prompts. Mitigation: `RELAY_KEYRING_BACKEND` override for tests/headless; deployment
  docs cover backend setup. The roadmap's encrypted-file-with-master-key alternative is
  recorded as a deferred fallback if keyring proves unusable in target environments.
- **Constant-time store lookup** must iterate without an index on `key_hash`; verified by
  construction and pinned by tests.
- **Scope-map drift** as endpoints change; the endpoint→scope map is explicit and
  test-pinned.
- **Wizard persistence change** (writes to keyring, not `.env`) alters user-visible
  behavior; `relay keys provider migrate` is included so users can clear legacy `.env`
  keys on their own schedule.
- **New runtime dependency** (`keyring`, pure-Python) plus an optional OS backend; no
  crypto deps beyond stdlib `hashlib.scrypt`.
- **Implementation order risk**: `security-best-practices` review gate must run before
  implementation (roadmap line 146).

### 7.4 Explicitly out of scope (P5)

- `request_log` table, durable apps view, `relay apps` (P6).
- `model_status` tracking (P6); `platform.db` migrations framework beyond the
  `api_keys` table (P6).
- OpenRouter/Groq provider wiring (P4, keys remain parsed-but-unused).
- TUI keys panel and full `relay config` surface (P7).
