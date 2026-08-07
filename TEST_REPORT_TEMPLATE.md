# Test Report

One report per tested release/commit. Copy this file, fill in the `__`_
fields, and replace the example rows with your actual results.

## Metadata

- **Release / commit:** __ (e.g. `1.0.0` / `v1.0.0`, or `8392b51`)
- **Version tested:** __ (`relay --version`)
- **Test date:** __
- **Environment:** __ (e.g. Windows 11 / Python 3.12.4, x86_64)
- **Test command:** __ (e.g. `python -m pytest tests -q`)
- **Tester / CI run:** __ (name or CI job URL)

## Summary

| Metric | Expected | Actual | Pass? |
| --- | --- | --- | --- |
| Total tests | (baseline + new) | __ passed | [ ] |
| Skipped | (baseline) | __ | [ ] |
| Failed | 0 | __ | [ ] |
| Suite duration | — | __ s | [ ] |
| `python -m compileall -q app tests` | clean | __ | [ ] |

## Suites

Replace with the suites you ran. Example:

| Suite | Tests | Passed | Skipped | Failed | Notes |
| --- | --- | --- | --- | --- | --- |
| Full suite (`tests`) | __ | __ | __ | __ | |
| RC validation (`test_rc_validation.py`) | __ | __ | __ | __ | |
| Continuity adversarial | __ | __ | __ | __ | |
| Security/hardening | __ | __ | __ | __ | |
| Packaging (`test_packaging.py`) | __ | __ | __ | __ | |
| Docs consistency (`test_docs_consistency.py`) | 7 | __ | __ | __ | |

## Verification highlights

- [ ] `release.ps1` / `release.sh` built sdist + wheel successfully.
- [ ] `SHA256SUMS` generated and matches.
- [ ] Fresh-install smoke passed (`relay --version`, `relay --help`, `/health` 200).
- [ ] `relay --version` matches the source version in `app/__version__.py`.

## Known deviations

List any skipped/expected failures and why (e.g. OpenAI quota blocker, the
accepted timing flake D14):

- __

## Sign-off

- [ ] No P0–P8 regressions vs. previous baseline.
- [ ] All deviations have an owner and a tracking issue.
- Signed: __ Date: __
