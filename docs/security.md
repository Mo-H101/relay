# Relay Security

This document describes Relay's key model, credential precedence,
permissions, redaction contract, and key lifecycle. It is the operational
reference for how secrets are stored, resolved, and protected.

## Key model

Relay distinguishes two kinds of credential:

| Credential | Format | Purpose | Storage |
| --- | --- | --- | --- |
| Relay API key | `rl_` + 43 base62 chars (~256 bits) | Authenticates **clients** to the Relay API (`/v1`, `/chat`, `/admin`, …) | Scrypt hashes in `platform.db`; raw key shown once at creation |
| Provider key | provider-native (`sk-…`, `nvapi-…`, …) | Authenticates Relay to an **upstream** provider | OS keyring (recommended) or `.env` (compatibility fallback) |

### Relay API keys

- The raw key is generated, returned exactly once (by `relay keys add`),
  and **never persisted**. `platform.db` stores only scrypt
  `key_hash`/`key_salt`/`kdf` columns. Verification iterates rows with
  constant-time digest comparison so the database cannot reveal which key
  matched through timing.
- `RELAY_API_KEY` is the **bootstrap** key: it lives in `.env` by design
  and is read on every request so it can be rotated by editing `.env` and
  reloading. It is not stored in `platform.db`.

### Provider keys

- With `RELAY_KEYRING=true`, provider keys are stored in the operating
  system credential store (Windows Credential Manager / macOS Keychain /
  libsecret) under service name `relay`, keyed by provider id.
- Without it, provider keys are written to `.env` for compatibility.
  The provider-key env vars (`NVIDIA_API_KEY`, `OPENAI_API_KEY`, …) are
  **deprecated but still honored**: they remain the runtime fallback when
  the keyring is disabled or holds no entry; removal is deferred beyond
  the P6 scope.

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

- `platform.db` and its SQLite `-wal`/`-shm` sidecars are created
  user-only (`0600`) on POSIX. `.corrupt-*.bak` backups are tightened to
  `0600` as well. Windows relies on the user-profile ACL.
- `.env` is tightened to `0600` after every write on POSIX so provider
  keys never sit at a umask-broad mode.
- The OS keyring delegates encryption to the platform backend
  (`RELAY_KEYRING_BACKEND` can override it); Relay never handles a master
  key.
- The state directory (`.relay`) is created with default permissions and
  should be owned by the service account.

### Windows-specific security behavior

Windows does not map POSIX permission bits, so the `0600` guarantees
above do not exist there. The equivalent protections are provided by the
**user-profile ACL** and by where Relay puts files:

- `platform.db`, its `-wal`/`-shm` sidecars, `.corrupt-*.bak` backups,
  and `.env` are created under the invoking user's profile/working
  directory and are not world-readable by default. Relay does **not**
  force ACL changes on these files on Windows.
- The permission-mode tests (`mode == 0o600` checks in the test suite)
  are **skipped on Windows** and only execute on POSIX (Linux CI). A
  Windows deploy should rely on profile ACLs and, in shared-user or
  service contexts, tighten ACLs at the directory level so only the
  service account can read `state_dir` and `.env`.
- The **keyring requirement** on Windows is the OS Credential Manager,
  which the `keyring` package uses automatically for the interactive
  user. Two caveats:
  - Running Relay as a Windows service under a virtual account may have
    **no accessible credential store**; verify with `relay provider keys
    set` / `relay provider keys migrate`, or point
    `RELAY_KEYRING_BACKEND` at an encrypted, headless-capable backend.
  - When the keyring is unavailable or `RELAY_KEYRING=false`, provider
    keys fall back to `.env` (plaintext on disk) — see the at-rest
    section below.

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

## At-rest protection of provider secrets (deployment requirement)

Relay has **no encryption-at-rest subsystem of its own.** Provider keys
are protected at rest in exactly one way: the **OS keyring**, which
encrypts its own material and which Relay never holds a master key for.

What this means in practice:

- **With `RELAY_KEYRING=true` and a working backend**, provider keys are
  stored in the encrypted OS credential store (Windows Credential
  Manager / macOS Keychain / libsecret) and never written to disk in
  plaintext.
- **Without a keyring** (`RELAY_KEYRING=false`, or a backend that is
  unavailable), provider keys fall back to the `.env` file, which is
  **plaintext on disk** (permission-tightened to `0600` on POSIX only).
  The runtime also treats an empty keyring read as "no stored key" and
  degrades to the `.env` value rather than failing the request.

There is intentionally **no Relay-side key-encryption key** that would
encrypt provider keys when the keyring is absent: that design was
evaluated (P6.4) and rejected to avoid a new at-rest crypto subsystem
whose master key would itself need a safe home. The consequence is a
**deployment requirement**, not a code fix:

> Run Relay with `RELAY_KEYRING=true` and an encrypted, working keyring
> backend. If that is not possible, protect the `.env` file at rest —
> full-disk encryption, directory ACLs limited to the service account,
> and rotating provider keys promptly — and treat `.env` as the
> secret it contains.

The migration runbook (`relay provider keys migrate`) is the supported
path out of plaintext `.env` storage; see the [deployment
runbook](deployment.md) for steps and rollback.

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
  revokes. `relay keys rotate <key_id>` (or `POST /admin/keys/{id}/rotate`)
  replaces a key with a fresh one: the new raw key is returned exactly
  once, then the original is revoked in the same operation. Rotation
  preserves the key's label and scopes. Non-interactive CLI runs require
  `--yes`; a revoked key cannot be rotated (`409` from the API).
- `/admin/keys` covers the full operator surface (create, list, inspect,
  revoke, rotate, permanent delete). List entries carry `expires_soon` so
  operators can spot keys at risk (within `_EXPIRY_WINDOW_DAYS` = 7 days of
  expiry); the CLI prints an `exp` marker in `relay keys list`.
- **Rotation runbook** (Relay keys): create the replacement (`relay keys
  rotate <key_id>` or `/admin/keys/{id}/rotate`), migrate clients to the
  new key, then keep the old key revoked-but-present until the grace window
  passes. A short overlap is safe because both old and new keys are valid
  during the swap. Purge the revoked rows once they are terminal and past
  the grace window (`relay keys prune --yes`).
- Provider-key rotation: `relay provider keys set <id>` writes the new
  value to the keyring (or `.env` with keyring off); the migration
  command is idempotent and safe to re-run after a rotation. Writes are
  refused non-interactively without `--yes`.

## Pruning terminal keys

`relay keys prune` deletes **terminal rows only**: keys that were revoked,
or expired keys past their `expires_at`. Rows still active are never
touched. A grace window keeps rows that became terminal recently so an
operator can still inspect them.

- `relay keys prune` — dry run by default: lists what would be removed
  and changes nothing.
- `--older-than-days N` — prune keys that have been terminal longer than
  N days (default 30, matching the internal `_PRUNE_GRACE_DAYS`).
- `--yes` — execute the prune; without it a non-interactive run is a dry
  run.
- `--json` — machine-readable output (dry-run or executed).
- `relay migrate` runs the same prune automatically after import and
  records a `key.prune` event; a purge failure never fails the migration.

## Security event log

Relay records a durable, append-only security event log in the `events`
table of `state_dir/platform.db` (schema v5). Rows carry `ts`, `action`,
`outcome`, `actor` (`cli`, `api`, `bootstrap`, …), `target` (an opaque key
id), and a small `detail` map. Example actions: `key.create`, `key.revoke`,
`key.rotate`, `key.prune`, `auth.failure`, `auth.success`,
`provider_key.set`, `provider_key.migrate`, `migrate.run`.

- Rows are **redacted at write time**: no raw keys, prompts, responses,
  or correlation ids are ever stored, so the log is safe to tail and
  export.
- Read surfaces: `relay events [--action …] [--outcome …] [--limit N]
  [--json]` (newest first) and `GET /admin/events?action=&outcome=&limit=`
  (admin scope, bounded).
- Write semantics: the hot path (auth) is **best-effort** — an event-log
  failure is recorded in metrics and never breaks a request; admin and
  operator paths are **fail-visible** so a broken audit log cannot hide a
  failed security action.
- Retention: `events` rows older than `PERSISTENCE_RETENTION_DAYS` are
  pruned on the flusher's retention tick (default `0` = disabled).

## Incident notes

- If a Relay API key leaks, revoke it (`relay keys remove <id>` or
  `/admin/keys`) and rotate a replacement (`relay keys rotate <id>`).
  Store hashes cannot be recovered into raw keys, so revocation is the
  only mitigation.
- If a provider key leaks, rotate it at the provider and update Relay
  with `relay provider keys set`.
- Bootstrapping `RELAY_API_KEY`: rotate by editing `.env` and reloading.
  Moving it into a vault-adjacent store is a P6 item.
- Audit: use `relay events` or `/admin/events` to trace key lifecycle and
  auth outcomes after an incident; the redaction contract guarantees the
  log holds no recoverable secrets.

## Migrating provider keys out of `.env`

`relay provider keys migrate` moves each cloud provider's key from `.env`
into the OS keyring, then removes it from `.env`. It never prints a
secret, is dry-run safe, aborts before `.env` removal on a keyring write
failure, and treats conflicts conservatively (skip unless `--force`).
After migration, set `RELAY_KEYRING=true` so runtime resolution reads the
keyring. A keyring-only install is detected as configured by the setup
wizard, so first-run setup is not re-launched. See the [deployment
runbook](deployment.md) for steps and rollback.
