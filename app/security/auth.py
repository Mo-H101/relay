"""
Centralized HTTP authentication for the Relay API.

A single global FastAPI dependency (require_api_key) guards the whole
application. Authentication has two tiers (P5 Phase 4):

* Tier 1 (bootstrap): when RELAY_API_KEY is set, every request except
  the public allowlist must present a matching credential. This path is
  byte-identical to pre-Phase-4 behavior.
* Tier 2 (store): when RELAY_AUTH_STORE is enabled, the dependency also
  accepts keys from the KeyStore (platform.db) with scope enforcement.
  Store-backed success is always checked after the bootstrap key, and a
  store outage fails closed (401) so a broken store cannot silently
  disable authentication.

When neither tier is configured, all requests are allowed and behavior
is unchanged. All failures return the same 401 body; the reason is
recorded only in metrics so callers cannot learn why a key was rejected.
"""

import hashlib
import hmac

from fastapi import HTTPException, Request

from app.core.config import settings, state_dir
from app.services.key_store import KeyStore, KeyStoreError
from app.services.metrics import relay_metrics

# Paths reachable without a valid API key when authentication is enabled.
# "/" is a lightweight status probe and "/health" is the public liveness
# endpoint used by reverse proxies, orchestrators, and monitors. Both
# expose only aggregate, non-sensitive status.
PUBLIC_PATHS = frozenset({"/", "/health"})

# Scope required by each protected path family. A key with empty scopes
# is granted full access; otherwise every scope in the family must be
# present. Store-backed keys only: the bootstrap key always has full
# access.
_SCOPES_BY_PATH: dict = {
    "/admin": frozenset({"admin"}),
    "/chat": frozenset({"chat", "v1"}),
    "/feedback": frozenset({"chat", "v1"}),
}
_SCOPES_BY_PREFIX: list = [
    ("/admin/", frozenset({"admin"})),
    ("/v1/", frozenset({"chat", "v1"})),
]

_HEADER_API_KEY = "x-relay-api-key"
_BEARER_PREFIX = "bearer"


def auth_configured() -> bool:
    """
    True when any form of API-key authentication is enabled: the
    bootstrap RELAY_API_KEY or the store-backed RELAY_AUTH_STORE flag.
    Read per request so flipping either setting takes effect without a
    restart.
    """
    if (settings.relay_api_key or "").strip():
        return True

    return bool(settings.relay_auth_store)


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


def _route_scopes(path: str) -> frozenset:
    """
    Scopes required for a path, or an empty frozenset when the path is
    not scope-gated (only store-backed keys are checked).
    """
    direct = _SCOPES_BY_PATH.get(path)
    if direct is not None:
        return direct

    for prefix, scopes in _SCOPES_BY_PREFIX:
        if path.startswith(prefix):
            return scopes

    return frozenset()


def _has_scopes(meta: dict, path: str) -> bool:
    """
    True when ``meta`` grants the scopes required by ``path``. An empty
    scopes list means full access; scope-gated paths must be covered.
    """
    required = _route_scopes(path)

    if not required:
        return True

    allowed = set(meta.get("scopes") or [])

    if not allowed:
        return True

    return required.issubset(allowed)


def _deny(request: Request, reason: str, status_code: int = 401):
    """
    Record an auth failure and raise the matching response. The body and
    headers are identical for every failure so callers cannot distinguish
    reasons from the response; the reason is visible only in metrics.
    """
    relay_metrics.record_auth(
        True,
        False,
        "",
        failure_reason=reason,
    )
    raise HTTPException(
        status_code=status_code,
        detail="Unauthorized" if status_code == 401 else "Forbidden",
        headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else None,
    )


_STORE_SINGLETON: KeyStore | None = None


def _key_store() -> KeyStore:
    """
    Lazily build the process-wide KeyStore used by the auth dependency.
    Tests monkeypatch this function to point at an isolated store.
    """
    global _STORE_SINGLETON

    if _STORE_SINGLETON is None:
        state_dir.mkdir(parents=True, exist_ok=True)
        _STORE_SINGLETON = KeyStore()

    return _STORE_SINGLETON


def _reset_key_store() -> None:
    """
    Close and drop the auth KeyStore singleton (test isolation).
    """
    global _STORE_SINGLETON

    if _STORE_SINGLETON is not None:
        _STORE_SINGLETON.close()
        _STORE_SINGLETON = None


def _grant_store(request: Request, meta: dict, scheme: str) -> None:
    """
    Accept a store-backed key: record the decision, publish the opaque
    key id to the shared scope (consumed by the metrics middleware), and
    enforce route scopes.
    """
    request.scope["relay_key_id"] = meta["id"]

    if not _has_scopes(meta, request.url.path):
        _deny(request, "forbidden", status_code=403)

    relay_metrics.record_auth(True, True, scheme, key_id=meta["id"])


def require_api_key(request: Request) -> None:
    """
    FastAPI dependency enforcing API-key authentication.

    Settings are read on every request, so enabling, rotating, or
    disabling authentication takes effect without restarting the process.
    The bootstrap key is checked first in constant time; when store
    authentication is enabled and the bootstrap key does not match, the
    KeyStore is consulted. A store outage fails closed (401). Failures
    return HTTP 401 without revealing the expected key or the reason;
    the key itself is never logged or returned to callers.
    """
    if not auth_configured():
        relay_metrics.auth_enabled.set(0)
        return

    relay_metrics.auth_enabled.set(1)

    if request.url.path in PUBLIC_PATHS:
        relay_metrics.record_auth(True, True, "public")
        return

    token = _extract_token(request)

    if token is None:
        _deny(request, "missing")
        return

    expected = (settings.relay_api_key or "").strip()

    if expected and _constant_time_eq(token, expected):
        authorization = request.headers.get("authorization", "")
        scheme = auth_scheme(
            path=request.url.path,
            authorization=authorization,
            x_api_key=request.headers.get(_HEADER_API_KEY, ""),
            auth_enabled=True,
        )
        relay_metrics.record_auth(True, True, scheme)
        return

    if settings.relay_auth_store:
        try:
            store = _key_store()
        except KeyStoreError as exc:
            relay_metrics.record_auth(
                True, False, "", failure_reason="store_unavailable"
            )
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

        try:
            meta = store.verify(token)
        except KeyStoreError:
            _deny(request, "store_unavailable")

        if meta is not None:
            authorization = request.headers.get("authorization", "")
            scheme = auth_scheme(
                path=request.url.path,
                authorization=authorization,
                x_api_key=request.headers.get(_HEADER_API_KEY, ""),
                auth_enabled=True,
            )
            _grant_store(request, meta, scheme)
            return

        reason = store.classify(token)["status"]
        if reason == "ok":
            reason = "invalid"

        _deny(request, reason)

    _deny(request, "invalid")
