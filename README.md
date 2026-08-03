# Relay

A self-hosted LLM routing proxy. Relay sits between your application and
one or more LLM providers, picks the best provider/model for each request,
fails over across models and providers when one is unavailable, and learns
from real request outcomes to keep routing smart over time.

- OpenAI-compatible endpoint (`/v1/chat/completions`) plus a native
  `/chat` endpoint.
- Multiple backends: NVIDIA NIM, OpenAI, and local LM Studio (OpenAI-
  compatible local servers work through the LM Studio client).
- Health-aware routing, task-specific routing, adaptive EWMA reliability/
  latency signals, quality-feedback routing, and an explainable decision
  engine.
- Optional SQLite write-behind persistence so learned state survives
  restarts.
- Optional API-key authentication, optional Prometheus metrics, optional
  outbound proxy support, and hot configuration reload.

## Quick start

```bash
# 1. Configure providers (interactive: API keys, model priority, task routing)
python -m app.cli setup

# 2. Run the server
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Send a chat:

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "meta/llama-3.1-8b-instruct", "messages": [{"role": "user", "content": "Hello"}]}'
```

Interactive docs: http://localhost:8000/docs (Swagger UI) or
http://localhost:8000/redoc (ReDoc).

## Documentation

- [Architecture](docs/architecture.md) — components, layering, request flow.
- [Configuration](docs/configuration.md) — every environment variable.
- [Routing & decisions](docs/routing-decisions.md) — how a provider/model is chosen.
- [Deployment](docs/deployment.md) — production hardening, persistence, auth, proxies.
- [Troubleshooting](docs/troubleshooting.md) — common problems and diagnostics.
- [Hardening audit report](docs/audit-report.md) — findings, fixes, remaining risks.
- [v1.0.0 readiness report](docs/v1.0.0-readiness-report.md) — release checklist, verification evidence, remaining risks, required actions.
- [UX validation guide](docs/ux-validation-guide.md) — Phase 8 manual test checklist for first-time users.
- [Platform analysis](docs/platform-architecture-report.md) — Phase 9 current-architecture report.
- [Platform missing components](docs/platform-missing-components-report.md) — Phase 9 gap analysis vs. the target platform.
- [Platform implementation roadmap](docs/platform-implementation-roadmap.md) — Phase 9 phased plan (P0–P8).
- [Platform recommended order](docs/platform-recommended-order.md) — Phase 9 sequencing rationale.

## Endpoints

| Method | Path | Description | Public |
| --- | --- | --- | --- |
| GET | `/` | Service banner | yes |
| GET | `/health` | Aggregate liveness (`ok`/`degraded`/`unavailable`), no internals | yes |
| GET | `/providers` | Registered providers, models, priority | no |
| GET | `/health/deep` | Deep per-model health report | no |
| GET | `/provider` | Provider Relay would select next | no |
| GET | `/decision/explain` | Why the last ranking came out as it did | no |
| POST | `/chat` | Native chat (task-aware) | no |
| GET | `/diagnostics` | Operational snapshot: health, telemetry, decision, persistence | no |
| POST | `/v1/chat/completions` | OpenAI-compatible chat (streaming supported) | no |
| GET | `/v1/models` | OpenAI-compatible model list | no |
| POST | `/feedback` | Metadata-only quality rating for a (provider, model) | no |
| POST | `/admin/reload` | Hot-reload configuration from `.env` | no |
| GET | `/metrics` | Prometheus text exposition | no |
| GET | `/docs`, `/redoc`, `/openapi.json` | API documentation | no* |

`*` The documentation routes are gated by the same API-key dependency as
every other route, so they are only reachable without a key when
`RELAY_API_KEY` is unset.

Authentication, when `RELAY_API_KEY` is set, uses either an
`Authorization: Bearer <key>` header or an `X-Relay-API-Key: <key>`
header. Requests without a valid key receive `401 Unauthorized`.

## Configuration

All configuration comes from environment variables or a `.env` file at
the project root. See [docs/configuration.md](docs/configuration.md) for
the full reference. The most common settings:

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
PERSISTENCE_PATH=./relay_state.db
```

The flags form a dependency chain: telemetry feeds adaptive learning and
health feedback; health feedback makes health-aware routing meaningful;
persistence keeps all learned state across restarts. `HEALTH_REFRESH_ENABLED`
is intentionally left off here — it runs a background prober that needs live
network access to every provider endpoint. See
[docs/configuration.md](docs/configuration.md) for the full reference and
dependency notes.

## Installation

Install Relay in a virtual environment, then start it by typing `relay`:

```bash
# From PyPI (once published)
pip install relay

# From GitHub (one command)
pip install git+https://github.com/<org>/<repo>.git

# One-command installers (from a checkout)
#   Windows PowerShell:
.\install.ps1
#   macOS / Linux:
./install.sh
```

After a successful install, the terminal prints:

    Installation complete. Type 'relay' to start Relay.

First launch detects a missing or incomplete configuration and walks you
through provider setup. After setup completes, running `relay` starts the
server, and any OpenAI-compatible client (Cline, OpenCode, Continue, …)
can point at `http://127.0.0.1:8000/v1`.

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
