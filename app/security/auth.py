"""
Centralized HTTP authentication for the Relay API.

A single global FastAPI dependency (require_api_key) guards the whole
application. When RELAY_API_KEY is set, every request except the public
allowlist must present a matching credential. When it is empty, all
requests are allowed and behavior is unchanged.
"""

import hashlib
import hmac

from fastapi import HTTPException, Request

from app.core.config import settings
from app.services.metrics import relay_metrics

# Paths reachable without a valid API key when authentication is enabled.
# "/" is a lightweight status probe and "/health" is the public liveness
# endpoint used by reverse proxies, orchestrators, and monitors. Both
# expose only aggregate, non-sensitive status.
PUBLIC_PATHS = frozenset({"/", "/health"})

_HEADER_API_KEY = "x-relay-api-key"
_BEARER_PREFIX = "bearer"


def auth_scheme(
    *,
    path: str,
    authorization: str = "",
    x_api_key: str = "",
    auth_enabled: bool = False,
) -> str:
    """
    Label the credential method presented by a request, without
    comparing anything: ``"public"`` (allowlisted path while auth is
    enabled), ``"bearer"``, ``"header"`` (X-Relay-API-Key), or ``"none"``.
    Used for the Applications metadata surface; never logs values.
    """
    if auth_enabled and path in PUBLIC_PATHS:
        return "public"

    if authorization:
        scheme = authorization.partition(" ")[0].strip().lower()

        if scheme == _BEARER_PREFIX:
            return "bearer"

    if x_api_key:
        return "header"

    return "none"


def _constant_time_eq(left: str, right: str) -> bool:
    """
    Compare two strings in constant time over their SHA-256 digests, so
    neither content nor length differences leak through timing.
    """
    left_digest = hashlib.sha256(left.encode("utf-8")).digest()
    right_digest = hashlib.sha256(right.encode("utf-8")).digest()
    return hmac.compare_digest(left_digest, right_digest)


def _extract_token(request: Request):
    """
    Pull a token from either the Authorization Bearer header or the
    X-Relay-API-Key header, whichever is present first.
    """
    authorization = request.headers.get("authorization")
    if authorization:
        scheme, _, credentials = authorization.partition(" ")
        if scheme.strip().lower() == _BEARER_PREFIX:
            token = credentials.strip()
            if token:
                return token

    token = request.headers.get(_HEADER_API_KEY)
    if token:
        return token.strip()

    return None


def require_api_key(request: Request) -> None:
    """
    FastAPI dependency enforcing API-key authentication.

    The expected key is read from settings on every request, so enabling,
    rotating, or disabling authentication takes effect without restarting
    the process. Failures return HTTP 401 without revealing the expected
    key. The key itself is never logged or returned to callers.
    """
    expected = (settings.relay_api_key or "").strip()
    if not expected:
        relay_metrics.auth_enabled.set(0)
        return

    relay_metrics.auth_enabled.set(1)

    if request.url.path in PUBLIC_PATHS:
        relay_metrics.record_auth(True, True, "public")
        return

    token = _extract_token(request)

    if token is None:
        relay_metrics.record_auth(True, False, "", failure_reason="missing")
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not _constant_time_eq(token, expected):
        relay_metrics.record_auth(True, False, "", failure_reason="invalid")
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )

    authorization = request.headers.get("authorization", "")
    scheme = auth_scheme(
        path=request.url.path,
        authorization=authorization,
        x_api_key=request.headers.get(_HEADER_API_KEY, ""),
        auth_enabled=True,
    )
    relay_metrics.record_auth(True, True, scheme)
