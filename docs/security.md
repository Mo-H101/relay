# Relay Security

This document describes Relay's key model, credential precedence,
permissions, redaction contract, and key lifecycle. It is the operational
reference for how secrets are stored, resolved, and protected.

## Key model

Relay distinguishes two kinds of credential:

| Credential | Format | Purpose | Storage |
| --- | --- | --- | --- |
| Relay API key | `rl_` + 43 base62 chars (~256 bits) | Authenticates **clients** to the Relay API (`/v1`, `/chat`, `/admin`, …) | Scrypt hashes in `relay_keys.db`; raw key shown once at creation |
| Provider key | provider-native (`sk-…`, `nvapi-…`, …) | Authenticates Relay to an **upstream** provider | OS keyring (recommended) or `.env` (compatibility fallback) |

### Relay API keys

- The raw key is generated, returned exactly once (by `relay keys add`),
  and **never persisted**. `relay_keys.db` stores only scrypt
  `key_hash`/`key_salt`/`kdf` columns. Verification iterates rows with
  constant-time digest comparison so the database cannot reveal which key
  matched through timing.
- `RELAY_API_KEY` is the **bootstrap** key: it lives in `.env` by design
  and is read on every request so it can be rotated by editing `.env` and
  reloading. It is not stored in `relay_keys.db`.

### Provider keys

- With `RELAY_KEYRING=true`, provider keys are stored in the operating
  system credential store (Windows Credential Manager / macOS Keychain /
  libsecret) under service name `relay`, keyed by provider id.
- Without it, provider keys are written to `.env` for compatibility.
  The provider-key env vars (`NVIDIA_API_KEY`, `OPENAI_API_KEY`, …) are
  **deprecated**: still honored as a fallback, no longer written by the
  tools after migration, and scheduled for removal in P6.

## Credential precedence

Resolution order is fixed and never configurable at runtime:

1. **Bootstrap `RELAY_API_KEY` wins** over every store-backed key when
   both are set. This is the Phase 4 "bootstrap always wins" contract:
   it is readable at every request and is the recovery path if the store
   is unavailable.
2. **Provider keys**: keyring-stored key first (when `RELAY_KEYRING=true`
   and an entry exists), then the `.env`/settings value, then empty.
   A keyring outage degrades to the `.env` fallback rather than failing
   the request.

A store-backed Relay key is accepted only when `RELAY_AUTH_STORE=true`.
A store outage **fails closed** (HTTP 401) so a broken store can never
silently disable authentication. All auth failures return an identical
`401` body; the reason is recorded only in metrics.

## Permissions

- `relay_keys.db` and its SQLite `-wal`/`-shm` sidecars are created
  user-only (`0600`) on POSIX. `.corrupt-*.bak` backups are tightened to
  `0600` as well. Windows relies on the user-profile ACL.
- `.env` is tightened to `0600` after every write on POSIX so provider
  keys never sit at a umask-broad mode.
- The OS keyring delegates encryption to the platform backend
  (`RELAY_KEYRING_BACKEND` can override it); Relay never handles a master
  key.
- The state directory (`.relay`) is created with default permissions and
  should be owned by the service account.

## Keyring backends and headless servers

The `keyring` package picks a backend for the current session. On a
desktop this is the platform credential store. On a **headless** server
there may be no backend or no desktop session:

- `ProviderKeyStore.get` swallows keyring errors and returns `""`
  (degradation, not crash); `set`/`remove` raise.
- For headless production, set `RELAY_KEYRING_BACKEND` to a dotted
  `module.Class` path naming an encrypted, headless-capable backend
  (e.g. a `SecretService` backend with an unlocked keyring) and verify
  `relay provider keys set` / `relay provider keys migrate` succeed.
- The migration command aborts before touching `.env` if a keyring write
  fails, so a keyring outage never leaves a half-migrated environment.

## Redaction contract

Every exported diagnostic, rendered log line, and provider error body is
scrubbed before it can reach a file, the TUI, or a caller. The redaction
layer (`app/services/redaction.py`) masks:

- sensitive **key names** (`api_key`, `token`, `password`, `authorization`,
  `x-relay-api-key`, `credential`, …) and their values;
- known secret **value shapes**: `sk-…`, `nvapi-…`, and the Relay `rl_…`
  key format (43 base62 chars), anywhere in the text;
- `Authorization`/`X-Relay-API-Key` header values as a whole;
- provider error bodies additionally strip the provider's own key
  (`safe_error_body`) before truncation.

The database, metrics, and ops events never contain prompts, responses,
API keys, proxy credentials, or correlation ids; the opaque `key_id`
(uuid) is the only identity Relay's observability surfaces carry.

## Key lifecycle and rotation

- Lifecycle: `create` → `active` → `expired` (evaluated at verify time) /
  `revoked` (soft) → permanent `delete`. `last_used_at` is recorded on
  successful store-backed auth.
- `relay keys add` prints the raw key exactly once. `relay keys remove`
  revokes. `/admin/keys` covers the full operator surface (create, list,
  inspect, revoke, permanent delete).
- **Rotation**: `KeyStore.rotate` exists internally but is not yet
  exposed by the CLI or `/admin/keys` (a P6 API candidate). Today,
  rotate a Relay key by creating a new key and revoking the old one.
- Provider-key rotation: `relay provider keys set <id>` writes the new
  value to the keyring (or `.env` with keyring off); the migration
  command is idempotent and safe to re-run after a rotation.

## Migrating provider keys out of `.env`

`relay provider keys migrate` moves each cloud provider's key from `.env`
into the OS keyring, then removes it from `.env`. It never prints a
secret, is dry-run safe, aborts before `.env` removal on a keyring write
failure, and treats conflicts conservatively (skip unless `--force`).
After migration, set `RELAY_KEYRING=true` so runtime resolution reads the
keyring. See the [deployment runbook](deployment.md) for steps and
rollback.

## Incident notes

- If a Relay API key leaks, revoke it (`relay keys remove <id>` or
  `/admin/keys`) and create a replacement. Store hashes cannot be
  recovered into raw keys, so revocation is the only mitigation.
- If a provider key leaks, rotate it at the provider and update Relay
  with `relay provider keys set`.
- Bootstrapping `RELAY_API_KEY`: rotate by editing `.env` and reloading.
  Moving it into a vault-adjacent store is a P6 item.
