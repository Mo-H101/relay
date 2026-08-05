"""
Availability classification and provider-error redaction (setup scans).

The three-state model used by the setup wizard and the health system:

- ``available``   — probe succeeded (✓)
- ``overloaded``  — temporarily degraded: HTTP 429/529 or a timeout (⚠)
- ``unavailable`` — failed with an auth/request/server error (✗)

These helpers are shared by the setup scan engine and (by extraction) the
health checker so both surfaces agree on the mapping.
"""

from app.providers.base import ModelProbe
from app.services.redaction import redact_provider_error

AVAILABLE = "available"
OVERLOADED = "overloaded"
UNAVAILABLE = "unavailable"

GLYPH = {
    AVAILABLE: "\u2713",      # ✓
    OVERLOADED: "\u26a0",     # ⚠
    UNAVAILABLE: "\u2717",    # ✗
}

# Status codes that mean "the model exists but is currently busy".
_DEGRADED_CODES = (429, 529)


def classify_probe(probe: ModelProbe) -> str:
    """
    Map a single model probe to the three-state availability status.
    """
    if probe.healthy:
        return AVAILABLE

    if probe.status_code in _DEGRADED_CODES:
        return OVERLOADED

    if probe.status_code == 0 and "timeout" in (probe.error or "").lower():
        return OVERLOADED

    return UNAVAILABLE


def safe_error_body(
    provider,
    status_code: int,
    body: str,
) -> str:
    """
    Build a bounded, redacted message from an untrusted provider body.

    Provider bodies may echo request content or the API key back. The key
    is stripped, control characters removed, and the text truncated so it
    never flows verbatim into wizard output, logs, or errors. Delegates to
    the shared redaction layer (P6.3 dedupe of ``_safe_provider_body``).
    """
    api_key = (
        provider.api_key
        if provider is not None and provider.has_api_key()
        else None
    )
    return redact_provider_error(api_key, status_code, body)
