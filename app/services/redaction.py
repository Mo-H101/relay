"""
Redaction layer for exported diagnostics and rendered log metadata.

Every Diagnostics export passes through this layer before it reaches a
file, and any ``data`` dict from the JSON file log is scrubbed before the
TUI renders it. The layer is intentionally conservative: it masks by key
name and by known secret shapes, so even unexpected leakage of a key or
an ``Authorization`` header cannot survive into an export.

Security contract enforced by tests:
- fake API keys (``sk-…``, ``nvapi-…``, ``rl_…``) never appear;
- ``Authorization`` header values never appear;
- request/prompt/message content never appears (it is never captured in
  the first place; the layer is the last line of defense).
"""

from __future__ import annotations

import re

# Key-name substrings whose value is always masked. ``authorization`` is
# included so an Authorization header value can never be rendered.
SENSITIVE_KEYS = (
    "api_key",
    "apikey",
    "api-key",
    "secret",
    "token",
    "password",
    "passwd",
    "authorization",
    "x-relay-api-key",
    "proxy",
    "credential",
)

# Known secret value shapes, matched anywhere in the text. The value
# patterns (bearer/sk/nvapi/rl) only replace the token text, keeping any
# surrounding quotes so JSON exports stay parseable. The header pattern
# then masks the whole ``authorization`` / ``x-relay-api-key`` value,
# re-emitting a quoted placeholder when the value was quoted.
_SK_KEY = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b")
_NVAPI_KEY = re.compile(r"\bnvapi-[A-Za-z0-9_\-]{8,}\b")
_RL_KEY = re.compile(r"\brl_[A-Za-z0-9]{43}\b")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=+\-]{4,}\b")
_AUTH_HEADER = re.compile(
    r"(?i)(['\"]?\b(?:authorization|x-relay-api-key)\b['\"]?\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\r\n,}\]\[]+)"
)

_REDACTED = "<redacted>"

# Bound for provider error bodies surfaced in error responses and logs.
# Kept small so an untrusted provider body can never flow verbatim anywhere.
_MAX_PROVIDER_ERROR_BODY = 200


def _mask_auth_header(match: re.Match) -> str:
    """
    Replace an authorization header value, preserving the key so JSON
    structure survives. Quoted values become a quoted placeholder.
    """
    prefix = match.group(1)
    value = match.group(0)[len(prefix):]

    if value[:1] in ("'", '"'):
        return prefix + f'"{_REDACTED}"'

    return prefix + _REDACTED


def redact_text(text: str) -> str:
    """
    Mask every known secret shape in ``text``.
    """
    text = _BEARER.sub(_REDACTED, text)
    text = _SK_KEY.sub(_REDACTED, text)
    text = _NVAPI_KEY.sub(_REDACTED, text)
    text = _RL_KEY.sub(_REDACTED, text)
    text = _AUTH_HEADER.sub(_mask_auth_header, text)
    return text


def redact_provider_error(
    api_key: str | None,
    status_code: int,
    body: str,
) -> str:
    """
    Build a bounded, redacted message from an untrusted provider body.

    Provider error bodies are treated as untrusted: they may echo the
    request prompt or the provider's own API key back to the relay. The
    provider's API key is masked when present, non-printable control
    characters are removed, and the text is truncated to a fixed bound so
    it never flows verbatim into error responses or logs. An empty body
    degrades to ``status <code>``.

    ``api_key`` is the provider's plain key value (or None). The shared
    redaction layer is the single implementation for every wire client and
    the availability scans (P6.3 dedupe).
    """
    if not body:
        return f"status {status_code}"

    text = body

    if api_key:
        text = text.replace(api_key, "[REDACTED]")

    text = "".join(
        ch
        for ch in text
        if ch == "\n" or ch == "\t" or ch.isprintable()
    )

    if len(text) > _MAX_PROVIDER_ERROR_BODY:
        text = text[:_MAX_PROVIDER_ERROR_BODY].rstrip() + "..."

    return text


def _is_sensitive(key: str) -> bool:
    normalized = key.lower()
    return any(marker in normalized for marker in SENSITIVE_KEYS)


def redact_dict(value):
    """
    Deep-scrub a value: mask values under sensitive key names, then run
    ``redact_text`` over every remaining string. Returns a plain copy.
    """
    if isinstance(value, dict):
        return {
            key: _REDACTED if _is_sensitive(str(key)) else redact_dict(item)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [redact_dict(item) for item in value]

    if isinstance(value, tuple):
        return tuple(redact_dict(item) for item in value)

    if isinstance(value, str):
        return redact_text(value)

    return value
