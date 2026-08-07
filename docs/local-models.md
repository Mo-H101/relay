# Local models (LM Studio & Ollama)

Relay can route to **local** model servers that run on your own machine.
Unlike cloud providers, local providers need **no API key**. This guide
walks through the two supported local options: **LM Studio** (an
OpenAI-compatible desktop app) and **Ollama**.

## How local providers work

`relay setup` lists local providers like any other provider. Selecting one
does a **connectivity check** against the server's base URL before
proceeding. If the server is not running (or not on the expected address),
the wizard reports `Not reachable` and skips the provider.

Relay does not start or manage the local model server — you start LM
Studio / Ollama yourself, and Relay talks to it over HTTP.

## Prerequisites

- Relay installed (see [README installation](../README.md#installation)).
- A local model server running on your machine:
  - **LM Studio**: the app's built-in HTTP server on `http://127.0.0.1:1234/v1`.
  - **Ollama**: the Ollama service on `http://127.0.0.1:11434`.

## Setting up LM Studio

1. Install and launch **LM Studio**.
2. Load a model: search the model catalog, download a model, and load it in
   the chat UI. It does not need to be loaded *before* setup, but it must be
   loaded before Relay can serve chats for it.
3. Start the local HTTP server:
   - In LM Studio, open the **Developer** tab.
   - Press **Start Server**. LM Studio serves the OpenAI-compatible API at
     `http://127.0.0.1:1234/v1` by default.
4. Run the Relay setup wizard and select **LM Studio (local)**:

   ```bash
   relay setup
   ```

5. Relay checks connectivity to the default base URL
   (`http://127.0.0.1:1234/v1`), lists the models LM Studio has loaded, and
   lets you set a priority order.

> **Using a different port?** Set the base URL before running setup by
> editing your `.env` file (see [Configuration](../README.md#configuration)
> for where it lives) and add:
>
> ```dotenv
> LMSTUDIO_BASE_URL=http://127.0.0.1:<your-port>/v1
> ```
>
> The value **must** end in `/v1` (the OpenAI-compatible suffix). LM Studio
> itself may also be configured to listen on another port in its Developer
> tab — the base URL must match.

## Setting up Ollama

1. Install and start **Ollama** (the installer starts the background service).
2. Pull a model from the command line, for example:

   ```bash
   ollama pull llama3.2
   ```

3. Confirm the service is reachable: `curl -s http://127.0.0.1:11434` should
   return `Ollama is running`.
4. Run the Relay setup wizard and select **Ollama (local)**:

   ```bash
   relay setup
   ```

5. Relay checks connectivity to the default base URL
   (`http://127.0.0.1:11434`), lists the models you have pulled, and lets you
   set a priority order.

> **Using a different port?** Set the base URL before running setup by
> editing your `.env` file and add:
>
> ```dotenv
> OLLAMA_BASE_URL=http://127.0.0.1:<your-port>
> ```
>
> (Ollama's base URL has **no** `/v1` suffix.)

## After setup

Once a local provider is configured, start Relay and use it exactly like a
cloud provider:

```bash
relay
```

Pick a model from `GET /v1/models` (the **exact** id — local ids often
include a size tag such as `llama3.2:3b`) and chat through
`/v1/chat/completions` or `/chat`. See the
[client setup guides](clients/index.md) to connect Cline, OpenCode,
Continue, or any OpenAI-compatible tool.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Setup says `Not reachable` for the provider | The local server is not running, or `LMSTUDIO_BASE_URL` / `OLLAMA_BASE_URL` does not match. Start the server, then re-run `relay setup`. |
| Setup lists no models | LM Studio has no model loaded; Ollama has no models pulled. Load/pull a model first, then re-run `relay setup`. |
| Chat returns `model not found` | The model id does not match `GET /v1/models` exactly (case, slashes, size tag). |
| Chat returns `502 provider_error` | The local server is running but the request failed — confirm the model is loaded and the server is still serving. |
| Relay and the model server bind the same port | Relay defaults to `127.0.0.1:8000`; the local servers use 1234 (LM Studio) and 11434 (Ollama), so this is unusual. If you changed ports, make sure Relay and the model server do not collide. |

## Related documentation

- [Client setup guides](clients/index.md)
- [Configuration reference](configuration.md)
- [Deployment & hardening](deployment.md)
- [Troubleshooting](troubleshooting.md)
