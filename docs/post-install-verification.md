# Post-Install Verification Checklist

Verify a freshly installed Relay exactly as an end user would. Run this
against the **published artifact** (or the release bundle) on a clean
machine/venv, not against a source checkout.

**Version under test:** ________________
**Install method:**  [ ] PyPI  [ ] release bundle  [ ] source (`pip install .`)
**OS:** ________________
**Date:** ________________
**Tester:** ________________

## 1. Install

- [ ] Installation completed without errors.
- [ ] `relay --version` prints the expected version.
      Expected: `relay <version>`   Actual: ______________
- [ ] `relay --help` lists the full subcommand surface:
      `setup, tui, serve, keys, provider, migrate, events, conversations,
      apps, config`.

## 2. Health

Start a server (in one terminal: `relay serve`, or run `relay` for the TUI
which embeds an API server), then:

- [ ] `curl -s http://127.0.0.1:8000/health` returns HTTP 200 and a JSON
      body with a `"status"` field (e.g. `"degraded"` when no providers are
      configured yet).
- [ ] `curl -s http://127.0.0.1:8000/v1/models` returns HTTP 200 and a JSON
      `{"object": "list", "data": [...]}` payload (may be empty until
      providers are configured).

## 3. Configuration

- [ ] `relay setup` runs the interactive wizard (or `relay config show`
      prints effective settings with secrets masked).
- [ ] After configuring at least one provider, `/v1/models` lists it.
- [ ] Config file / env vars are honored (e.g. `RELAY_HOST`, `RELAY_PORT`,
      `RELAY_API_KEY`).

## 4. Smoke requests (requires a live provider)

With a configured, working provider key:

- [ ] Non-streaming: `POST /v1/chat/completions` with `{"model": <model>,
      "messages": [{"role":"user","content":"ping"}]}` returns a completion.
- [ ] Streaming: same request with `"stream": true` returns SSE chunks
      ending in `[DONE]`.
- [ ] Native: `POST /chat` returns a completion.

## 5. Auth (recommended profile)

With `RELAY_API_KEY` set:

- [ ] A request without the key returns 401/403.
- [ ] A request with the bootstrap key returns 200.
- [ ] (If `RELAY_AUTH_STORE=true`) a scoped key with `chat,v1` scopes can
      chat and is denied on `/admin`.

## 6. Upgrade path (when upgrading an existing install)

- [ ] Existing state files (legacy or `platform.db`) are still readable;
      `relay migrate --dry-run` reports a correct plan before any `--yes`.
- [ ] Downgrade/rollback is possible per `docs/rollback-procedure.md`.

## Sign-off

- [ ] All items PASS, or every failure is recorded in a bug report using
      [BUG_REPORT_TEMPLATE.md](BUG_REPORT_TEMPLATE.md) with the version and
      OS captured above.
