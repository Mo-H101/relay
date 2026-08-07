# Relay — Client Setup Guides

This index is the entry point for connecting any OpenAI-compatible client to
Relay. Pick your client below; every guide follows the same shape: what Relay
is, prerequisites, key creation, authentication, endpoint configuration, a
verify step, troubleshooting, and security notes.

## What Relay is

Relay is a self-hosted LLM routing proxy. It sits between a client and one or
more LLM providers (NVIDIA NIM, OpenAI, local LM Studio, Ollama, …), picks the
best provider/model for each request, fails over across models and providers
when one is unavailable, and learns from real request outcomes to keep routing
smart. It exposes an **OpenAI-compatible endpoint** (`/v1/chat/completions`,
`/v1/models`), so any tool that speaks the OpenAI API can point at Relay's
`/v1` instead of a cloud provider and inherit its routing and failover.

See [docs/architecture.md](../architecture.md) and
[docs/platform-architecture-report.md](../platform-architecture-report.md)
for the full design.

## Which guide do you need?

| Client | Guide |
| --- | --- |
| Cline (VS Code extension) | [Cline setup](cline.md) |
| OpenCode (terminal AI agent) | [OpenCode setup](opencode.md) |
| Continue (VS Code / JetBrains extension) | [Continue setup](continue.md) |
| Any other OpenAI-compatible client (OpenAI SDK, curl, custom tools) | [Generic OpenAI-compatible setup](openai-compatible.md) |

If your client is not listed, the generic guide covers the contract any
OpenAI-compatible client needs (`base_url`, `api_key`, model ids) plus
curl/OpenAI SDK examples.

## Shared prerequisites

1. **Install Relay** and make sure the `relay` command is on your PATH
   ([README installation section](../../README.md#installation)):
   ```bash
   pip install git+https://github.com/<org>/<repo>.git
   # or, from a checkout: .\install.cmd   (Windows) / ./install.sh (macOS/Linux)
   ```
2. **Configure at least one provider** with credentials (interactive wizard):
   ```bash
   relay setup
   ```
   or set provider keys in `.env` (see
   [docs/configuration.md](../configuration.md) for every variable). For
   local models (LM Studio / Ollama), see the
   [local models walkthrough](../local-models.md).
3. **Start Relay** so clients can reach it:
   ```bash
   # Terminal interface with an embedded API server (client-friendly):
   relay
   # Headless API server only:
   relay serve
   ```
   Defaults are `127.0.0.1:8000` (`RELAY_HOST` / `RELAY_PORT` in
   [docs/configuration.md](../configuration.md#server-and-installation-state)).
   While the TUI is open, clients can point at `http://127.0.0.1:8000/v1`.
4. **Confirm `/v1` responds** before configuring a client:
   ```bash
   curl -s http://127.0.0.1:8000/v1/models
   ```
   With auth enabled (`RELAY_API_KEY` set or `RELAY_AUTH_STORE=true`), add an
   auth header — see [Authentication](#authentication) below.

## Local deployment workflow

- **Same machine as Relay:** the default `http://127.0.0.1:8000/v1` works
  as-is. The client and Relay share the loopback interface; no firewall or
  host changes are needed.
- **Another machine on the same LAN:** start Relay bound to all interfaces
  (set `RELAY_HOST=0.0.0.0` then `relay serve`), and point the
  client at `http://<relay-host-ip>:8000/v1`. Allow port 8000 through the
  Relay machine's firewall.
- **Remote / container / internet:** terminate TLS at a reverse proxy in
  front of Relay (Relay serves plain HTTP by default), expose only the proxy,
  and point clients at `https://<your-host>/v1`. Keep `RELAY_API_KEY` set on
  the public instance. See [docs/deployment.md](../deployment.md) for the
  reverse-proxy and hardening notes.
- **Run exactly one Relay process.** Learned state lives in SQLite, which is
  single-writer; do not run `uvicorn --workers N` against one database (a
  documented corruption risk). Scale by running isolated instances with their
  own `PERSISTENCE_PATH`. See [docs/deployment.md](../deployment.md).

## Authentication

Relay has two tiers of API-key authentication (both documented in
[docs/security.md](../security.md)):

- **Tier 1 — bootstrap key:** set `RELAY_API_KEY=<long-random-value>` in
  `.env`. Every non-public route then requires the key.
- **Tier 2 — store-backed per-app keys:** set `RELAY_AUTH_STORE=true`, then
  create keys with the CLI. Store keys are scrypt-hashed in `platform.db`
  and support scopes and expiry.

The `.env` file is `%LOCALAPPDATA%\relay\.env` on Windows for an installed
package, or `project-root/.env` for a source checkout (see
[README Configuration](../../README.md#configuration) for the per-OS
locations).

The client sends the key in one of two forms (either works):

```
Authorization: Bearer <key>
X-Relay-API-Key: <key>
```

`/` and `/health` are always public. Everything else — including
`/v1/chat/completions`, `/v1/models`, `/docs`, and `/admin/*` — requires a
valid key once auth is on. The bootstrap key always wins and has full access;
a store-backed key's scopes gate what it can do.

> **Security warning:** with `RELAY_API_KEY` empty **and** `RELAY_AUTH_STORE`
> unset, authentication is **off**. Do not expose an unauthenticated instance
> to anything beyond your own machine.

### Creating a key for a client

Per-app scoped key (recommended — the client gets only chat access):

```bash
# in .env:  RELAY_AUTH_STORE=true
relay keys add --label opencode --scopes chat,v1
```

The raw key is printed **exactly once** — save it into the client's config
immediately. It will not be shown again. Metadata-only views:

```bash
relay keys list               # metadata only, never the raw key
relay keys test <key>         # verifies a key without echoing it
```

Bootstrap alternative (simplest, shared by all clients):

```bash
# in .env:  RELAY_API_KEY=<long-random-value>
```

### Key lifecycle

- Rotate a key: `relay keys rotate <key_id>`
- Revoke a key: `relay keys remove <key_id>`
- Prune terminal keys: `relay keys prune --yes`
- Audit log: `relay events` (tails `key.create` / `key.rotate` / `key.prune`
  / `auth.failure` / …)

Full runbooks in [docs/deployment.md](../deployment.md) and
[docs/security.md](../security.md).

## Verify a client connection

The generic check that every client performs under the hood:

```bash
# 1. Model list (auth on → add the header)
curl -s http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer <KEY>"

# 2. One chat round trip
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <KEY>" \
  -d '{"model": "<MODEL_ID>", "messages": [{"role": "user", "content": "Hello"}]}'
```

Pick `<MODEL_ID>` from the `/v1/models` output — the client must use the
**exact** id Relay lists.

## Common failures (at a glance)

| Symptom | Cause / fix |
| --- | --- |
| `401 Unauthorized` | Key missing/mismatched in the client config; wrong header form; key revoked. See [docs/troubleshooting.md](../troubleshooting.md#authentication-problems). |
| `400 model not found` | Model id does not match `/v1/models` exactly (case, prefix, slashes). |
| Connection refused | Wrong host/port, or Relay not running / bound to `127.0.0.1` while the client is remote. |
| Client falls back to its own default provider | The client's config landed in the wrong provider slot, or the wrong config file is being read. |
| Streaming text arrives all at once | Client-side or proxy buffering; check the client's stream settings. |
| `429` from the provider | Upstream rate limit; see [docs/known-limitations.md](../known-limitations.md#1-429-retry-after-is-honored-only-when-explicitly-enabled). |
| Tool calls fail/hang | Verify the model actually supports tool calling; check the exact model id and the client's tools config. |

See each client guide for client-specific troubleshooting, and
[docs/troubleshooting.md](../troubleshooting.md) for the general checklist.

## Security at a glance

- Never paste a real key into a chat message, log, issue, or shared config.
- Prefer a scoped store-backed key (`--scopes chat,v1`) over sharing the
  bootstrap key with every tool.
- The bootstrap key always wins; keep it private to Relay's operator.
- Provider keys (upstream credentials) never need to be in the client at all —
  Relay holds them. Move them out of `.env` into the OS keyring with
  `relay provider keys migrate` ([docs/deployment.md](../deployment.md)).
- Relay never stores prompts, responses, or key material in its database
  ([docs/security.md](../security.md#redaction-contract)).

## More Relay documentation

- [Configuration reference](../configuration.md) — every environment variable.
- [Security model](../security.md) — key model, precedence, redaction, lifecycle.
- [Deployment](../deployment.md) — production hardening, persistence, proxies.
- [Troubleshooting](../troubleshooting.md) — common problems and diagnostics.
- [Known limitations](../known-limitations.md) — validated behavior to know before production.
- [Terminal interface guide](../tui-guide.md) — the TUI's screens and startup.
- [UX validation guide](../ux-validation-guide.md) — manual end-to-end checks.
