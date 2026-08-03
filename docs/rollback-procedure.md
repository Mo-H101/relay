# Rollback Procedure

How to revert a Relay deployment to a previous known-good state. Assume
the rollback target is the last version whose RC checklist was green
(see [release-candidate-checklist.md](release-candidate-checklist.md)).

## 1. Decide scope

- **Roll back code** — a new release regressed behavior.
- **Roll back config** — a `.env` change broke providers, routing, or auth.
- **Roll back data** — learned state became corrupted or undesirable.

These can be done independently.

## 2. Prepare

1. Confirm the exact running version and the version you are rolling to:
   `git log --oneline -5` (or the deployed artifact tag).
2. Capture the current state for post-mortem:
   `GET /diagnostics` and `GET /metrics`; save the `relay_state.db` file
   to a timestamped backup (do **not** delete it — you may need it).
3. Verify the target version's test state before switching:
   `python -m pytest tests -q`.

## 3. Roll back the code

1. Check out / deploy the previous artifact:
   ```bash
   git checkout <previous-tag-or-sha>
   ```
2. Reinstall dependencies if the lockfile changed.
3. Run the full suite and the offline RC suite:
   ```bash
   python -m pytest tests -q
   python -m pytest tests/test_rc_validation.py -q
   ```

## 4. Roll back the config

1. Restore the previous `.env` from backup (never commit keys).
2. If auth was changed, keep `RELAY_API_KEY` consistent across
   proxies/clients so nothing gets locked out.
3. Use the in-place reload to validate without a restart:
   `POST /admin/reload?dry_run=true`, then `POST /admin/reload`.

## 5. Roll back learned state

Learned state lives in the SQLite DB at `PERSISTENCE_PATH`. To reset it:

1. Stop the server (graceful: `SIGTERM`; a final flush runs on shutdown).
2. Replace `relay_state.db` with the backup from step 2, or delete it to
   start clean (Relay will recreate it and continue in memory until the
   first flush).
3. Start the server and confirm `/diagnostics` shows the expected
   `learned_health` / telemetry / quality state.

## 6. Restart and verify

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Verification, in order:

1. `GET /health` → `{"status": "ok"}`
2. `GET /v1/models` with the API key → provider model list
3. One non-stream and one streamed completion via the OpenAI SDK
   (or `python tests/run_live_smoke.py`)
4. `GET /diagnostics` — persistence `available`, telemetry counters
   increasing, no unexpected degraded providers

## 7. Communicate

- Note the rollback (version, time, trigger, what was restored) in the
  release notes / runbook.
- Do not redeploy the rolled-back version until the root cause is
  understood; a flapping rollback indicates an untested config or an
  unhandled `known-limitations.md` case.

## Ordering rules

- Roll back **config first** when the trigger was config: it is the
  fastest and least risky change.
- Roll back **data last**: only after code/config are at the target, so
  the state file you restore matches the software reading it.
- If a provider key is dead (e.g. the RC-time OpenAI quota failure),
  rotating the key is a config change — treat it as a rollback of that
  provider's config, not of the deployment.
