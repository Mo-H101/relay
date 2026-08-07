# Pre-Release Checklist

Run this checklist before tagging and publishing any Relay release. Record
evidence for every gate. Do not tag until all items pass or have an
accepted, documented deviation.

**Release version:** ________________
**Target commit:** ________________
**Date:** ________________
**Release engineer:** ________________

## Gate A — Source state

- [ ] Working tree is clean (`git status --porcelain` empty).
- [ ] On the intended release branch (`master`).
- [ ] `app/__version__.py` contains the release version (e.g. `1.0.0`).
- [ ] `CHANGELOG.md` has an entry for this version under `## [<version>] - <date>`.
- [ ] No secrets in the tree (`grep -R -i "sk-...\|_API_KEY=" . --exclude-dir=.venv --exclude=docs/*plan*`).
- [ ] No uncommitted generated files (`build/`, `dist/`, `release/`,
      `release-candidate/` are gitignored).

## Gate B — Build

- [ ] `release.ps1` / `release.sh` completed with exit code 0.
- [ ] sdist + wheel built and match the source version.
- [ ] `SHA256SUMS` present in the release bundle.
- [ ] Fresh-install smoke passed (installed wheel, not source).
- [ ] Bundle exists at `release/relay-<version>/`.

## Gate C — Test

- [ ] Full suite green: `python -m pytest tests -q`.
      Result: ______ passed, ______ skipped, ______ failed.
- [ ] `python -m compileall -q app tests` clean.
- [ ] Packaging verification (`tests/test_packaging.py`) green.
- [ ] RC validation + adversarial suites green (when applicable):
      `python -m pytest tests/test_rc_validation.py tests/test_continuity_adversarial.py -q`.
- [ ] Security/hardening suites green:
      `python -m pytest tests/test_auth.py tests/test_key_auth.py tests/test_hardening.py tests/test_retry_hardening.py tests/test_memory_contract.py tests/test_security_hardening.py -q`.
- [ ] (If live providers available) `python tests/run_live_smoke.py` passes.

## Gate D — Docs

- [ ] README matches the shipped feature surface (providers, CLI, install).
- [ ] `KNOWN-ISSUES.md` is up to date.
- [ ] `docs/pre-release-checklist.md` and `docs/post-install-verification.md`
      match the release's expected outputs.
- [ ] `RELEASE.md` steps were followed and no step was skipped.

## Gate E — Sign-off

- [ ] All items above are PASS, or each deviation is recorded with a reason
      and an owner.
- [ ] No open P0–P8 regressions vs. the previous baseline.
- [ ] Release decision record (`docs/release-decisions.md`) reviewed for
      this release.

## After this checklist passes

Tag, push, and publish per `RELEASE.md` Steps 4–6. Then run
`docs/post-install-verification.md` against the published artifact.
