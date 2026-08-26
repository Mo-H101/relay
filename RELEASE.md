# Relay Release Process

This document describes how to produce and validate a Relay release. It is
written for maintainers preparing a versioned, installable artifact for
public distribution.

> **Scope.** This process covers **building, verifying, packaging, and
> tagging** a release. Publishing to a package index and deploying to a
> server are intentionally left as manual, human-in-the-loop steps.

## Versioning

- The single source of truth for the version is `app/__version__.py`
  (`__version__ = "X.Y.ZrcN"` or `"X.Y.Z"`).
- `pyproject.toml` reads the version dynamically from that module, so the
  wheel and sdist always match the source.
- Version string must follow PEP 440. Examples: `1.0.0rc1`, `1.0.0`.
- The version is **already baked into `app/__version__.py`** before a
  release; the scripts below never bump it.

## Before you start

1. **Working tree must be clean.** Commit or stash all changes first.
2. You are on the intended release branch (`master` for this repo). The
   scripts refuse to run on a detached HEAD.
3. Python 3.10+ is on `PATH` (or use `.venv\Scripts\python.exe` /
   `.venv/bin/python`). The build dependencies (`build`, `setuptools`,
   `wheel`) must be installed (see `requirements-dev.txt`).
4. No secret keys may be present in any file that ships in the artifact.
   `grep -R -i "sk-...\|OPENAI_API_KEY=\|NVIDIA_API_KEY="` over the tree
   before packaging.

## Step 1 — Build and verify (automated)

Run the release script from the repository root. It performs, in order:

1. **Preflight** — clean working tree, expected branch, non-detached HEAD.
2. **Version capture** — reads `app/__version__.py` and the dist metadata;
   fails if they disagree.
3. **Clean build** — removes `build/`, `dist/`, and egg-info; builds sdist +
   wheel with `python -m build`.
4. **Checksums** — computes `SHA256` for both artifacts; the hashes are
   written to `SHA256SUMS` in the release bundle.
5. **Packaging verification** — runs `tests/test_packaging.py` (manifest
   sanity, wheel/sdist import smoke, upgrade-from-previous-release drill).
6. **Fresh-install smoke** — installs the wheel into a brand-new throwaway
   virtualenv and runs `relay --version`, `relay --help`, and a `/health`
   probe; fails if the installed CLI does not match the source version.
7. **Bundle** — copies the wheel, sdist, and `SHA256SUMS` into
   `release/relay-<version>/` together with the release documentation set.

The script **stops before tagging and publishing**. It prints the commit
hash that produced the artifacts and the path to the bundle.

### Usage

```bash
# POSIX
./release.sh

# Windows (PowerShell)
.\release.ps1
```

Both scripts accept an optional branch argument, defaulting to `master`:

```bash
./release.sh main
.\release.ps1 main
```

### What the scripts do NOT do

- They do not create a git tag.
- They do not push anything.
- They do not publish to PyPI.
- They do not change the version number.
- They do not require network access (except the fresh-install smoke, which
  needs to resolve runtime dependencies; if you are offline, set
  `RELAY_SKIP_SMOKE=1`).

## Step 2 — Test gate

Before tagging, the full suite must pass on the release commit:

```bash
# full suite (offline, no live providers)
python -m pytest tests -q
```

Expected baseline: **3041 passed, 20 skipped** (may drift upward as tests
are added). Also run:

- `python -m compileall -q app tests`
- The security/hardening and RC validation modules are included in the full
  run above.

If the release includes live-provider changes, run the live smoke manually:

```bash
python tests/run_live_smoke.py
```

(requires valid `OPENAI_API_KEY` and `NVIDIA_API_KEY`).

## Step 3 — Pre-release checklist

Run the checklist in `docs/pre-release-checklist.md` and record evidence for
every gate. Do not tag until all items pass or have an accepted, documented
deviation.

## Step 4 — Tag the release

After the checklist passes:

```bash
git tag -s -a v1.0.0-rc.1 -m "Relay 1.0.0rc1"
git push origin v1.0.0-rc.1
```

Conventions:

- The git tag is `v<version>` with dots normalized per the existing scheme
  (`1.0.0rc1` → `v1.0.0-rc.1`, `1.0.0` → `v1.0.0`).
- The tag must point at the exact commit the scripts verified (the hash
  printed in Step 1).
- Sign the tag when your key is available.

## Step 5 — Publish (manual)

1. Upload `release/relay-<version>/relay-<version>.tar.gz` and
   `relay-<version>-py3-none-any.whl` to the package index.
2. Publish the corresponding `CHANGELOG.md` entry and any release notes.
3. Attach the `SHA256SUMS` file to the release for checksum verification.

## Step 6 — Post-release verification

Install the published artifact exactly as an end user would (see the
POST-INSTALL checklist) and confirm the installed version, CLI surface,
`/health`, and `/v1/models` behave as documented.

## Rollback

If a released version is broken:

1. Do **not** delete the published artifact or the tag (published artifacts
   are immutable).
2. Release a new patch version with the fix and follow this process again.
3. Deployers downgrade by installing the previous version; see
   `docs/rollback-procedure.md` for state-file compatibility notes.

## Known environment caveats

- **CI branch trigger.** `.github/workflows/ci.yml` triggers `push` CI on the
  `main` branch only, but this repository's branch is `master`. CI still runs
  on pull requests. When you push the release tag, confirm CI runs on the tag
  or run the equivalent checks locally.
- **OpenAI quota.** The live OpenAI smoke requires an account with active
  billing; otherwise the gateway is NVIDIA-only in practice.
