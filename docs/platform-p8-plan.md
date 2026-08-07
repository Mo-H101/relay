# Relay — P8 Client Integration Guides Plan

Date: 2026-08-06 · Planning only — no code changed.

Status: awaiting approval. Plan document stays uncommitted per workflow.

Scope: P8 per `docs/platform-implementation-roadmap.md` — client setup guides
(requirement 19): Cline, OpenCode, Continue, plus a generic OpenAI-compatible
section. Covers local deployment workflow, authentication/key configuration,
and troubleshooting. The quality-gate half of P8 (CI) already landed in P6.4
(`.github/workflows/ci.yml`, `[tool.pytest.ini_options]`) and is out of scope.

Explicitly **not** in scope: any runtime behavior change, API contract
changes, schema/persistence changes, `PROJECT_LOG.md`, `tests/`, new
dependencies, CI changes, and the pre-v1.0 decisions listed in §11 (recorded
here, resolved elsewhere).

---

## 1. Current P8 state

- **CI / quality gate — done (P6.4).** `.github/workflows/ci.yml` runs
  compileall + full suite on ubuntu/windows, packaging smoke, on every push.
  `[tool.pytest.ini_options]` present. The roadmap's "one command runs the
  suite; pipeline green" half of P8 is satisfied.
- **Client setup guides — missing (the entire remaining P8 deliverable).**
  Requirement 19 ("OpenAI-compatible clients, minimal setup") is unmet: there
  are no per-client setup guides. The `docs/platform-missing-components-report.md`
  gap row is unchanged since it was written (`| Client integration guides | ❌ |`).

### What already exists (to build on, not duplicate)

| Artifact | Covers | Gap vs. req 19 |
| --- | --- | --- |
| `README.md` §Quick start (lines ~99–104) | Two-line "point your client at `http://relay-host:8000/v1`" wiring for "OpenAI SDK, Cline, OpenCode, …" | No per-client steps, no key flow, no troubleshooting |
| `docs/deployment.md` §"Cloud gateway configuration" (lines ~24–44) | `base_url` + `api_key` snippet, recommended production profile, hardening checklist | Deployment-oriented; not a step-by-step client setup guide |
| `docs/ux-validation-guide.md` §7 (Cline) and §8 (OpenCode) | Manual validation walkthroughs (settings screens, connection check) | Validation procedures, not setup docs; **no Continue section** |
| `docs/tui-guide.md` (lines ~26, ~105) | Mentions Cline/OpenCode/Continue can point at Relay | No setup |
| `tests/test_client_detection.py`, `test_auth_enforced`, `test_openai_error_shape` | Contract the client-facing behavior the guides document | — (verification hooks) |

Nothing documents: Continue setup, the generic OpenAI-compatible case,
per-app key creation for a client (`relay keys add --label <client>`,
`RELAY_AUTH_STORE=true`, scopes), or common client failures.

---

## 2. Exact documentation gaps

1. **No Cline setup guide** — only a validation walkthrough (`ux-validation-guide.md` §7).
2. **No OpenCode setup guide** — only a validation walkthrough (§8).
3. **No Continue setup guide** — no content at all (Continue is only
   mentioned as a UA bucket and a roadmap requirement).
4. **No generic OpenAI-compatible section** — no guidance for any other
   OpenAI-compatible client (other SDKs, curl, scripts, custom tools).
5. **No local deployment workflow** — nothing explains how to start Relay
   locally (`relay serve` / `python -m uvicorn app.main:app`, default
   `127.0.0.1:8000`), reach it from a client on the same machine, or from
   another machine, or from a container/remote host (TLS via reverse proxy).
6. **No authentication/key configuration guide** — nothing documents the two
   tiers (`RELAY_API_KEY` bootstrap vs. store-backed per-app keys with
   `RELAY_AUTH_STORE=true`), the header contract (`Authorization: Bearer` /
   `X-Relay-API-Key`), scope enforcement (`chat,v1` for `/v1`+`/chat`),
   key lifecycle (`relay keys add/list/remove/rotate/prune/test`), or the
   insecure-by-default warning (empty `RELAY_API_KEY` = auth off).
7. **No troubleshooting/common-failures section** — nothing covers: 401
   auth mismatch, `model not found` (id vs. `/v1/models`), client traffic
   routing to the client's own default provider instead of Relay,
   stream errors, proxy/TLS issues, single-process scaling constraint.
8. **No cross-links** — README quick-start and `deployment.md` mention the
   clients but do not link to dedicated guides.

---

## 3. Files expected to change

All changes are **documentation only**.

New files (the guides):

- `docs/clients/index.md` — entry point: which guide for which tool, quick
  overview, shared prerequisites, link out to the four guides.
- `docs/clients/cline.md` — Cline (VS Code extension).
- `docs/clients/opencode.md` — OpenCode (terminal client).
- `docs/clients/continue.md` — Continue (VS Code / JetBrains extension).
- `docs/clients/openai-compatible.md` — generic OpenAI-compatible client
  (OpenAI SDK, curl, other tools).

Modified files (cross-links only, no content rewrite):

- `README.md` — add one "Client setup guides" link line in Quick start
  pointing at `docs/clients/index.md`.
- `docs/deployment.md` — add one link line from the "Client wiring" block to
  `docs/clients/index.md`.

---

## 4. Files that must remain untouched

- `app/` — all runtime code (no behavior change).
- `tests/` — no new or edited tests (docs-only phase; the existing 2055-test
  suite is the regression gate).
- `pyproject.toml`, `requirements.txt`, `requirements-dev.txt` — no new deps.
- `.env`, `.env.example` — no key/example changes.
- `.github/workflows/ci.yml` — CI already satisfies the P8 quality-gate half.
- `PROJECT_LOG.md` — untouched per constraint (updated only after commit
  approval, per workflow).
- Every other `docs/*.md` (including `docs/ux-validation-guide.md` — kept as
  the manual validation reference; the new guides do not replace it).
- `docs/platform-*.md` plan documents — remain uncommitted per workflow.

---

## 5. Documentation structure

`docs/clients/` groups the four guides under one index. Each guide follows a
common template so a user gets the same shape for every client.

### Shared prerequisites (index.md)

- How to install Relay (`pip install` / `install.*`), start it
  (`relay serve`, or `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`),
  defaults (`127.0.0.1:8000`, `RELAY_HOST`/`RELAY_PORT`).
- At least one provider enabled + credentialed (link `configuration.md`,
  `relay setup`).
- Auth basics + the security warning (see §6).

### Common template (every client guide)

1. **What this guide covers** — client name, version assumed, where it runs.
2. **Prerequisites** — Relay running locally (or deployed URL), model
   available via `/v1/models`.
3. **Create a key for this client** — two options:
   - Single bootstrap key: `RELAY_API_KEY` (simplest, shared).
   - Per-app scoped key (recommended): `RELAY_AUTH_STORE=true` +
     `relay keys add --label <client> --scopes chat,v1`; the raw key is
     printed exactly once — store it in the client's config now.
4. **Point the client at Relay** — exact config snippet for that client
   (base URL `http://127.0.0.1:8000/v1` or the deployed URL; api_key field;
   model id chosen from `/v1/models`).
5. **Verify** — send one test message through the client; expected result.
6. **Troubleshooting** — client-specific common failures from the shared
   list (§6), mapped to fixes.
7. **Cleanup / rotate** — `relay keys rotate` / `remove`, `relay events`.

### Generic OpenAI-compatible guide

Same shape but tool-agnostic: OpenAI SDK examples (Python/JS), curl
examples, `base_url`/`api_key` contract, streaming/tools passthrough notes,
error-shape note (`{"error":{...}}`).

### Local deployment workflow (index.md, shared section)

- Same machine: default `127.0.0.1:8000` works as-is.
- Other machine on LAN: run with `--host 0.0.0.0`, client base URL becomes
  `http://<relay-host-ip>:8000/v1`.
- Remote/container: TLS termination at a reverse proxy (link
  `deployment.md`), expose only the proxy; keep `RELAY_API_KEY` set.
- Scaling: **run exactly one Relay process** (SQLite single-writer); scale by
  isolated instances with separate `PERSISTENCE_PATH` (link `deployment.md`).

---

## 6. Security considerations

- **Never print real keys in the guides.** All examples use placeholders
  (`<RELAY_API_KEY>`, `rl_…` ellipses). The only time a real key appears is
  the user's own `relay keys add` output, which the guide explicitly says is
  shown once and must be saved into the client's config immediately.
- **Warn loudly about the default-off auth state.** Empty `RELAY_API_KEY` +
  `RELAY_AUTH_STORE=false` ⇒ unauthenticated. Guides instruct setting one of
  the two tiers before exposing a client to anything beyond localhost.
- **Recommend scoped per-app keys.** `--scopes chat,v1` grants chat access
  only (client tools never need `admin`). Note the bootstrap key always has
  full access and always wins (`app/security/auth.py` precedence).
- **Document the credential contract accurately**: `Authorization: Bearer
  <key>` or `X-Relay-API-Key: <key>`; `/` and `/health` stay public; all
  other paths (including `/docs`, `/redoc`, `/openapi.json`) require auth
  when configured.
- **Rotation guidance**: per-client keys rotate via `relay keys rotate`; a
  leaked key is revoked with `relay keys remove` and events reviewed via
  `relay events`.
- **No secrets in doc artifacts**: no examples carry real provider or relay
  keys; no screenshots with live credentials.
- **Redaction consistency**: reuse the existing masking notation
  (`********abcd`) already used across the docs.

---

## 7. Accuracy verification strategy

Every command and value in the guides is verified against the real
application before the guides are written into final form:

1. **Live command check (local instance).** Start `relay serve` (or uvicorn)
   and execute each documented command exactly as printed:
   - `relay keys add --label opencode --scopes chat,v1` → confirm a scoped
     key is created and printed once; `relay keys list` shows it without the
     raw key.
   - `curl` a `/v1/models` against the key → confirm the base-URL and auth
     header forms work (`Authorization: Bearer` and `X-Relay-API-Key`).
   - Scope enforcement: the `chat,v1` key on `/admin` → `403`; wrong key on
     `/v1` → `401` (matching `test_auth_enforced`).
2. **Model-id accuracy.** Guide model examples are taken from real
   `/v1/models` output for the enabled provider, and/or stated as
   "any model id from `/v1/models`".
3. **Contract cross-check.** Base URL (`.../v1`), error shape
   (`{"error":{...}}`), streaming `id` stability, tools passthrough — each
   claim is backed by the test that locks it (`test_rc_validation.py`,
   `test_openai_api.py`, `test_openai_error_shape_unknown_model`). No claim
   is stated that no test covers.
4. **CLI-name check.** Guide commands match `relay --help` / `relay keys
   --help` output exactly (verified: `keys add/list/remove/rotate/prune/test`;
   the roadmap's "`keys create`" wording is corrected to `keys add`).
5. **Client-version caveat.** Where a client's config UI/format changes
   across versions, the guide states the assumed version and links the
   client's own docs rather than guessing.

---

## 8. Testing / validation strategy

Docs-only phase — **no new or edited pytest tests**; runtime, API, and
schema are untouched by design.

- **Regression gate:** full suite stays green — `python -m pytest tests -q`
  → **2055 passed, 20 skipped** (the P7.3-verified baseline). CI re-runs it
  as the merge gate on commit.
- **Manual doc verification (the P8-specific gate):** the §7 live command
  checklist is run against a local instance and recorded in the plan's
  follow-up report:
  - key creation + `keys list` masking,
  - authenticated `/v1/models` and one `/v1/chat/completions` round trip with
    the documented header forms,
  - scope denial on `/admin` for a `chat,v1` key,
  - the local-deployment base-URL variants (`127.0.0.1` vs `--host 0.0.0.0`).
- **Consistency check:** every code block in the guides is a verbatim command
  from §7 verification; every internal link resolves (`docs/clients/…`,
  `configuration.md`, `deployment.md`, `security.md`, `troubleshooting.md`).
- No new CI job (CI already green and out of scope).

---

## 9. Rollback strategy

Documentation-only phase ⇒ rollback is trivial and low-risk:

- **Pre-commit:** if any verification step fails or any constraint is
  violated, do not commit; revise the affected guide (or drop the phase and
  keep the plan doc).
- **Post-commit revert:** `git revert <commit>` removes the new guide files
  and the two cross-link edits in `README.md` / `deployment.md`. No state,
  DB, or runtime impact; the suite remains green before and after.
- **No data/state rollback needed** — nothing persists, migrates, or writes.
- The guides reference only existing behavior; reverting them restores the
  exact pre-P8 documentation state with no dangling references (the new links
  in `README.md`/`deployment.md` are removed by the same revert).

---

## 10. Acceptance criteria

- [ ] Four guides exist under `docs/clients/` (Cline, OpenCode, Continue,
      generic OpenAI-compatible) plus `index.md`, each following the common
      template (§5).
- [ ] Requirement 19 closed: each of Cline/OpenCode/Continue has a
      step-by-step setup that points the client at Relay's `/v1`, creates a
      usable key, selects a model from `/v1/models`, and verifies a round trip.
- [ ] Local deployment workflow, authentication/key configuration (both
      tiers, headers, scopes), and troubleshooting/common failures are
      covered per the user-approved scope (§P8 scope).
- [ ] Every command in the guides was executed against a real local instance
      (§7) and recorded; no unverified claims.
- [ ] Security pass: no real keys anywhere; the default-off auth warning,
      scoped-key recommendation, and header contract are stated accurately
      (§6).
- [ ] Constraints honored: zero runtime behavior change, zero API contract
      change, zero schema/persistence change, `PROJECT_LOG.md` untouched,
      plan docs uncommitted until approval.
- [ ] Full suite green on commit: 2055 passed, 20 skipped (CI merge gate).
- [ ] `README.md` and `docs/deployment.md` cross-link to `docs/clients/index.md`.
- [ ] Documentation/security/release audits run after implementation and
      before commit (workflow step 5); commit happens only after approval
      (workflow step 6).

---

## 11. Pre-v1.0 decisions still required after P8

Recorded here per the roadmap post-P7 audit (§9). These are **decisions and
release actions, not P8 deliverables**; none block the docs phase, and each
must be resolved at the v1.0.0 gate.

1. **OpenRouter/Groq wiring decision (P4 remainder / Decision H).** The
   `OPENROUTER_API_KEY` / `GROQ_API_KEY` env vars are parsed
   (`app/core/config.py:294/316`, `config_spec.py:408/423`) but no client or
   registry entry exists. Choose: **wire both providers** (add registry
   entries + async OpenAI-compatible clients, small diff) or **formally drop
   the reserved keys** (remove the parsed-but-unused surface and `.env.example`
   entries). Left open; must not ship ambiguous.

2. **P1 CLI deviation record.** The roadmap's P1 exit criterion listed
   subcommands `status`, `providers`, `models`, `routing`, `logs`, `test`.
   The shipped CLI is `setup`, `tui`, `serve`, `keys`, `provider keys`,
   `migrate`, `events`, `apps`, `config` (`logs` ≈ `events`; the TUI covers
   the status/providers/models/routing surfaces). Record this as an
   intentional deviation (TUI is the main interface) in the release gate —
   decision, not code.

3. **OpenAI quota / hard blocker.** No OpenAI completion can succeed until
   the account behind `OPENAI_API_KEY` has billing restored; the gateway is
   NVIDIA-only in practice. After the fix, re-run
   `python tests/run_live_smoke.py` and confirm 6/6 (see
   `docs/blockers-before-public-release.md`). Gate action, not a code change.

4. **Deployed auth/persistence profile.** Defaults are insecure-by-design
   (`RELAY_API_KEY` empty ⇒ auth off; `PERSISTENCE_ENABLED=false`). The
   deployed profile must set `RELAY_API_KEY=<long-random>`,
   `PERSISTENCE_ENABLED=true`, `PERSISTENCE_PATH=…`, and the operator must
   decide whether to enable retry hardening (`RETRY_HONOR_RETRY_AFTER`,
   backoff, budget) and pin `NVIDIA_MODEL_PRIORITY`/`OPENAI_MODEL_PRIORITY`.
   The P8 guides assume this profile in their security warnings.

5. **Version bump / tag process.** Package version is `0.1.0`
   (`relay --version` prints `relay 0.1.0`); do **not** tag `v1.0.0` without
   bumping the package to `1.0.0` in the same change (alignment-audit warning
   W1). Process: after P8, run the release gate (§9 of
   `docs/roadmap-post-p7-audit.md`), then one commit bumping
   `app/__version__` (and `pyproject.toml` if it mirrors it) to `1.0.0` with
   the `v1.0.0` tag.
