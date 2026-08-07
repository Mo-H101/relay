# Connecting Continue to Relay

Continue is an AI coding assistant extension for VS Code and JetBrains. Its
`openai` provider is OpenAI-compatible, so pointing `apiBase` at Relay's `/v1`
routes Continue's chat, edit, and apply through Relay.

## What Relay is

Relay is a self-hosted LLM routing proxy with an OpenAI-compatible endpoint.
Continue speaks the OpenAI API; setting `apiBase` to Relay's `/v1` gives
Continue Relay's routing, failover, and health-aware selection. See the
[client guides index](index.md) for the overview.

## Prerequisites

- Relay installed and running (see the [index's shared prerequisites](index.md#shared-prerequisites)).
- At least one provider configured and enabled (`relay setup`).
- A model id from `GET /v1/models` (pick the **exact** id — see [Verify](#verify)).
- Continue installed from the marketplace for your editor (VS Code or JetBrains).

## Create a key for Continue

Two options (from the [index's authentication section](index.md#authentication)):

Per-app scoped key (recommended):

```bash
# in .env:  RELAY_AUTH_STORE=true
relay keys add --label continue --scopes chat,v1
```

The raw key prints **once**. Put it in the config in the next step.

Bootstrap alternative:

```bash
# in .env:  RELAY_API_KEY=<long-random-value>
```

The `.env` file is `%LOCALAPPDATA%\relay\.env` on Windows for an installed
package, or `project-root/.env` for a source checkout (see
[README Configuration](../../README.md#configuration)).

If both are empty, auth is off and `apiKey` can be left out — but do not
expose that instance beyond localhost.

## Authentication methods

Continue sends the configured `apiKey` with each request. Relay accepts either
`Authorization: Bearer <key>` or `X-Relay-API-Key: <key>`. When `RELAY_API_KEY`
is set or `RELAY_AUTH_STORE=true`, every non-public route requires it; `/` and
`/health` stay public.

## Endpoint configuration

Continue reads `config.yaml` from its global directory — `~/.continue/` on
macOS/Linux, `%USERPROFILE%\.continue` on Windows. (A `config.yaml` present is
loaded instead of the deprecated `config.json`; Continue's docs call
`config.json` deprecated.) Add a Relay model:

```yaml
name: My Config
version: 0.0.1
schema: v1

models:
  - name: Relay
    provider: openai
    model: <MODEL_ID>
    apiBase: http://127.0.0.1:8000/v1
    apiKey: <KEY>
    roles:
      - chat
      - edit
      - apply
```

For a remote Relay, set `apiBase` to `https://<your-host>/v1` (keep the `/v1`
suffix). See the [local deployment workflow](index.md#local-deployment-workflow).

## Client-specific configuration steps

1. Keep Relay running.
2. Open `~/.continue/config.yaml` (create it if missing) and add the model
   block above, replacing `<KEY>` and `<MODEL_ID>`.
3. Reload Continue (or restart the extension).
4. Use the model selector in the Continue panel to pick **Relay**.
5. Ask a question in the Continue chat panel; test **Edit** / **Apply** to
   confirm non-chat roles work.

## Example request

The equivalent direct call (what Continue sends under the hood):

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <KEY>" \
  -d '{"model": "<MODEL_ID>", "messages": [{"role": "user", "content": "Hello"}]}'
```

## Verify

- Continue lists **Relay** in its model selector.
- Chat responses work and stream.
- Edit/apply operations succeed (they use the same `/v1` endpoint).

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Model not listed in Continue | Config file not read (check the path `~/.continue/config.yaml` / `%USERPROFILE%\.continue`), YAML indentation, or a model selector filter hiding it. Reload/restart the extension after editing. |
| Auth `401` | Missing or incorrect `apiKey`; key revoked. Verify with `relay keys test <key>`. |
| "model not found" | `<MODEL_ID>` does not match `/v1/models` exactly. |
| Connection refused | `apiBase` host/port wrong, or Relay not running. Confirm with the curl check above. |
| Requests go to another provider | Another model entry is selected; pick the Relay model explicitly. |
| Legacy `completions` vs `chat/completions` | Relay implements `/chat/completions`. If Continue forces the legacy endpoint, remove `useLegacyCompletionsEndpoint: true` if you set it. |

## Security notes

- The `apiKey` sits in your Continue config on disk — prefer a scoped key
  (`--scopes chat,v1`) and protect the file (it is in your home directory by
  default).
- Rotate/revoke per-client keys with `relay keys rotate <id>` /
  `relay keys remove <id>`; audit with `relay events`.
- Never paste the raw key into a chat message or a shared issue.
- Relay holds the upstream provider keys; you never put those in Continue.

## Related documentation

- [Client setup guides index](index.md)
- [Authentication & keys](../security.md)
- [Configuration reference](../configuration.md)
- [Deployment & hardening](../deployment.md)
- [Troubleshooting](../troubleshooting.md)
- [Known limitations](../known-limitations.md)
