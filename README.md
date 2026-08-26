# Relay

A self-hosted LLM routing proxy. Relay sits between your application and
one or more LLM providers, picks the best provider/model for each request,
fails over across models and providers when one is unavailable, and learns
from real request outcomes to keep routing smart over time.

- OpenAI-compatible endpoint (`/v1/chat/completions`) plus a native
  `/chat` endpoint.
- Multiple backends: NVIDIA NIM, OpenAI, Anthropic, Google Gemini, and the
  local LM Studio and Ollama servers (OpenAI-compatible local servers work
  through the LM Studio client).
- **Async-first API hot path** — both endpoints are `async def` with
  non-blocking provider I/O via `httpx.AsyncClient`; sync path retained
  as fallback.
- **OpenAI-compatible async SSE streaming** — `data: {...}\n\n` format,
  chunk ordering, usage passthrough, mid-stream error handling, client
  disconnect handling, and empty-stream failover.
- Health-aware routing, task-specific routing, adaptive EWMA reliability/
  latency signals, quality-feedback routing, and an explainable decision
  engine.
- Optional SQLite write-behind persistence so learned state survives
  restarts.
- Optional API-key authentication, optional Prometheus metrics, optional
  outbound proxy support, and hot configuration reload.
- A terminal interface (`relay`) with a dashboard, chat, model/provider
  management, and diagnostics panels.

## Quick start

```bash
# 1. Configure providers (interactive: API keys, model priority, task routing)
relay setup

# 2. Run the terminal interface (starts an embedded API server)
relay
#   ...or, to run only the headless API server:
#   relay serve
```

Send a chat:

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "meta/llama-3.1-8b-instruct", "messages": [{"role": "user", "content": "Hello"}]}'
```

Interactive docs: http://127.0.0.1:8000/docs (Swagger UI) or
http://127.0.0.1:8000/redoc (ReDoc).

## Choosing a provider

`relay setup` presents the providers below. Start with a cloud provider
(no local setup) or a local one (no API key).

| Provider | Type | Requires | Notes |
| --- | --- | --- | --- |
| NVIDIA NIM | Cloud | API key from build.nvidia.com | Ready-to-use hosted models; a good default |
| OpenAI | Cloud | API key from platform.openai.com | Standard OpenAI catalog |
| Anthropic | Cloud | API key | Claude models |
| Google Gemini | Cloud | API key | Gemini models |
| LM Studio (local) | Local | LM Studio running locally | OpenAI-compatible; no API key needed |
| Ollama (local) | Local | Ollama running locally | No API key needed |

For local models, see [docs/local-models.md](docs/local-models.md) for the
LM Studio and Ollama setup walkthrough.

## Documentation

- [Architecture](docs/architecture.md) — components, layering, request flow.
- [Configuration](docs/configuration.md) — every environment variable.
- [Security](docs/security.md) — key model, precedence, permissions, redaction, lifecycle.
- [Routing & decisions](docs/routing-decisions.md) — how a provider/model is chosen.
- [Deployment](docs/deployment.md) — production hardening, persistence, auth, proxies.
- [Troubleshooting](docs/troubleshooting.md) — common problems and diagnostics.
- [Terminal interface guide](docs/tui-guide.md) — startup behavior, the seven screens, Windows requirements.
- [Client setup guides](docs/clients/index.md) — connect Cline, OpenCode, Continue, or any OpenAI-compatible client to Relay.
- [Project continuity](docs/clients/continuity.md) — opt-in conversation resume across provider switches and Relay restarts.
- [Local models](docs/local-models.md) — LM Studio and Ollama setup walkthrough.
- [Known limitations](docs/known-limitations.md) — accepted risks, single-process constraints, operational caveats.

## Endpoints

| Method | Path | Description | Public |
| --- | --- | --- | --- |
| GET | `/` | Service banner | yes |
| GET | `/health` | Aggregate liveness (`ok`/`degraded`/`unavailable`), no internals | yes |
| GET | `/providers` | Registered providers, models, priority | no |
| GET | `/health/deep` | Deep per-model health report | no |
| GET | `/provider` | Provider Relay would select next | no |
| GET | `/decision/explain` | Why the last ranking came out as it did | no |
| POST | `/chat` | Native chat (task-aware, async) | no |
| GET | `/diagnostics` | Operational snapshot: health, telemetry, decision, persistence | no |
| POST | `/v1/chat/completions` | OpenAI-compatible chat (async, streaming supported) | no |
| GET | `/v1/models` | OpenAI-compatible model list | no |
| POST | `/feedback` | Metadata-only quality rating for a (provider, model) | no |
| POST | `/admin/reload` | Hot-reload configuration from `.env` | no |
| GET | `/metrics` | Prometheus text exposition | no |
| GET | `/docs`, `/redoc`, `/openapi.json` | API documentation | no* |

`*` The documentation routes are gated by the same API-key dependency as
every other route, so they are only reachable without a key when
`RELAY_API_KEY` is unset.

### Client API keys

Create a per-client key and point your tooling at Relay with it:

```bash
relay keys add --label opencode --scopes chat,v1   # prints the raw key exactly once
```

Then configure the client (OpenAI SDK, Cline, OpenCode, …):

```bash
base_url=http://relay-host:8000/v1
api_key=<rl_... returned by `relay keys add`>
```

`relay keys list` shows metadata only, `relay keys remove <id>` revokes,
and `relay keys test` verifies a key without echoing it. Store-backed keys
are accepted when `RELAY_AUTH_STORE=true`; a store outage fails closed
(`401`). See [docs/security.md](docs/security.md) for the full key model.

Step-by-step setup for Cline, OpenCode, Continue, and any other
OpenAI-compatible client: [client setup guides](docs/clients/index.md).

### Async Streaming

Both `/chat` and `/v1/chat/completions` are now `async def` and use a
fully non-blocking provider layer:

- **Non-blocking I/O** — providers use `httpx.AsyncClient`; no threadpool
  hops for the hot path.
- **OpenAI-compatible SSE** — `data: {...}\n\n` format with proper
  `[DONE]` termination, chunk ordering preserved.
- **Usage passthrough** — provider `usage` chunks are forwarded verbatim.
- **Mid-stream errors** — provider errors mid-stream yield an error chunk
  then `[DONE]`; connection stays clean.
- **Client disconnect handling** — when the HTTP client disconnects, the
  provider generator is closed cleanly (no leaked tasks).
- **Empty-stream failover** — if a provider yields no content, Relay
  fails over to the next candidate automatically.
- **Cancellation safety** — `asyncio.CancelledError` propagates cleanly
  through all layers (validated by `tests/test_async_cancellation.py`).

Authentication, when `RELAY_API_KEY` is set, uses either an
`Authorization: Bearer <key>` header or an `X-Relay-API-Key: <key>`
header. Requests without a valid key receive `401 Unauthorized`.

## Configuration

All configuration comes from environment variables or a `.env` file. Where
that `.env` lives depends on how you installed Relay:

- **Installed package** (`pip install` or `install.cmd`): `%LOCALAPPDATA%\relay\.env` on
  Windows, `~/.local/share/relay/.env` on Linux, and
  `~/Library/Application Support/relay/.env` on macOS — Relay always uses
  its per-user data directory, regardless of the current working directory.
- **Source checkout**: `.env` at the project root (or in the current working
  directory, then next to the app package).

See [docs/configuration.md](docs/configuration.md) for the
full reference. The most common settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| `NVIDIA_ENABLED` | `true` | Use the NVIDIA NIM endpoint |
| `NVIDIA_API_KEY` | — | NVIDIA API key |
| `OPENAI_ENABLED` | `false` | Use the OpenAI endpoint |
| `OPENAI_API_KEY` | — | OpenAI API key |
| `LMSTUDIO_ENABLED` | `false` | Use a local OpenAI-compatible server |
| `LMSTUDIO_BASE_URL` | `http://localhost:1234/v1` | Local server base URL |
| `RELAY_API_KEY` | — | Enables API-key auth on all non-public routes |
| `HEALTH_AWARE_ROUTING` | `false` | Skip unhealthy providers/models during routing |
| `ADAPTIVE_ROUTING_ENABLED` | `false` | Learn EWMA reliability/latency and reorder within health bands |
| `QUALITY_FEEDBACK_ENABLED` | `false` | Learn from `/feedback` ratings within health bands |
| `DECISION_ENGINE_ENABLED` | `false` | Produce explicit, explainable decisions |
| `DECISION_EXPLANATIONS_ENABLED` | `false` | Serve `/decision/explain` |
| `PERSISTENCE_ENABLED` | `false` | Persist learned state to SQLite |
| `TELEMETRY_ENABLED` | `false` | Record per-attempt telemetry |
| `HEALTH_FEEDBACK_ENABLED` | `false` | Feed request outcomes into the health store |
| `RELAY_TUI_NO_EMBED` | `false` | Run the TUI without the embedded API server |

### Recommended production profile

Every intelligence flag above defaults to `false`, so out of the box Relay
is byte-identical to a plain priority + failover router. When you are ready
to let it learn, copy the block below into `.env`. It turns on telemetry,
health learning, adaptive routing, quality scoring, the decision engine,
and persistence together, keeping all other thresholds at their defaults:

```dotenv
# Intelligence & learning (recommended production profile)
TELEMETRY_ENABLED=true
HEALTH_FEEDBACK_ENABLED=true
HEALTH_AWARE_ROUTING=true
ADAPTIVE_ROUTING_ENABLED=true
QUALITY_FEEDBACK_ENABLED=true
DECISION_ENGINE_ENABLED=true
DECISION_EXPLANATIONS_ENABLED=true
PERSISTENCE_ENABLED=true
PERSISTENCE_PATH=./.relay/platform.db
```

The flags form a dependency chain: telemetry feeds adaptive learning and
health feedback; health feedback makes health-aware routing meaningful;
persistence keeps all learned state across restarts. `HEALTH_REFRESH_ENABLED`
is intentionally left off here — it runs a background prober that needs live
network access to every provider endpoint. See
[docs/configuration.md](docs/configuration.md) for the full reference and
dependency notes.

## Installation

Requires **Python 3.10 or newer**. Install Relay in a virtual environment,
then start it by typing `relay`:

```bash
# From PyPI (once published)
pip install relay

# From GitHub (one command)
pip install git+https://github.com/<org>/<repo>.git

# One-command installers (from a checkout)
#   Windows (recommended — bypasses the default execution policy safely):
.\install.cmd
#   Windows PowerShell (requires an execution-policy bypass or remote-signed
#   policy; the .cmd wrapper above does this for you):
powershell -ExecutionPolicy Bypass -File .\install.ps1
#   macOS / Linux:
./install.sh
```

After a successful install, the terminal prints:

    Installation complete. ... Open a NEW terminal and type:
        relay

The installer adds `relay` to your PATH, but already-open terminals do
**not** see the change — open a new cmd/PowerShell/shell window first.
On Windows, if a new terminal still reports `'relay' is not recognized`,
open one as administrator and run
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`
(process-local, not persisted) before re-running the installer.

### Upgrading

- **From PyPI:** `python -m pip install --upgrade relay`.
- **From GitHub:** re-run
  `python -m pip install --upgrade git+https://github.com/<org>/<repo>.git`.
- **From a checkout:** `git pull`, reinstall dependencies if the lockfile
  changed (`python -m pip install -r requirements.txt`), then restart.
- **One-command installers:** re-run `install.cmd` / `install.ps1` /
  `install.sh`; each reinstalls into the same venv (`%USERPROFILE%\.relay`
  on Windows, `~/.relay` elsewhere) and refreshes the `relay` entry point.

Your configuration (`.env`), learned state (`platform.db`), and setup
marker live in your user-data directory (or next to the checkout when
running from source), **not** inside the installed package, so upgrading
never touches them. If a release needs a schema or config change it is
applied on startup or via `relay migrate`. To revert an upgrade, see
[docs/rollback-procedure.md](docs/rollback-procedure.md).

### Uninstalling

- **From PyPI/GitHub:** `python -m pip uninstall relay`.
- **One-command installers:** remove the venv and the PATH entry the
  installer added — on Windows `%USERPROFILE%\.relay\Scripts` (remove it
  from your user PATH under System Properties → Environment Variables); on
  macOS/Linux remove `~/.relay` and the `~/.local/bin/relay` symlink (or
  the `export PATH="$HOME/.relay/bin:$PATH"` line in your shell profile).

Uninstalling the package never deletes your data. To remove it too,
delete the user-data directory shown on the TUI Dashboard (`Env file` /
`State dir` tiles): `%LOCALAPPDATA%\relay` on Windows,
`~/.local/share/relay` on Linux, `~/Library/Application Support/relay` on
macOS (or the `.env` / `.relay` files next to a source checkout). Keys
stored in the OS keyring (service name `relay`) are separate — remove them
with `relay provider keys remove <provider>` or your operating system's
credential manager.

First launch detects a missing or incomplete configuration and walks you
through provider setup. After setup completes, running `relay` opens the
terminal interface with an embedded API server, so any OpenAI-compatible
client (Cline, OpenCode, Continue, …) can point at
`http://127.0.0.1:8000/v1` while the TUI is open. Run `relay serve` for
the pre-TUI behavior (headless server only), and `relay tui` to force
the terminal interface.

Startup behavior at a glance:

| Command | Behavior |
| --- | --- |
| `relay` (first run) | Opens the interactive setup wizard |
| `relay` (configured) | Opens the terminal interface (TUI) with an embedded API server |
| `relay tui` | Forces the terminal interface |
| `relay serve` | Runs the API server only (no UI) |
| `relay setup` | (Re)runs the setup wizard; on success hands off to the TUI |

The terminal interface needs an interactive terminal. When it is launched
without one (a scheduled task, a service manager, or redirected stdout),
Relay prints guidance and exits cleanly instead of crashing — use
`relay serve` for headless operation. On Windows, the TUI runs in
Windows Terminal (ConPTY), PowerShell, VS Code, or a conhost console; see
[docs/tui-guide.md](docs/tui-guide.md) for full details.

### Terminal interface

`relay` (and `relay tui`) opens the terminal UI:

- `1` Dashboard — server state, provider/model availability, recent activity
- `2` Chat — talk to your configured providers
- `3` Models — availability and priority controls
- `4` Providers — keys, scanning, and setup
- `5` Configuration — routing, failover, server settings
- `6` Applications — client activity and endpoint/auth status
- `7` Diagnostics — operations tail, health, and export
- `q` quit

While the Chat message box (or any text input / picker) is focused, the
plain digit keys are typed normally instead of switching tabs — use the
`Ctrl+1`–`Ctrl+7` variants, which work from anywhere, even mid-edit; see
[docs/tui-guide.md](docs/tui-guide.md) for the full keymap.

Set `RELAY_TUI_NO_EMBED=1` to run the TUI against a separately managed
`relay serve` instead of the embedded server. A complete walkthrough of
the interface, the Configuration and Applications panels, diagnostics
export, and Windows requirements lives in
[docs/tui-guide.md](docs/tui-guide.md).

> Publishing to PyPI and Windows package managers (winget/choco) is
> planned; until then use the commands above.

## Development

```bash
# Create the virtual environment and install dependencies
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# Run the test suite
.venv\Scripts\python -m pytest tests -q

# Byte-compile everything (import-time sanity)
.venv\Scripts\python -m compileall -q app tests
```

The test suite is self-contained: provider loading is disabled for the
whole test session (see `tests/conftest.py`) so no test touches the
network.

## Design principles

1. Relay owns business logic; API routers stay thin.
2. Services perform work; providers only know how to talk to providers.
3. Never ship a feature that cannot be tested immediately.
