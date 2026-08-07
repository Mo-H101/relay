# Relay Terminal Interface Guide

Relay ships a full terminal interface (TUI) for operating and configuring
the gateway without a browser. This guide covers startup behavior, the
seven screens, configuration and client-activity panels, diagnostics
export, and the Windows requirements.

---

## 1. Startup behavior

The `relay` command dispatches by configuration state:

| Command | Behavior |
| --- | --- |
| `relay` (no configuration) | Opens the interactive setup wizard. |
| `relay` (configured) | Opens the terminal interface with an embedded API server. |
| `relay tui` | Forces the terminal interface (same as a configured `relay`). |
| `relay serve` | Runs the API server only — no UI, no wizard. |
| `relay setup` | (Re)runs the setup wizard; on success hands off straight to the TUI. |

First launch detects a missing or incomplete configuration and walks you
through provider setup (API keys, model priority, availability scans).
After setup completes, running `relay` opens the terminal interface with
an embedded API server on `RELAY_HOST`/`RELAY_PORT`, so any
OpenAI-compatible client (Cline, OpenCode, Continue, …) can point at
`http://127.0.0.1:8000/v1` while the TUI is open.

- `relay serve` preserves the pre-TUI headless behavior exactly.
- `RELAY_TUI_NO_EMBED=1` runs the TUI against a separately managed
  `relay serve` instead of the embedded server (for service-manager
  setups).

## 2. Terminal interface requirements

The TUI is a real interactive terminal application. It needs an
interactive stdin/stdout:

- **POSIX / macOS / Linux:** any real shell terminal.
- **Windows:** Windows Terminal (ConPTY), PowerShell, a conhost console,
  or VS Code's integrated terminal. Windows Terminal and VS Code are
  detected via the `WT_SESSION` / `TERM_PROGRAM` environment variables;
  a bare console is verified through the standard output handle.

When Relay is launched without an interactive terminal (a scheduled task,
a service manager, redirected stdout, or a non-console Windows context),
it **prints guidance and exits cleanly** instead of crashing:

```
Relay's terminal interface needs an interactive terminal.
Reason: standard output is not an interactive terminal.

  - Run 'relay' (or 'relay tui') from a real terminal, such as
    Windows Terminal, PowerShell, or a POSIX shell.
  - To run Relay without a UI, use 'relay serve' to start only
    the API server.
```

For headless operation use `relay serve`; the API is fully usable
without the TUI. The interactive setup wizard also requires a terminal —
configure providers from a real terminal, or edit `.env` directly and
run `relay serve`.

## 3. The seven screens

`1`–`7` switch tabs; `q` (or `Ctrl+C`) quits. While a text input or
the model picker holds focus the plain digit keys are typed normally, so
navigate with the `Ctrl` variants — `Ctrl+1`–`Ctrl+7` switch tabs and
`Ctrl+Q` quits, and they work from anywhere, even mid-edit. `Escape`
returns to the Dashboard from any screen (the Chat tab also has a
visible "Back to Dashboard" button).

| Key | Screen | What it shows |
| --- | --- | --- |
| `1` / `Ctrl+1` | Dashboard | Server state, provider/model availability, recent activity, persistence status. |
| `2` / `Ctrl+2` | Chat | Random or specific-model chat with streaming, plus an inline model availability test. |
| `3` / `Ctrl+3` | Models | Model availability and priority controls. |
| `4` / `Ctrl+4` | Providers | Provider keys, scanning, and setup. |
| `5` / `Ctrl+5` | Configuration | Routing, failover, and server settings. |
| `6` / `Ctrl+6` | Applications | Client activity and endpoint/auth status. |
| `7` / `Ctrl+7` | Diagnostics | Operations tail, health, and export. |

Screens are kept in memory across tab switches, so state such as the chat
transcript and form edits survives moving between tabs and refreshes
automatically when you return.

## 4. Configuration screen (tab 5)

The Configuration panel is a live settings form split into three groups:

- **Routing (`TASK_*`) and failover/retry — applied live.** Edits are
  written to `.env` through the single config writer, validated with a
  dry-run reload, and applied in-process. The status line reports which
  fields were applied; failures restore the previous values.
- **Restart-required (read-only).** Server bind (`RELAY_HOST`,
  `RELAY_PORT`), persistence, logging, and the LM Studio URL cannot
  change without a restart and are shown read-only with a warning.
- **Informational (read-only).** The preferred provider (highest runtime
  priority) is shown without a silent behavior change.

API keys are **never** shown here — they are managed on the Providers
screen, password-masked and validated before anything is persisted.

## 5. Applications screen (tab 6)

The Applications panel shows which clients are talking to Relay:

- **Auth status** — whether `RELAY_API_KEY` auth is enabled, plus
  cumulative authenticated/failed requests by method and reason.
- **Endpoint status** — rolling request/success/failure counts.
- **Client activity table** — one row per (client, route): requests,
  successes, failures, auth scheme, last seen.

Clients are bucketed by user-agent heuristics into `Cline`, `OpenCode`,
`Continue`, and `Other`. The tracker is **metadata-only**: it never
stores user agents, `Authorization` header values, request bodies, or
messages — only the bucket label, route, status, and auth-scheme label.

## 6. Diagnostics screen (tab 7)

- **Summary** — ops-window totals, success rate, chats, auth failures,
  persistence state.
- **Operations tail** — metadata-only per-request events (age, kind,
  method, route, status, latency, provider, model).
- **File log tail** — the JSON log (`LOG_FILE`) is tailed and redacted
  before display; secret-shaped `data` keys are masked.
- **Provider health** — per-model health deep view (health, snapshot,
  latency, learned marks) and an explicit "Test connection" probe.
- **Export snapshot** — writes the redacted diagnostics snapshot to a
  file (default `<env dir>/relay-diagnostics-<timestamp>.json`). Every
  export passes through the redaction layer before its atomic file write,
  so API keys, `Authorization` headers, and message content can never
  appear in an export.

## 7. Windows requirements

- **Supported:** Windows 10/11 with Windows Terminal, PowerShell, or a
  conhost console; VS Code integrated terminal works too.
- **ConPTY:** Windows Terminal and VS Code provide a ConPTY and are
  auto-detected. A plain conhost console is also supported via the
  console output handle.
- **Not supported (graceful degradation):** non-console contexts
  (pythonw, services, scheduled tasks) and redirected stdout. Relay
  detects these and prints guidance instead of a broken screen — use
  `relay serve` for headless service operation.
