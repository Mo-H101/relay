# Bug Report

Before filing, check [KNOWN-ISSUES.md](KNOWN-ISSUES.md) to see whether the
behavior is already accepted. Include everything below — missing fields
slow down diagnosis.

## 1. Summary

- **Version:** __ (`relay --version`)
- **OS:** __ (e.g. Windows 11, Ubuntu 24.04)
- **Python:** __ (`python --version`)
- **Install method:** [ ] PyPI  [ ] release bundle  [ ] source  [ ] other
- **Date:** __

## 2. What happened

One or two sentences describing the failure.

## 3. Expected vs. actual

- **Expected:** __
- **Actual:** __

## 4. How to reproduce

Steps:

1. __
2. __
3. __

## 5. Request details (if an API failure)

- **Endpoint:** __ (e.g. `POST /v1/chat/completions`)
- **Model requested:** __
- **Streaming?** [ ] yes  [ ] no
- **Auth used?** [ ] bootstrap key  [ ] scoped key  [ ] none
- **Request body:** __ (redact keys)
- **Response (HTTP status + body):** __ (redact keys; provider bodies are
  already bounded/scrubbed by Relay)

## 6. Environment / configuration

- **Environment variables set:** __ (names only, redact values; e.g.
  `RELAY_API_KEY`, `RETRY_HONOR_RETRY_AFTER=true`, `PERSISTENCE_ENABLED=true`)
- **Config file:** __ (attach or redact)
- **Logs / traceback:** __ (paste relevant lines, redact secrets)

## 7. Severity

- [ ] Critical (blocks use)
- [ ] High (major function broken)
- [ ] Medium (partial failure, workaround exists)
- [ ] Low (cosmetic / minor)

## 8. Regression?

- [ ] I believe this is a regression vs. version __
- [ ] Not sure / new behavior

## 9. Additional context

Anything else: frequency, does it happen every time, related open issues,
screenshots (redact secrets).
