# Connecting Cline to Relay

Cline (a VS Code extension) is an OpenAI-compatible client. This guide sets it
up to talk to Relay instead of a cloud provider.

## What Relay is

Relay is a self-hosted LLM routing proxy with an OpenAI-compatible endpoint.
Cline speaks the OpenAI API, so it can point its "OpenAI Compatible" provider
at Relay's `/v1` and inherit Relay's routing, failover, and health-aware
selection. See the [client guides index](index.md) for the overview.

## Prerequisites

- Relay installed and running (see the [index's shared prerequisites](index.md#shared-prerequisites)).
- At least one provider configured and enabled (`relay setup`).
- A model id from `GET /v1/models` (pick the **exact** id — see [Verify](#verify)).
- Cline installed from the VS Code marketplace.

## Create a key for Cline

Two options (from the [index's authentication section](index.md#authentication)):

Per-app scoped key (recommended):

```bash
# in .env:  RELAY_AUTH_STORE=true
relay keys add --label cline --scopes chat,v1
```

The raw key prints **once**. Copy it into Cline's settings in the next step.

Bootstrap alternative:

```bash
# in .env:  RELAY_API_KEY=<long-random-value>
```

The `.env` file is `%LOCALAPPDATA%\relay\.env` on Windows for an installed
package, or `project-root/.env` for a source checkout (see
[README Configuration](../../README.md#configuration)).

If you left both empty, auth is off and Cline's API Key field can be left
blank — but do not expose that instance beyond localhost.

## Authentication methods

Cline sends the key you paste into its settings as a bearer credential. Relay
accepts either `Authorization: Bearer <key>` or `X-Relay-API-Key: <key>` —
Cline uses the standard bearer form. When `RELAY_API_KEY` is set or
`RELAY_AUTH_STORE=true`, every non-public route requires it; `/` and `/health`
stay public.

## Endpoint configuration

In Cline's settings, choose the **"OpenAI Compatible"** API provider and set:

| Field | Value |
| --- | --- |
| API Provider | `OpenAI Compatible` |
| Base URL | `http://127.0.0.1:8000/v1` |
| API Key | the key from [Create a key](#create-a-key-for-cline) |
| Model ID | a model id from `/v1/models` |

For a remote Relay, replace the host with `https://<your-host>` and keep the
`/v1` suffix. See the [local deployment workflow](index.md#local-deployment-workflow).

## Client-specific configuration steps

1. Keep Relay running.
2. In VS Code, open the Cline panel → its settings (gear icon).
3. Set API Provider to **OpenAI Compatible**.
4. Fill in Base URL, API Key, and Model ID from the table above.
5. Send a message in the Cline chat panel.

## Example request

Cline constructs the same call you can test directly:

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <KEY>" \
  -d '{"model": "<MODEL_ID>", "messages": [{"role": "user", "content": "Hello"}]}'
```

## Verify

- Cline connects without an auth/connection error.
- Messages get responses and streaming appears to stream (text appears
  incrementally in the chat panel).
- The model picker lists the Relay model(s) you configured.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| "invalid API key" | Key mismatch, key not sent, or the key was revoked. Re-run `relay keys add --label cline --scopes chat,v1` and paste the new key; verify with `relay keys test <key>`. |
| "model not found" | Model ID does not match `/v1/models` exactly (case, prefix, slashes). |
| Connection refused | Relay not running, or the Base URL host/port is wrong. Confirm with the curl check above. |
| Requests go to Cline's own default provider | The settings were applied to the wrong provider slot; make sure API Provider is "OpenAI Compatible" and Base URL points at Relay. |
| Streaming text appears all at once | Client/proxy buffering; check Cline's stream settings and that nothing in front of Relay buffers SSE. |

## Security notes

- The key you paste into Cline is stored by Cline — treat it like any
  credential and use a scoped key (`--scopes chat,v1`), not the bootstrap key.
- Rotate/revoke per-client keys with `relay keys rotate <id>` /
  `relay keys remove <id>`; audit with `relay events`.
- Never paste the raw key into a chat message or a shared issue.
- Relay holds the upstream provider keys; you never put those in Cline.

## Related documentation

- [Client setup guides index](index.md)
- [Project continuity](continuity.md) — resume a conversation after a provider switch or Relay restart.
- [Authentication & keys](../security.md)
- [Configuration reference](../configuration.md)
- [Deployment & hardening](../deployment.md)
- [Troubleshooting](../troubleshooting.md)
- [Known limitations](../known-limitations.md)
