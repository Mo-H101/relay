# Relay v1.0.0 — User Experience Validation Guide (Phase 8)

This guide walks a **first-time user** through every surface of Relay with
the same eyes you will use to judge it. Its purpose is **manual, human
validation**: to find problems automated tests cannot — confusing prompts,
silent failures, surprising defaults, missing docs, unclear errors, and
rough edges that only show up when a real person is driving.

You are the tester. There are no right or wrong answers; anything that
confused you, anything that looked broken, anything you had to guess at is
a finding. Record it in the report template at the end.

> Scope note: this phase is **test-only**. Do not fix or refactor anything
> you find. Just document it.

---

## How to use this guide

- Work through the checklists **in order** where possible; some sections
  assume earlier ones are done.
- Every checklist item is a box `[ ]`. Mark it `[PASS]`, `[FAIL]`,
  `[PARTIAL]`, or `[N/A]`.
- For every `[FAIL]`/`[PARTIAL]`, fill in the "collect on failure" box.
- Keep the server's console log visible in a second terminal for the
  whole session — many answers live there.
- Every Relay response/error carries an `X-Relay-Correlation-Id` header.
  **Always record it when something goes wrong** — it is the fastest way
  to tie a failure to a log line.

### Recommended setup

Two terminals side by side:

| Terminal | Purpose |
| --- | --- |
| A | Run Relay (`python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`) |
| B | Run the CLI (`python -m app.cli setup`) and `curl` requests |

### Prerequisites

- Python 3.10+ available (`python --version`)
- A working internet connection
- An **NVIDIA NIM API key** (`nvapi-...`) from https://build.nvidia.com
- An **OpenAI API key** (`sk-...`) from https://platform.openai.com
  (note: the OpenAI account used in development had **no quota** — every
  completion returned 429. If your key also returns 429, that is expected
  and is the known blocker, not necessarily a bug.)

---

# Part A — The "first 30 minutes with Relay" journey

Follow this as a first-time user before touching the detailed checklists.
Time-box yourself: **0:30–0:40 total**. Take notes on anything that stops
you or makes you pause.

| Time | Step | What you do | Expected |
| --- | --- | --- | --- |
| 0:00–0:04 | Clone & install | `git clone <repo>`, `cd`, `python -m venv .venv`, activate, `pip install -r requirements.txt` | Install completes with no errors; `requirements.txt` pinned |
| 0:04–0:06 | Explore | `python -m app.cli --help`, then open README and `docs/configuration.md` | Help text appears; docs tell you where to get keys and what to set |
| 0:06–0:12 | First setup | `python -m app.cli setup`; enable NVIDIA, paste `nvapi-` key, accept model fetch, skip or set model priority | CLI masks your key, fetches the catalog, asks to test the provider, prints `OK: <model> (latency ...ms)` |
| 0:12–0:14 | Start server | `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000` | uvicorn banner, `Application startup complete`, no stack trace |
| 0:14–0:16 | First checks | `curl -s http://127.0.0.1:8000/` and `curl -s http://127.0.0.1:8000/providers` | `{"name":"Relay","status":"running"}`; providers list with NVIDIA models |
| 0:16–0:20 | First chat | `curl` to `/v1/chat/completions` with a model you saw in `/providers` | HTTP 200, JSON with `choices[0].message.content`, `X-Relay-Correlation-Id` header |
| 0:20–0:24 | Streaming | Same request with `"stream": true` and `curl -N` | Chunks arrive **incrementally**, end with `data: [DONE]` |
| 0:24–0:28 | Diagnose | `curl` `/diagnostics`, `/health`, `/metrics` | Snapshot JSON; aggregate health; Prometheus text |
| 0:28–0:30 | Reflect | Note everything that surprised you | — |

**Journey questions to answer in your report:**

1. Did any step require info the docs did not give you?
2. Did any step fail silently (no error, no hint)?
3. Did any output look scary or confusing (stack traces, raw keys, 500s)?
4. What would have made the flow obvious the first time?

---

# Part B — Detailed checklists

---

## 1. Fresh installation / setup

**What I should do**

```bash
python --version
git clone <relay-repo-url> relay
cd relay
python -m venv .venv
# Windows: .venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
python -m app.cli --help
python -m compileall -q app
```

**Expected behavior**

- `pip install` succeeds (5 pinned dependencies; `python-dotenv`,
  fastapi, httpx, pydantic, uvicorn).
- `python -m app.cli --help` prints usage with a `setup` subcommand.
- `compileall` finishes silently (byte-compiles `app`).
- The project root contains a `.env.example` you can copy.
- There is no `.env` yet (or it is gitignored) and no leftover
  `health.json`/state DB at the root.

**Signs of a bug**

- `pip install` fails on a pinned version (missing wheel, resolution error).
- `ModuleNotFoundError` or import errors at startup after a clean install.
- The CLI help is empty or crashes.
- The repo ships a `.env` with real keys, or state/diagnostic artifacts.

**Logs / diagnostics to collect**

- Full `pip install` output (tail).
- `python -m pip list` inside the venv.
- The exact error text of any import failure.
- `ls` of the project root showing stray artifacts.

---

## 2. Environment configuration

**What I should do**

- Run `python -m app.cli setup` and enable **NVIDIA first**. Paste your
  `nvapi-` key when asked, then when it asks about custom model priority,
  select a small, fast model you expect to work (e.g. search
  `deepseek-v4-flash` or `llama-3.1-8b`).
- Answer "Test NVIDIA provider now?" → yes.
- Then open `.env` and read it back.

**Expected behavior**

- The CLI masks your key in prompts (`********XXXX`).
- The key is written into `.env` quoted, e.g. `NVIDIA_API_KEY="nvapi-..."`.
- The CLI fetches the NVIDIA model catalog ("N models available").
- The provider test prints `OK: <model> (latency Nms)` or a clear
  `FAILED: <model> (<reason>)`.
- The CLI ends with: `Configuration saved to .env. Restart the server to
  apply.`

**Signs of a bug**

- Key saved unquoted or mangled (spaces, wrong quotes).
- CLI says "No API key set" even though you typed one.
- "Could not fetch models: ..." with a raw, scary error (or worse, a
  stack trace).
- The provider test hangs indefinitely.
- `.env` does not appear after setup.

**Logs / diagnostics to collect**

- Full CLI transcript (copy the whole terminal block).
- The `_mask_key` output line (should show only last 4 chars).
- Contents of `.env` **with keys redacted** (replace the key value with
  `REDACTED` — never paste a real key into a report).

---

## 3. Starting Relay

**What I should do**

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then, in terminal B:

```bash
curl -s http://127.0.0.1:8000/
curl -s http://127.0.0.1:8000/providers
curl -s http://127.0.0.1:8000/health
```

**Expected behavior**

- uvicorn banner, "Application startup complete", no traceback.
- `GET /` → `{"name":"Relay","status":"running"}`.
- `GET /providers` → `{"providers":[{"name":"NVIDIA","enabled":true,
  "priority":...,"models":[...]}]}` with a non-empty model list.
- `GET /health` → `{"status":"ok"|"degraded"|"unavailable"}` only.
- Stop with `Ctrl+C`: clean shutdown message, process exits.

**Signs of a bug**

- Startup crash: `ValueError`/`ValidationError` (invalid config value in
  `.env`), port already in use, `ImportError`.
- `/providers` shows **zero models** even though a key is set (this is a
  known silent-failure spot — a bad/expired key makes the provider
  register with an empty catalog and Relay does **not** warn).
- `/health` hangs for a long time (it performs live per-provider probes).
- `Ctrl+C` does not exit cleanly.

**Logs / diagnostics to collect**

- Complete startup log block.
- Output of `/`, `/providers`, `/health`.
- If `/providers` is empty: `curl -s http://127.0.0.1:8000/health/deep`
  and `/diagnostics`.

---

## 4. Adding cloud providers (NVIDIA first, OpenAI later)

**What I should do**

1. NVIDIA is already enabled from section 2. Verify:
   `curl -s http://127.0.0.1:8000/providers`.
2. Now add OpenAI: `python -m app.cli setup`, answer "Enable OpenAI" →
   `y`, paste the `sk-...` key, pick a model (e.g. search `gpt-4o-mini`),
   and run the provider test.
3. Check `.env` shows `OPENAI_ENABLED=true`.
4. **Restart the server** (the CLI does not apply changes until restart).

**Expected behavior**

- OpenAI provider appears alongside NVIDIA in `/providers`.
- The OpenAI provider test reports `OK` if the key has quota, or a clear
  failure (429 quota) if it does not.
- Both providers keep their own model priority from `.env`.

**Signs of a bug**

- Enabling OpenAI silently flips NVIDIA off (or vice versa).
- The second `setup` run forgets the first provider's settings.
- Key masking shows more than the last 4 characters.
- Duplicate model entries in `/providers` when both list the same model.

**Logs / diagnostics to collect**

- `/providers` output after both providers are added.
- CLI transcript for the OpenAI step.
- Note the exact failure text of the OpenAI probe if it fails.

---

## 5. Making the first API request

**What I should do**

Pick a model from `/providers` (copy the exact id), then:

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "<EXACT-MODEL-ID>", "messages": [{"role": "user", "content": "Say hello in one short sentence."}]}'
```

**Expected behavior**

- HTTP 200 with OpenAI-shaped JSON:
  `id` (starts `chatcmpl-`), `object: "chat.completion"`, `model`
  (echoed), `choices[0].message.content`, `usage` tokens.
- Response header `X-Relay-Correlation-Id` is present and matches the
  server log line for this request.

**Signs of a bug**

- HTTP 400 with `code: "model_not_found"` — usually a wrong model id;
  verify it matches `/providers` exactly.
- HTTP 502 `provider_error` — provider rejected the request (see
  `error.message`).
- HTTP 500 `relay_error` with a raw exception string leaking to the
  client — **this is a known finding, record it** (non-stream path returns
  `str(exc)`).
- Hang with no response — note how long, then check the log.

**Logs / diagnostics to collect**

- Full response body (redact nothing — it contains no secrets; but the
  request prompt you send is yours to share or not).
- The `X-Relay-Correlation-Id` value.
- The matching log lines from the server terminal.

---

## 6. Using `/v1/chat/completions`

**What I should do**

Test the non-streaming surface systematically:

```bash
# A. system + user message, generation params
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "<MODEL>", "temperature": 0.2, "max_tokens": 64,
       "messages": [
         {"role": "system", "content": "You answer in pirate dialect."},
         {"role": "user", "content": "What is 2+2?"}
       ]}'

# B. unknown model -> expect 400 model_not_found
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "no/such-model", "messages": [{"role": "user", "content": "hi"}]}'

# C. tool_choice without tools -> expect 400 invalid_request
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "<MODEL>", "tool_choice": "auto",
       "messages": [{"role": "user", "content": "hi"}]}'

# D. model list
curl -s http://127.0.0.1:8000/v1/models
```

**Expected behavior**

- (A) Returns the system prompt respected (pirate-flavored answer),
  `finish_reason: "stop"`, `temperature`/`max_tokens` honored.
- (B) HTTP 400, body `{"error":{"type":"invalid_request_error",
  "code":"model_not_found",...}}`.
- (C) HTTP 400, body `{"error":{... "code":"invalid_request"}}` telling
  you tool_choice needs a tools list.
- (D) `{"object":"list","data":[{...,"owned_by":"NVIDIA"}]}` — every model
  from every provider.

**Signs of a bug**

- (A) `max_tokens` ignored; `temperature` ignored; `finish_reason`
  missing; `usage` missing.
- (B)/(C) Wrong status code (500 instead of 400) or a `{"detail":...}`
  FastAPI error shape instead of the OpenAI `{"error":...}` shape.
- (D) Empty list, or `owned_by` wrong, or missing models.

**Logs / diagnostics to collect**

- The 4 responses.
- Correlation ids for any failure.
- Server log lines for each.

---

## 7. Connecting Cline

**What I should do**

1. Keep Relay running.
2. Open Cline (VS Code extension) → open its settings.
3. Choose API provider **"OpenAI Compatible"**.
4. Set:
   - Base URL: `http://localhost:8000/v1`
   - API Key: the `RELAY_API_KEY` value (see section 7 note below; empty
     is fine only if you left `RELAY_API_KEY` unset)
   - Model ID: a model you saw in `GET /v1/models`
5. Send a message in the Cline chat panel.

**Note on auth:** with `RELAY_API_KEY` set, **every** non-public route
(including `/v1/chat/completions`) requires `Authorization: Bearer
<RELAY_API_KEY>` or `X-Relay-API-Key: <RELAY_API_KEY>`. Cline must send
one of these. If you left `RELAY_API_KEY` empty, no key is needed.

**Expected behavior**

- Cline connects without an auth/connection error.
- Messages get responses, and streaming appears to stream (chunked text
  in the chat panel).
- Model picker shows the Relay model(s) you configured.

**Signs of a bug**

- Cline reports "invalid API key" (auth mismatch or key not sent).
- Cline reports "model not found" (model id mismatch with `/v1/models`).
- Connection refused (wrong port/host or Relay stopped).
- Requests go to Cline's own default provider instead of Relay (config
  applied to the wrong provider slot).
- Streaming text appears all at once at the end (proxy buffering).

**Logs / diagnostics to collect**

- The exact Cline error message.
- Relay's server log for the failed request (with correlation id).
- Screenshot/quote of the Cline settings fields.

---

## 8. Connecting OpenCode

**What I should do**

1. Keep Relay running.
2. Add a custom OpenAI-compatible provider to your OpenCode config. The
   `provider` section lives in your project `.opencode/config.json` (or
   `opencode.json`). Example:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "relay": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Relay",
      "options": {
        "baseURL": "http://localhost:8000/v1",
        "apiKey": "<RELAY_API_KEY or empty>"
      },
      "models": {
        "<EXACT-MODEL-ID-FROM-/v1/models>": { "name": "NVIDIA <model>" }
      }
    }
  }
}
```

3. In OpenCode, select the Relay provider and a Relay model
   (`/models` command).
4. Ask it a coding question and let it use tools (read a file, run a
   command).

**Expected behavior**

- OpenCode lists the Relay provider and its model(s).
- Chat works and tool calls round-trip (the model emits tool calls;
   OpenCode executes them and sends results back through Relay).
- Responses stream.

**Signs of a bug**

- Provider not listed (config schema error — check `$schema`/fields).
- Model id mismatch errors (id must match `/v1/models` exactly).
- Tool calls fail or hang through Relay (a key area — see section 10).
- Auth 401s (missing/incorrect `apiKey`).
- Relay receives nothing at all (baseURL wrong or wrong config file).

**Logs / diagnostics to collect**

- Your config file (redact the key).
- OpenCode error text.
- Relay log lines + correlation ids for failed calls.
- OpenCode version (`opencode --version`).

---

## 9. Streaming responses

**What I should do**

```bash
curl -N -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "<MODEL>", "stream": true,
       "messages": [{"role": "user", "content": "Count slowly from 1 to 10."}]}'
```

**Expected behavior**

- `Content-Type: text/event-stream`.
- A sequence of `data: {...}` lines. Each chunk has `object:
  "chat.completion.chunk"`, stable `id`, `choices[0].delta.content`
  tokens, growing incrementally.
- A final chunk with `finish_reason: "stop"`.
- Terminated by `data: [DONE]`.
- Tokens arrive **over time**, not all at once.

**Signs of a bug**

- No output until the stream completes (buffered — the stream is not
  actually streaming).
- Missing `data: [DONE]` terminator.
- Error emitted as a chunk with HTTP 200 (should be, per current design, a
  `{"error":{"type":"stream_error",...}}` chunk — note if it surprises you
  or your client can't handle it).
- Chunks missing `id`/`created`/`model`.
- Mid-stream teleportation: chunk `delta` values that don't reassemble
  into the final text.

**Logs / diagnostics to collect**

- Raw `curl -N` output (save to a file).
- Server log lines.
- If you use a client (Cline/OpenCode), note how it renders streaming.

---

## 10. Tool calls

**What I should do**

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<MODEL>",
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "Get weather for a city",
        "parameters": {
          "type": "object",
          "properties": {"city": {"type": "string"}},
          "required": ["city"]
        }
      }
    }],
    "tool_choice": "auto",
    "messages": [{"role": "user", "content": "What is the weather in Paris?"}]
  }'
```

Then simulate the tool round-trip — send the assistant tool-call message
plus a `tool` result:

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<MODEL>",
    "messages": [
      {"role": "user", "content": "What is the weather in Paris?"},
      {"role": "assistant", "content": null, "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "get_weather", "arguments": "{\"city\":\"Paris\"}"}}
      ]},
      {"role": "tool", "tool_call_id": "call_1", "content": "22C, sunny"}
    ]
  }'
```

**Expected behavior**

- First call returns an assistant message whose `message.tool_calls` is
  populated (function name + JSON arguments) and `finish_reason:
  "tool_calls"`.
- Second call (with history) returns a natural-language answer using the
  tool result.
- Relay is a **transparent passthrough**: it must not alter the tool
  payload or invent fields.

**Signs of a bug**

- `tool_calls` never populated (model doesn't emit them → verify the
  model supports tools).
- `finish_reason` not `tool_calls`.
- Second call errors because the assistant `content: null` was dropped or
  the `tool_calls`/`tool_call_id` fields were lost in transit.
- `tool_choice` with an explicit function name ignored.
- Relay reorders or rewrites arguments.

**Logs / diagnostics to collect**

- Both full responses.
- Correlation ids.
- If it fails, the provider error inside the 502 body.

---

## 11. Multi-turn conversations

**What I should do**

Send a 3-turn conversation **as one request with full history**, then a
follow-up request that appends the history:

```bash
# Turn 1
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "<MODEL>", "messages": [
    {"role": "system", "content": "You are terse."},
    {"role": "user", "content": "My name is Alex."}
  ]}'

# Turn 2: append prior assistant reply + new user message
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "<MODEL>", "messages": [
    {"role": "system", "content": "You are terse."},
    {"role": "user", "content": "My name is Alex."},
    {"role": "assistant", "content": "<paste turn 1 reply>"},
    {"role": "user", "content": "What is my name?"}
  ]}'
```

**Expected behavior**

- Turn 2 answers "Alex" — context from earlier turns is preserved.
- The full message array is forwarded verbatim (no reordering, no dropped
  roles).
- `system`/`user`/`assistant`/`tool` roles all pass through.

**Signs of a bug**

- Turn 2 has no memory of "Alex" (context dropped).
- Relay rewrites or reorders messages.
- A `tool` role message without a matching `tool_call_id` fails when the
  provider (not Relay) rejects it — note whether the error is clear.
- Content ordering (system-first) is disturbed.

**Logs / diagnostics to collect**

- Both requests and responses.
- Correlation ids.
- Any provider rejection text.

---

## 12. Provider failure and failover

**What I should do**

Set up a deliberate failure. In `.env`:

```dotenv
NVIDIA_MODEL_PRIORITY=<a-model-that-404s-or-you-lack-access-to>,<a-good-model>
```

Or, to test cross-provider failover: list a model the OpenAI provider
claims but that errors, ahead of a working NVIDIA model of the same name.
Then restart Relay and send a chat for that model.

Also test the all-fail path with a model only OpenAI has (quota 429), or
set an invalid key temporarily.

**Expected behavior**

- A failing first candidate rolls to the next candidate (same provider,
  then other providers). The request succeeds via the working model.
- `GET /provider` shows which provider Relay would select.
- With all candidates failing, the `/v1` route returns HTTP 502
  `provider_error` and the `/chat` route returns 502/503 — **cleanly, with
  an explanation, not a hang**.
- `X-Relay-Correlation-Id` ties the whole failover sequence in the logs.
- If `DECISION_EXPLANATIONS_ENABLED=true`, `GET /decision/explain`
  explains the ranking.

**Signs of a bug**

- No failover at all (request fails even though a later candidate works).
- Hang while waiting for a dead provider (timeout not respected).
- 502 body leaks raw provider internals or keys.
- Retry storm: Relay retries beyond `MAX_RETRIES` or ignores
  `Retry-After` when configured to honor it.
- `/provider` disagrees with the actual provider that served a request.

**Logs / diagnostics to collect**

- The request you sent and the correlation id.
- Server log lines for the whole failover sequence (attempts, latencies,
  failure types).
- `GET /provider`, `GET /diagnostics`, and (if enabled)
  `GET /decision/explain`.

---

## 13. Restart and persistence recovery

**What I should do**

1. Enable the production learning profile in `.env`:

```dotenv
TELEMETRY_ENABLED=true
HEALTH_FEEDBACK_ENABLED=true
HEALTH_AWARE_ROUTING=true
ADAPTIVE_ROUTING_ENABLED=true
QUALITY_FEEDBACK_ENABLED=true
DECISION_ENGINE_ENABLED=true
DECISION_EXPLANATIONS_ENABLED=true
PERSISTENCE_ENABLED=true
PERSISTENCE_PATH=./relay_state.db
```

2. Restart Relay. Send a handful of chats (a few succeed, force one
   failure if you can).
3. `curl http://127.0.0.1:8000/diagnostics` — record the "before" snapshot.
4. Stop Relay with **`Ctrl+C`** (graceful SIGINT), watch the shutdown log.
5. Restart Relay. `curl http://127.0.0.1:8000/diagnostics` again.
6. Confirm `relay_state.db` exists at the project root.

**Expected behavior**

- A `relay_state.db` file (plus `-wal`/`-shm`) appears.
- Graceful shutdown runs a final flush (log shows it).
- After restart, learned state (health/telemetry/quality) is retained:
  `/diagnostics` before/after are substantially the same.
- A single process serves fine.

**Signs of a bug**

- State is reset on every restart (persistence not working).
- No `relay_state.db` created despite `PERSISTENCE_ENABLED=true`.
- Startup logs "persistence unavailable; continuing without it" (open or
  load failure — record the reason).
- Corrupt DB handling: if you delete/corrupt the DB, Relay should
  continue and back up the bad file as `<path>.corrupt-<timestamp>.bak` —
  note if it instead crashes or silently loses everything.
- **Running more than one worker/process against the same DB corrupts
  it.** Do NOT run `uvicorn --workers 4`. If a doc or the server lets you
  do this without a warning, that's a finding.

**Logs / diagnostics to collect**

- `/diagnostics` before and after restart.
- Shutdown log block.
- `dir` of the project root showing `relay_state.db*`.
- Any "persistence unavailable" warning.

---

## 14. Diagnostics and metrics

**What I should do**

```bash
curl -s http://127.0.0.1:8000/diagnostics
curl -s http://127.0.0.1:8000/health/deep
curl -s http://127.0.0.1:8000/providers
curl -s http://127.0.0.1:8000/metrics
curl -s http://127.0.0.1:8000/decision/explain

# feedback (metadata only; never include prompt text)
curl -s -X POST http://127.0.0.1:8000/feedback \
  -H "Content-Type: application/json" \
  -d '{"provider": "NVIDIA", "model": "<MODEL>", "rating": 5}'
```

**Expected behavior**

- `/diagnostics`: a rich JSON snapshot — provider states, health, learned
  telemetry/quality, persistence status. **No prompts, responses, or
  keys.**
- `/health/deep`: per-provider, per-model health with statuses and
  latencies.
- `/metrics`: Prometheus text exposition (`relay_http_requests_total`,
  `relay_chat_*`, `relay_auth_*`, ...). Label values are bounded
  (provider names, status bands, route templates) — no prompts, no keys,
  no unbounded label values.
- `/feedback` with a valid (provider, model): HTTP 202 `{"stored": true,
  ...}`. Sending `prompt`/`message`/`response`/`content` in the payload is
  **rejected** (422) — this is intentional (metadata only).
- If `DECISION_EXPLANATIONS_ENABLED=false`, `/decision/explain` returns
  `{"enabled": false, ...}` — not an error.

**Signs of a bug**

- Any of these leak prompts, API keys, or user data.
- `/metrics` has unbounded labels (e.g. a model string in a label) or is
  broken text.
- `/feedback` stores prompt-like fields, or returns 500.
- These endpoints are reachable **without auth** when `RELAY_API_KEY` is
  set (they must require the key).
- `/diagnostics` triggers network probes (it must be read-only, no
  probes).

**Logs / diagnostics to collect**

- Sample output of each endpoint (metrics can be truncated to a few
  representative lines).
- If any leak is suspected, the exact payload (redact any secret).

---

# Part C — Common mistakes new users might make

These are traps a first-time user is likely to hit. Each is either a doc
gap, a silent failure, or a UX sharp edge. Test each, then note in the
report which ones confused you and what the docs should have said.

1. **Running setup and forgetting to restart.** The CLI ends with
   "Restart the server to apply" — easy to miss. Test what happens if you
   don't restart (nothing changes; silent).
2. **Never running setup at all.** No provider → `GET /chat` returns 503
   "No provider available" and `/v1/chat/completions` returns 400
   `model_not_found`. Is the error clear enough?
3. **Typing a wrong model id.** Errors say "Model 'x' not available from
   any provider" — good, but a new user may not know to check
   `/v1/models`. Is the fix discoverable?
4. **Bad/expired NVIDIA key = silent zero models.** The provider registers
   with an **empty catalog and no warning**. `/providers` shows the
   provider but no models. Nothing in the logs explains it. This is the
   single most likely silent-failure trap.
5. **`RELAY_API_KEY` left empty.** Auth is **off by default** — every
   endpoint except `/` and `/health` is open (including `/metrics`,
   `/diagnostics`, `/admin/reload`, `/docs`). A new user who assumes auth
   is on will leak management endpoints.
6. **`RELAY_API_KEY` set, then client forgets the key.** Everything 401s
   including `/docs`. Test whether the docs page explains how to
   authenticate (Swagger "Authorize" button accepts Bearer or
   `X-Relay-API-Key`).
7. **Expecting OpenAI to work with an out-of-quota key.** 502
   `provider_error` on every request is the expected (documented) symptom,
   not a Relay bug — but is the error message self-explanatory?
8. **`uvicorn --workers N` / gunicorn multi-worker.** The state DB is
   **single-process/single-writer**. Multi-worker setup corrupts
   `relay_state.db`. A new user following generic FastAPI deployment
   advice will hit this. Is it warned about loudly enough?
9. **`HEALTH_REFRESH_ENABLED=true` on a machine that can't reach every
   provider endpoint.** The background prober then reports everything
   unavailable. Defaults are off; a user copying the full `.env.example`
   may enable it blindly.
10. **Expecting logs on stderr.** Relay JSON logs go to **stdout**
    (`LOG_FILE` empty). uvicorn's own access log goes to stderr — users
    who filter stderr will "lose" the application logs.
11. **Proxying everything.** `PROXY_ENABLED=true` falls back to process
    `HTTP(S)_PROXY` vars — a host with proxy env vars tunnels all provider
    traffic silently. Check whether that surprises you.
12. **Looking for `/decision` instead of `/decision/explain`.** The route
    is `/decision/explain`.
13. **Trusting `/health` for provider health.** `/health` performs **live
    probes** on every call and returns `unavailable` when keys are
    missing. A keyed-down box shows `unavailable` even though the app is
    fine.
14. **Polluting `/metrics`.** Requesting many random paths is fine (they
    bucket to "unmatched"), but anything that puts user-controlled strings
    into labels would be a bug — verify the exposure looks bounded.
15. **Sending prompt content to `/feedback`.** It's metadata-only and
    rejects content fields. New users trying to send a prompt get 422 —
    is that clear?
16. **LM Studio / OpenAI-compatible local server without the `/v1` suffix**
    in `LMSTUDIO_BASE_URL`.

---

# Part D — Final report template

Copy this template into a new file (e.g. `ux-findings.md`), fill it in,
and commit it with your findings.

```markdown
# Relay v1.0.0 — UX Validation Report

Tester: <name>
Date: <date>
Environment: OS / Python / venv / Relay commit (or version)
Providers tested: NVIDIA (yes/no) / OpenAI (yes/no) / LM Studio (yes/no)
Auth enabled during test: yes/no — RELAY_API_KEY set: yes/no
Intelligence profile enabled (sections 13): yes/no

## A. First-30-minutes journey

- Stopped me / confused me:
- Silent failures:
- Scary outputs:
- Would recommend changing:

## B. Checklist results

| # | Section | Result | Notes / correlation ids |
|---|---------|--------|-------------------------|
| 1 | Installation/setup |        | |
| 2 | Environment config |        | |
| 3 | Starting Relay      |        | |
| 4 | Adding providers    |        | |
| 5 | First API request   |        | |
| 6 | /v1/chat/completions|        | |
| 7 | Cline               |        | |
| 8 | OpenCode            |        | |
| 9 | Streaming           |        | |
| 10 | Tool calls          |        | |
| 11 | Multi-turn          |        | |
| 12 | Failover            |        | |
| 13 | Restart/persistence |        | |
| 14 | Diagnostics/metrics |        | |

(Result: PASS / FAIL / PARTIAL / N/A)

## C. Findings

### Finding 1
- Section / step:
- Severity (blocker / major / minor / nit):
- What I did:
- What I expected:
- What happened:
- Evidence (exact output, correlation id, log block):
- Does docs/behavior explain it? Where?

### Finding 2
... (repeat)

## D. Common-mistakes check

Which of the 16 traps in Part C did you hit or find under-documented?
- 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16  (circle)
- Worst one:
- What the docs should have said:

## E. Verdict

- Biggest UX problem found:
- Is Relay usable by a first-time user with only the README + docs?
- Recommended actions before GA (do not fix now — just list):
```

---

*This guide is part of the Phase 8 manual validation effort. Findings are
recorded for triage; no fixes are applied in this phase.*
