# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Nothing yet; post-v1.0.0 notes land here.

## [1.0.0rc1] - 2026-08-07

> Release candidate. The package version is `1.0.0rc1`; the entry below
> documents the full v1.0.0 feature set as it ships in this candidate.
> Final `1.0.0` punctuation lands here after the release gate passes.

### Project continuity (new in v1.0.0)

- New **project continuity** capability (P9a–P9e): durable conversation
  storage, context management, handoff envelopes, and crash recovery. Relay
  now delivers the "no progress lost = committed turns" contract across
  provider switches and process restarts.
- Continuity is an **opt-in** capability, disabled by default
  (`CONTINUITY_ENABLED=false`). When enabled it is additive: clients may send
  `X-Relay-Conversation-Id` / `X-Relay-Project-Id` headers to opt a
  conversation into durable, metadata-only storage and resume support.
- Opt-in clients use a single-use **resume token** after a restart; the
  server replays a durable, per-conversation sequence so a client never
  re-executes acknowledged work. Replays are bounded
  (`MAX_RESUME_REPLAYS`, default 3) and the resume path fails closed if the
  replay tracker cannot be persisted.
- Nothing content-shaped is ever persisted: prompts, responses, and API keys
  remain strictly ephemeral (memory contract §P0), enforced by negative
  tests across exports, events, metrics, and log payloads.

### Providers

- **Six supported providers** at v1.0.0: NVIDIA, OpenAI, Anthropic, Google
  Gemini, LM Studio, and Ollama.
- OpenRouter and Groq are **not** included in v1.0.0; their reserved config
  keys were removed. They remain a candidate for a post-v1 release (decision
  D1).
- Anthropic and Google Gemini are wired runtime providers; all six are
  conformance-tested, and NVIDIA/OpenAI/LM Studio are live-validated.

### Command-line interface

- The TUI supersedes the originally planned `status`, `providers`, `models`,
  `routing`, `logs`, and `test` subcommands; `relay events` is the log
  surface (decision D2). The shipped CLI is `setup`, `tui`, `serve`, `keys`,
  `provider keys`, `migrate`, `events`, `apps`, `config`.

### Security

- Deployed auth: `RELAY_API_KEY` (constant-time bootstrap) plus optional
  store-backed per-app scoped keys (`RELAY_AUTH_STORE=true`); scoped keys
  deny `/admin/*`.
- Retry hardening (opt-in profile): `RETRY_HONOR_RETRY_AFTER` with bounded
  backoff and an overall request budget.
- At-rest provider secrets via the OS keyring (`RELAY_KEYRING=true`), with a
  configurable headless backend.
- `FORBIDDEN_KEYS` backstop extended to catch content-shaped variants
  (`prompt_text`, `user_message`, `secret_value`, `model_response`).

### Platform & operations

- Declarative settings registry (P7) with a live-reload allowlist; the TUI
  Configuration panel derives entirely from the registry.
- Platform database schema v8 with idempotent, additive migrations
  (`relay migrate`) and backup/rollback.
- Metrics, `/v1/health` feedback, adaptive health-aware routing, and the
  `relay events` / TUI operator surfaces.

### Notes

- The full regression baseline at the v1.0.0 tag is **3041 passed / 20
  skipped**, with the adversarial (80) and continuity-simulation (6) suites
  green.
- The project is released source-available under the MIT License with a
  single-process SQLite storage model; see `docs/deployment.md` and
  `docs/known-limitations.md` for the supported operating envelope.
