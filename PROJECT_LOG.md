# Relay Project Log

---

# Milestone 1 — Project Foundation

## Status
✅ Complete

## Completed

- Created Relay project structure
- Added configuration system
- Created Provider abstraction
- Implemented NVIDIA provider
- Implemented ProviderManager
- Created `/providers` endpoint
- Relay now owns ProviderManager

## Notes

- Relay is the application's main facade.
- Providers are registered during Relay startup.
- API layer should remain as thin as possible.

---

# Milestone 2 — Health System

## Status
✅ Complete

## Completed

- Added HealthChecker service
- Added ProviderHealth data model
- Added `/health` endpoint
- Relay now owns HealthChecker
- Moved health logic from API into Relay

## Notes

Current health check is simulated.

It measures latency locally and always reports:

- healthy

Next milestone will replace the simulated check with a real HTTP request to NVIDIA.

---

# Platform P0 — Packaging & distribution

## Status
✅ Complete

## Completed

- Versioned the package (`0.1.0`) via `app/__version__.py` (PEP 440),
  re-exported from `app/__init__.py`.
- Added `pyproject.toml` (setuptools, dynamic version, `relay` console
  script, `app*` package discovery). Existing `python -m app.cli` usage is
  unchanged.
- Added first-run setup-state mechanism (`app/services/setup_state.py`)
  with three states — `not_configured`, `configured`, `incomplete` —
  stored in `<state_dir>/state.json` (atomic write), independent of `.env`
  presence.
- `app/core/config.py`: `RELAY_ENV_FILE` override, cwd-first `.env`
  resolution, `RELAY_STATE_DIR`, `RELAY_HOST`, `RELAY_PORT`.
- `app/cli.py`: `--version`, `relay setup`, and no-args dispatch
  (configured → start server; otherwise → first-run/setup). Setup now
  prints `Installation complete. Type 'relay' to start Relay.`
- One-command installers `install.ps1` and `install.sh`.
- `.env.example`, `requirements-dev.txt`, `.gitignore`, README and
  configuration docs updated.
- P0 tests (`tests/test_packaging.py`, 15 tests): packaging metadata, CLI
  entry point, version command, first-run detection, configured startup
  path, and a wheel-build + installed-console-script smoke test.

## Notes

- Full suite after P0: **836 passed, 5 skipped** (821 baseline + 15 new).
- PyPI publishing and winget/choco are deferred to a later phase; the
  installers and console entry point are designed for that path.

---

# Architecture Principles

1. Relay owns business logic.
2. API routers remain thin.
3. Services perform work.
4. Providers only know how to communicate with providers.
5. Never implement a feature that cannot be tested immediately.

---

# Next Milestone

- Real HTTP health check