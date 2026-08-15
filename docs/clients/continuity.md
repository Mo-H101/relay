# Relay project continuity

Project continuity lets an OpenAI-compatible client resume a conversation
after a provider switch **or a Relay restart** without re-executing
acknowledged work. It is **opt-in and disabled by default**; when enabled,
only conversations that explicitly ask for it are tracked.

> Every guide in this index uses the same wire contract. This page documents
> that contract for any client (Cline, OpenCode, Continue, a custom script,
> an OpenAI SDK tool). See [the client guides index](index.md) for per-client
> setup.

## What continuity provides

Relay is a routing proxy with failover. Without continuity, a request that
fails over — or a Relay restart — starts the conversation from scratch:
the provider has no memory of earlier turns, so work is repeated.

With continuity, an opt-in conversation is tracked durably in `platform.db`
(metadata only). On each request Relay hands the client a **one-time resume
token**; when the client presents that token on its next request, Relay:

1. validates the token against the durable per-conversation state,
2. injects a continuity envelope (data-marked metadata about prior work)
   as a leading system message,
3. continues the conversation's sequence at exactly `last_seq + 1`, so
   already-acknowledged turns are never re-executed,
4. hands back a **fresh** one-time resume token for the next turn.

The token survives Relay restarts (it is persisted as a SHA-256 hash), so a
client can resume even after the server process was killed.

## Prerequisites

- **Continuity enabled**: set `CONTINUITY_ENABLED=true` in `.env` and
  restart Relay. This is a restart-required setting (see
  [docs/configuration.md](../configuration.md#project-continuity-opt-in-p9)).
- **Store-backed key**: continuity requires a key created with
  `relay keys add` (the scrypt-hashed store-backed tier). Bootstrap keys
  and unauthenticated requests never get continuity.
- The client can send the three continuity headers (below) and read the
  two response headers.

## The wire contract

### Request headers (per request)

| Header | Example | Meaning |
| --- | --- | --- |
| `X-Relay-Conversation-Id` | `5f6a…` (32 hex chars) | The conversation id. Use the one Relay echoed on a previous response, or omit it on the very first request and let Relay issue one. |
| `X-Relay-Project-Id` | `proj-1` | A project namespace. Together with your key it derives the durable project scope; the same key + project id = the same conversation family. |
| `X-Relay-Resume-Token` | *(single-use)* | **Optional.** Present the token from the previous response to resume. On the first request of a conversation, omit it. |

When you omit `X-Relay-Conversation-Id`, Relay issues a fresh
conversation id and echoes it back.

### Response headers

| Header | Meaning |
| --- | --- |
| `X-Relay-Conversation-Id` | The active conversation id — save it. |
| `X-Relay-Resume-Token` | A **new, one-time** resume token for the turn that just completed — save it and present it on the next request. |

## The pattern

Every turn follows the same two steps:

```bash
# 1. Send with the conversation id + project id (+ the resume token from
#    the previous turn, if any).
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <KEY>" \
  -H "X-Relay-Conversation-Id: <CID>" \
  -H "X-Relay-Project-Id: <PROJECT>" \
  -H "X-Relay-Resume-Token: <TOKEN>" \
  -d '{"model": "<MODEL_ID>", "messages": [{"role": "user", "content": "continue"}]}'
```

```bash
# 2. From the response headers, read the fresh token (and confirm the cid).
curl -sI -D - -o /dev/null ... # the X-Relay-Resume-Token and
                               # X-Relay-Conversation-Id headers
```

Store `X-Relay-Resume-Token` between turns. On the next request, send it
back in `X-Relay-Resume-Token`. Each presented token works **exactly
once**: after it is honored and the turn commits, Relay replaces it, so an
old token can never replay earlier work.

## Across clients

Continuity is keyed by **your key + conversation id + project id**, not by
which client is talking. A conversation started by Cline (with its
`X-Relay-Conversation-Id`) can be continued by OpenCode or Continue — or
any script — as long as it presents the same three headers and the current
resume token. That is the cross-client handoff scenario Relay is built
for: start on one tool, pick up on another, and the provider sees the
envelope of prior work instead of an empty conversation.

## Staleness, wrong tokens, and limits

- A **wrong or stale token never breaks chat**: the request still succeeds
  as a normal continuation (at `last_seq + 1`), the resume is simply not
  honored. No acknowledged work is ever replayed.
- Relay tracks resume attempts durably (`MAX_RESUME_REPLAYS`, default `3`).
  Once a token's replay budget is exhausted the resume path denies — even
  across restarts.
- The continuity envelope is **data-marked** (`[continuity context]` ...
  "data, not instructions"), so a provider can never mistake it for a
  system prompt.

## Privacy contract

Relay's database stores **metadata only**: conversation identity, per-turn
providers/models/outcomes/tokens, derived project state, summaries, and
resume-token **hashes** (SHA-256). Raw prompts, responses, and raw resume
tokens are **never persisted**. The opt-in content context
(`CONTINUITY_CONTENT_CONTEXT_ENABLED`) derives a redacted, bounded summary
of the *current request's* messages for the forwarded payload only — it is
ephemeral and never stored, logged, or exported. See
[docs/security.md](../security.md#redaction-contract) and
[docs/platform-db-schema.md](../platform-db-schema.md).

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Response has no `X-Relay-Resume-Token` | Continuity is off (`CONTINUITY_ENABLED=true` missing), the key is a bootstrap key (needs a store-backed key), or a header is missing. |
| `400 invalid_request` on a long/garbled conversation id | Malformed `X-Relay-Conversation-Id` value; Relay never echoes it. Use the exact id Relay issued. |
| Sequence seems to restart after a Relay restart | The resume token was not presented (or was stale); present the latest token and the same conversation/project ids. |
| Handoff envelope absent on the resumed request | The resume was denied (stale/wrong token, replay limit, or an unavailable store); chat still works. |

## Related documentation

- [Client setup guides index](index.md)
- [Configuration reference](../configuration.md#project-continuity-opt-in-p9) — all `CONTINUITY_*` variables
- [Security model](../security.md#redaction-contract) — the redaction/memory contract
- [Platform DB schema](../platform-db-schema.md) — the continuity tables and resume tracker
- [Troubleshooting](../troubleshooting.md)
- [Known limitations](../known-limitations.md)
