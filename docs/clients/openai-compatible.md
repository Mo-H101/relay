# Connecting Any OpenAI-Compatible Client to Relay

This guide covers the generic OpenAI-compatible contract every client needs:
the base URL, the API key, model ids, and the response shape. Use it for the
OpenAI SDK (Python/Node), curl, scripts, and any other tool that speaks the
OpenAI API and is not covered by a dedicated guide
([Cline](cline.md), [OpenCode](opencode.md), [Continue](continue.md)).

## What Relay is

Relay is a self-hosted LLM routing proxy that exposes an OpenAI-compatible
endpoint. Any OpenAI-compatible client works unchanged: set `base_url` /
`baseURL` to Relay's `/v1` and keep your existing request bodies. Relay picks
the best provider/model, fails over when one is unavailable, and learns from
request outcomes. See the [client guides index](index.md) for the overview.

## Prerequisites

- Relay installed and running (see the [index's shared prerequisites](index.md#shared-prerequisites)).
- At least one provider configured and enabled (`relay setup`).
- A model id from `GET /v1/models` (pick the **exact** id — see [Verify](#verify)).
- Your client's own OpenAI-compatible configuration surface (SDK init, env
  vars, or a settings UI).

## Create a key

Two options (from the [index's authentication section](index.md#authentication)):

Per-app scoped key (recommended):

```bash
# in .env:  RELAY_AUTH_STORE=true
relay keys add --label my-client --scopes chat,v1
```

The raw key prints **once**.

Bootstrap alternative:

```bash
# in .env:  RELAY_API_KEY=<long-random-value>
```

The `.env` file is `%LOCALAPPDATA%\relay\.env` on Windows for an installed
package, or `project-root/.env` for a source checkout (see
[README Configuration](../../README.md#configuration)).

If both are empty, auth is off — the SDK still expects an `api_key` argument
(use any placeholder), but do not expose that instance beyond localhost.

## Authentication methods

Relay accepts the key in either of two header forms:

```
Authorization: Bearer <key>
X-Relay-API-Key: <key>
```

The OpenAI SDKs send `Authorization: Bearer` automatically when you pass
`api_key`. When `RELAY_API_KEY` is set or `RELAY_AUTH_STORE=true`, every
non-public route requires a valid key; `/` and `/health` stay public. Store
keys enforce scopes: a `chat,v1` key works on `/v1/*` and `/chat` but is
rejected with `403` on `/admin/*`.

## Endpoint configuration

Set your client's base URL to Relay's `/v1`:

- Local Relay: `http://127.0.0.1:8000/v1`
- Remote Relay: `https://<your-host>/v1`

The `/v1` suffix is part of the contract — OpenAI SDKs append the paths
(`/chat/completions`, `/models`) to the base URL, so the base must end in
`/v1`.

## Example configurations

Python (OpenAI SDK):

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="<KEY>",
)

resp = client.chat.completions.create(
    model="<MODEL_ID>",
    messages=[{"role": "user", "content": "Hello"}],
)
print(resp.choices[0].message.content)
```

Node (OpenAI SDK):

```js
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://127.0.0.1:8000/v1",
  apiKey: "<KEY>",
});

const resp = await client.chat.completions.create({
  model: "<MODEL_ID>",
  messages: [{ role: "user", content: "Hello" }],
});
console.log(resp.choices[0].message.content);
```

curl:

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <KEY>" \
  -d '{"model": "<MODEL_ID>", "messages": [{"role": "user", "content": "Hello"}]}'
```

## Streaming and passthrough

- `stream: true` is supported and returns OpenAI-compatible SSE
  (`data: {...}\n\n`, terminated by `data: [DONE]`).
- `messages`, `tools`, `tool_choice`, and `stream_options` are forwarded
  verbatim, so tool calling and usage reporting work unchanged.
- Streamed responses carry one stable Relay-generated `id` for the whole
  stream.
- Errors use the OpenAI shape: `{"error": {...}}` (never FastAPI's
  `{"detail": ...}`).

## Verify

```bash
# 1. Model list — pick an exact id
curl -s http://127.0.0.1:8000/v1/models -H "Authorization: Bearer <KEY>"

# 2. One chat round trip
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <KEY>" \
  -d '{"model": "<MODEL_ID>", "messages": [{"role": "user", "content": "Hello"}]}'
```

Success = a `200` with an OpenAI-shaped choices body. Any non-2xx returns the
`{"error": {...}}` shape with a bounded, key-redacted provider message.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `401 Unauthorized` | Key missing/mismatched; header form not accepted; key revoked. Verify with `relay keys test <key>`. |
| `400` "model not found" | Model id must match `/v1/models` exactly. |
| `403 Forbidden` | Store key without the required scope (e.g. calling `/admin` with a `chat,v1` key). |
| `502` with a `provider_error` | Upstream provider failed or hit a quota; Relay has already retried/failed over. Check `/diagnostics` and the provider config. |
| Connection refused | Wrong host/port, or Relay not running / bound to `127.0.0.1` while you are remote. |
| Streaming broken / buffered | Client or proxy buffering; ensure nothing in front of Relay buffers SSE. |
| `429` from the provider | Upstream rate limit — Relay retries immediately by default; enable `RETRY_HONOR_RETRY_AFTER` for polite retries ([docs/known-limitations.md](../known-limitations.md#1-429-retry-after-is-honored-only-when-explicitly-enabled)). |

## Security notes

- Use a scoped store-backed key (`--scopes chat,v1`) rather than the shared
  bootstrap key wherever the client stores its own credential.
- SDK `api_key` values live in your code/config — never commit real keys.
  Use env vars or the client's secret store.
- Rotate/revoke per-client keys with `relay keys rotate <id>` /
  `relay keys remove <id>`; audit with `relay events`.
- Relay never stores prompts, responses, or key material
  ([docs/security.md](../security.md#redaction-contract)).
- Keep the upstream provider keys out of the client entirely — only Relay
  needs them ([docs/deployment.md](../deployment.md)).

## Related documentation

- [Client setup guides index](index.md)
- [Project continuity](continuity.md) — resume a conversation after a provider switch or Relay restart.
- [Authentication & keys](../security.md)
- [Configuration reference](../configuration.md)
- [Deployment & hardening](../deployment.md)
- [Troubleshooting](../troubleshooting.md)
- [Known limitations](../known-limitations.md)
