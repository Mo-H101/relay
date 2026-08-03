"""
Client-application detection for the Applications surface.

Pure, heuristic-only bucketing of a request ``User-Agent`` header into a
small fixed set of buckets (Cline / OpenCode / Continue / Other). The
heuristics are intentionally simple substring matches on the trimmed,
lower-cased header; they are documented guesses, never an identity
assertion.

Security: only the bucket (and the already-trimmed header, capped at
``_MAX_UA`` characters) may ever be stored. The header is metadata, not
payload, but it is still bounded so arbitrary UA content cannot grow
unbounded in memory or confuse the UI.
"""

from __future__ import annotations

# Bucket list is fixed so the Applications table and tests stay
# deterministic. Add new clients here only with a matching test.
CLIENT_BUCKETS: tuple[str, ...] = ("cline", "opencode", "continue", "other")

# Substrings checked in priority order. Order matters: a UA mentioning
# several tools is bucketed as the first match.
_MARKERS: tuple[tuple[str, str], ...] = (
    ("cline", "cline"),
    ("opencode", "opencode"),
    ("continue", "continue"),
)

# Maximum length of the User-Agent kept for display.
_MAX_UA = 200


def classify_client(user_agent: str | None) -> str:
    """
    Bucket a User-Agent header into ``CLIENT_BUCKETS``.

    The header is stripped, lower-cased, and capped at ``_MAX_UA``
    characters before matching; empty or unrecognized headers fall into
    ``"other"``.
    """
    ua = (user_agent or "").strip().lower()[:_MAX_UA]

    for marker, bucket in _MARKERS:
        if marker in ua:
            return bucket

    return "other"
