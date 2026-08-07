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
   `GET /diagnostics` and `GET /metrics`; save the `platform.db` file
   (and its `-wal`/`-shm` sidecars) to a timestamped backup — do **not**
   delete it, you may need it.
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

Since P6.1, learned state lives in the consolidated `platform.db` at
`PERSISTENCE_PATH` (see [platform-db-schema.md](platform-db-schema.md)).
Legacy `relay_keys.db` / `relay_state.db` / `availability.json` remain on
disk, inert, as the migration rollback target.

To reset the consolidated database:

1. Stop the server (graceful: `SIGTERM`; a final flush runs on shutdown).
2. Restore `platform.db` (and sidecars) from the backup in step 2, or use
   `relay migrate --rollback <timestamp|last>` to restore the legacy
   sources and remove `platform.db` (relies on the backup under
   `state_dir/backups/` made by the migration). A manual restore always
   works too, because the migration copies rather than moves the sources.
3. After `--rollback`, the runtime's legacy-unmigrated guard re-engages:
   it refuses to create a fresh `platform.db` until `relay migrate` is run
   again.
4. Start the server and confirm `/diagnostics` shows the expected
   `learned_health` / telemetry / quality state.

## 6. Restart and verify

```bash
relay serve
```

(To bind all interfaces, set `RELAY_HOST=0.0.0.0` before starting; see
[docs/deployment.md](deployment.md).)

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
