"""
Correlation ids for request tracing (Phase 0).

A correlation id is a random, opaque token generated per chat request and
carried out of the process only as a response header and an ephemeral
log field. It is never persisted, never linked to prompts or responses,
and never used as a lookup key for stored content.
"""

import uuid


def new_correlation_id() -> str:
    """
    Return a fresh random correlation id (lowercase hex).
    """
    return uuid.uuid4().hex
