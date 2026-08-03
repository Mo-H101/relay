# Windows TUI Smoke Checklist (manual, P2e)

Run these on a real Windows machine (Windows 10/11). Each check is a
pass/fail; record results in the project log or a PR review comment.
Automated coverage for the pieces below lives in
`tests/test_ui_app.py` (pilot), `tests/test_ui_terminal.py` (preflight),
and `tests/test_ui_boundary.py` (isolation).

Prereq: a configured `.env` with at least one usable provider
(`relay setup` from a real terminal first).

## 1. Startup behavior

| # | Check | Expected | Result |
| --- | --- | --- | --- |
| 1.1 | On a clean machine run `relay` (no config). | Opens the interactive setup wizard. | ☐ |
| 1.2 | After setup, run `relay`. | TUI opens with embedded API server; `curl http://127.0.0.1:8000/health` returns `ok`. | ☐ |
| 1.3 | Run `relay serve`. | API server only, no UI; TUI never appears. | ☐ |
| 1.4 | Run `relay tui`. | TUI forced open. | ☐ |

## 2. Non-interactive / ConPTY degradation

| # | Check | Expected | Result |
| --- | --- | --- | --- |
| 2.1 | Run `relay` with stdout redirected: `relay > out.txt`. | Guidance printed (mentions `relay serve`), exit code 0, no traceback, `out.txt` has no escape-sequence garbage. | ☐ |
| 2.2 | Run `relay tui` from a scheduled-task / service-manager style context (stdin+stdout not a console). | Same clean guidance, no crash. | ☐ |
| 2.3 | Run from Windows Terminal and from VS Code integrated terminal. | TUI renders correctly in both (ConPTY detection). | ☐ |
| 2.4 | Run from a conhost console (classic cmd). | TUI renders correctly (console-handle detection). | ☐ |
| 2.5 | With `RELAY_TUI_NO_EMBED=1`, run `relay`. | TUI opens without an embedded server; API reachable only via a separate `relay serve`. | ☐ |

## 3. All seven screens

| # | Check | Expected | Result |
| --- | --- | --- | --- |
| 3.1 | Press `1`–`7`. | Each tab opens: Dashboard, Chat, Models, Providers, Configuration, Applications, Diagnostics. | ☐ |
| 3.2 | Press `q`. | TUI exits cleanly; embedded server stops (no orphan process on `netstat -ano | findstr 8000`). | ☐ |

## 4. Configuration screen (tab 5)

| # | Check | Expected | Result |
| --- | --- | --- | --- |
| 4.1 | Edit a live field (e.g. `MAX_RETRIES`), Save. | Status line reports applied fields; no restart needed. | ☐ |
| 4.2 | `RELAY_HOST`/`RELAY_PORT`/`LOG_LEVEL` are read-only. | Form refuses edits with a restart-required note. | ☐ |
| 4.3 | API keys never appear on this tab. | Only the Providers tab manages keys; nothing unmasked anywhere. | ☐ |

## 5. Applications (tab 6) and Diagnostics (tab 7)

| # | Check | Expected | Result |
| --- | --- | --- | --- |
| 5.1 | Send a few requests from Cline/OpenCode/Continue/curl while the TUI runs, then open tab 6. | Client rows appear bucketed by client; auth scheme column shows `bearer`/`none`. | ☐ |
| 5.2 | With `Authorization: Bearer <sk-…>` on a request. | No token substring appears in the Applications table or anywhere in the TUI. | ☐ |
| 5.3 | Open tab 7, click Export snapshot with the default path. | `relay-diagnostics-<ts>.json` written; file contains no `sk-`, no `Bearer `, no `Authorization` values, `has_api_key` is boolean. | ☐ |
| 5.4 | Per-provider Test connection. | Runs off the UI thread (TUI stays responsive), result in the status line. | ☐ |

## 6. Docs

| # | Check | Expected | Result |
| --- | --- | --- | --- |
| 6.1 | `docs/tui-guide.md` matches observed behavior for startup, tabs, and Windows guidance. | No drift. | ☐ |
