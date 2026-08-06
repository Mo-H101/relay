"""
P9c wire-format contract for the continuity request headers.

Pure functions plus the request-scope resolver. No I/O; nothing here ever
touches SQLite. The contract is enforced in one place and nowhere else on
the request path:

* ``X-Relay-Conversation-Id`` / ``X-Relay-Project-Id`` values are
  printable ASCII only (no control characters), at most 128 bytes, and
  are never echoed in errors, logs, or metrics.
* ``project_key`` is a key-scoped one-way hash of an opaque project id:
  ``sha256(key_id || ":" || project_id)[:16]`` hex. A project id never
  appears without its owning key id and is never treated as a path.
* Conversation ids are uuid4 hex generated server-side
  (``new_conversation_id``).

``resolve_scope`` centralizes the "is continuity on for this request"
decision: off when the feature flag is disabled, off when the request is
not store-key scoped (bootstrap key and unauthenticated traffic get no
continuity), and off when neither header is present. A malformed header
value raises ``ContinuityHeaderError`` (generic 400 at the API layer;
the offending value is never surfaced).
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Optional

# Wire bound for opaque header values, matching the conversation store's
# own ``_MAX_ID_LENGTH`` (128) so a value accepted here always fits a row.
_MAX_HEADER_BYTES = 128

# Printable ASCII without control characters (0x20..0x7E).
_CHARSET = frozenset(chr(code) for code in range(0x20, 0x7F))


class ContinuityHeaderError(ValueError):
    """A malformed continuity header value (generic; never echoes it)."""


def new_conversation_id() -> str:
    """Generate a new opaque conversation id (uuid4 hex, 32 chars)."""
    return uuid.uuid4().hex


def _valid(value: object) -> Optional[str]:
    """Return the normalized value when it is a well-formed opaque id."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if len(text.encode("utf-8")) > _MAX_HEADER_BYTES:
        return None
    if not _CHARSET.issuperset(text):
        return None
    return text


def validate_conversation_id(value: object) -> Optional[str]:
    """
    Validate an ``X-Relay-Conversation-Id`` value.

    Returns the normalized value, or None when the header is absent or
    fails the bounds/charset contract.
    """
    return _valid(value)


def validate_project_id(value: object) -> Optional[str]:
    """
    Validate an ``X-Relay-Project-Id`` value (same contract as the
    conversation id).
    """
    return _valid(value)


def derive_project_key(key_id: object, project_id: object) -> Optional[str]:
    """
    Key-scoped opaque project hash, or None when either input is invalid.

    ``project_key = sha256(key_id || ":" || project_id)[:16]`` hex. The
    result is stable for the same key/project pair and never reversible.
    """
    key = _valid(key_id)
    project = _valid(project_id)
    if key is None or project is None:
        return None
    digest = hashlib.sha256((key + ":" + project).encode("utf-8")).digest()
    return digest[:16].hex()


def validate_resume_token(value: object) -> Optional[str]:
    """
    Validate an ``X-Relay-Resume-Token`` value (P9d).

    The token is an opaque one-time value issued by
    ``ContinuityRecovery`` (uuid4 hex). Same bounds/charset contract as
    the other continuity headers: printable ASCII only, at most 128
    bytes. Returns the normalized value, or None when absent or malformed.
    The value itself is never echoed in errors, logs, or metrics; only a
    one-way SHA-256 hash is ever persisted or compared.
    """
    return _valid(value)


def derive_resume_token_hash(token: object) -> Optional[str]:
    """
    One-way hash of a resume token: ``sha256(token)`` hex (full digest).
    Never reversible; only the hash is persisted or compared (P9d). None
    when the token fails the wire contract.
    """
    text = _valid(token)
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_scope(request) -> Optional[dict]:
    """
    Resolve the continuity scope for one HTTP request.

    Returns None when continuity is disabled, the request is not
    key-scoped, or neither continuity header is present. Raises
    ``ContinuityHeaderError`` on a malformed header value (the value is
    never echoed). Otherwise returns a scope dict consumed by the relay
    facade:

    ``key_id``, ``client_bucket``, ``project_key``,
    ``conversation_id`` (may be None), ``token_budget``,
    ``resume_token`` (may be None; P9d, validated under the same
    bounds/charset contract).
    """
    from app.core.config import settings
    from app.services.client_detection import classify_client

    if not settings.continuity_enabled:
        return None

    key_id = request.scope.get("relay_key_id")
    if not key_id:
        return None

    raw_conversation = request.headers.get("x-relay-conversation-id")
    raw_project = request.headers.get("x-relay-project-id")
    raw_resume = request.headers.get("x-relay-resume-token")

    conversation_id = None
    if raw_conversation:
        conversation_id = validate_conversation_id(raw_conversation)
        if conversation_id is None:
            raise ContinuityHeaderError("invalid relay conversation id")

    project_key = None
    if raw_project:
        project_id = validate_project_id(raw_project)
        if project_id is None:
            raise ContinuityHeaderError("invalid relay project id")
        project_key = derive_project_key(key_id, project_id)

    if conversation_id is None and project_key is None:
        return None

    # Without an explicit project id the key-scoped hash is derived from
    # the conversation id so every row still carries an opaque, key-scoped
    # project key (derived from opaque header input only).
    if project_key is None:
        project_key = derive_project_key(key_id, conversation_id)

    resume_token = None
    if raw_resume:
        resume_token = validate_resume_token(raw_resume)
        if resume_token is None:
            raise ContinuityHeaderError("invalid relay resume token")

    return {
        "key_id": str(key_id),
        "client_bucket": classify_client(request.headers.get("user-agent")),
        "project_key": project_key,
        "conversation_id": conversation_id,
        "token_budget": settings.continuity_context_token_budget,
        "resume_token": resume_token,
    }


__all__ = [
    "ContinuityHeaderError",
    "new_conversation_id",
    "validate_conversation_id",
    "validate_project_id",
    "derive_project_key",
    "validate_resume_token",
    "derive_resume_token_hash",
    "resolve_scope",
]
