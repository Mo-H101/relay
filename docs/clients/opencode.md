# Connecting OpenCode to Relay

OpenCode is a terminal-based AI coding agent. It connects to providers through
the Vercel AI SDK, so adding Relay is a matter of registering an
OpenAI-compatible provider in OpenCode's config.

## What Relay is

Relay is a self-hosted LLM routing proxy with an OpenAI-compatible endpoint.
OpenCode can use any OpenAI-compatible provider via `@ai-sdk/openai-compatible`;
pointing that provider at Relay's `/v1` gives OpenCode Relay's routing,
failover, and health-aware selection. See the [client guides index](index.md)
for the overview.

## Prerequisites

- Relay installed and running (see the [index's shared prerequisites](index.md#shared-prerequisites)).
- At least one provider configured and enabled (`relay setup`).
- A model id from `GET /v1/models` (pick the **exact** id — see [Verify](#verify)).
- OpenCode installed (`opencode` on your PATH).

## Create a key for OpenCode

Two options (from the [index's authentication section](index.md#authentication)):

Per-app scoped key (recommended):

```bash
# in .env:  RELAY_AUTH_STORE=true
relay keys add --label opencode --scopes chat,v1
```

The raw key prints **once**. Put it in the config in the next step.

Bootstrap alternative:

```bash
# in .env:  RELAY_API_KEY=<long-random-value>
```

If both are empty, auth is off and `apiKey` can be any placeholder value
(OpenCode/AI SDK still expect a non-empty string) — but do not expose that
instance beyond localhost.

## Authentication methods

OpenCode sends the configured `apiKey` as a bearer credential. Relay accepts
either `Authorization: Bearer <key>` or `X-Relay-API-Key: <key>`. When
`RELAY_API_KEY` is set or `RELAY_AUTH_STORE=true`, every non-public route
requires it; `/` and `/health` stay public.

## Endpoint configuration

The provider config lives in your project's `.opencode/config.json` (or a
top-level `opencode.json`). Register Relay as an OpenAI-compatible provider:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "relay": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Relay",
      "options": {
        "baseURL": "http://localhost:8000/v1",
        "apiKey": "<KEY>"
      },
      "models": {
        "<MODEL_ID>": { "name": "Relay <model>" }
      }
    }
  }
}
```

For a remote Relay, set `baseURL` to `https://<your-host>/v1` (keep the `/v1`
suffix). See the [local deployment workflow](index.md#local-deployment-workflow).

## Client-specific configuration steps

1. Keep Relay running.
2. Add the `relay` provider block above to `.opencode/config.json` (or
   `opencode.json`), replacing `<KEY>` and `<MODEL_ID>`.
3. In OpenCode, open the model picker (`/models` command) and select the
   **Relay** provider and a Relay model.
4. Ask a coding question and let it use tools (read a file, run a command) to
   confirm tool calling round-trips through Relay.

## Example request

The equivalent direct call (what OpenCode sends under the hood):

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <KEY>" \
  -d '{"model": "<MODEL_ID>", "messages": [{"role": "user", "content": "Hello"}]}'
```

## Verify

- OpenCode lists the **Relay** provider and its model(s).
- Chat works and responses stream.
- Tool calls round-trip: the model emits tool calls, OpenCode executes them,
  and results come back through Relay.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Provider not listed | Config schema error — check `$schema` and the field names against the example. Run `opencode --version` and confirm the AI SDK package name. |
| Model id mismatch errors | `<MODEL_ID>` must match `/v1/models` exactly. |
| Auth `401` | Missing or incorrect `apiKey`; key revoked. Verify with `relay keys test <key>`. |
| Tool calls fail or hang through Relay | Confirm the model supports tool calling and the id is exact; check Relay logs for the failed call (`relay events`, server log). |
| Relay receives nothing at all | Wrong `baseURL` or the wrong config file is being read (project vs. global). |
| Requests go to another provider | The client defaulted to a different provider; select Relay explicitly with `/models`. |

## Security notes

- The `apiKey` in your OpenCode config is plaintext on disk — prefer a scoped
  key (`--scopes chat,v1`) and keep the file out of shared repos (`.gitignore`
  it if you commit the config).
- Rotate/revoke per-client keys with `relay keys rotate <id>` /
  `relay keys remove <id>`; audit with `relay events`.
- Never paste the raw key into a chat message or a shared issue.
- Relay holds the upstream provider keys; you never put those in OpenCode.

## Related documentation

- [Client setup guides index](index.md)
- [Authentication & keys](../security.md)
- [Configuration reference](../configuration.md)
- [Deployment & hardening](../deployment.md)
- [Troubleshooting](../troubleshooting.md)
- [Known limitations](../known-limitations.md)
