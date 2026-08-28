"""
Security tests for RELAY_API_KEY authentication.

The dependency is evaluated per-request against the live settings
object, so tests monkeypatch `settings.relay_api_key` directly rather
than rebuilding the app or the auth module.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app

PUBLIC_PATHS = ["/", "/health"]
PROTECTED_PATHS = [
    "/providers",
    "/health/deep",
    "/diagnostics",
    "/docs",
    "/redoc",
    "/openapi.json",
]


@pytest.fixture
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


def test_authentication_disabled_by_default(client):
    assert client.get("/providers").status_code == 200


def test_docs_reachable_when_auth_disabled(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 200


def test_openapi_does_not_leak_schema_without_key(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "secret-token")

    response = client.get("/openapi.json")
    assert response.status_code == 401


def test_docs_work_with_bearer_token(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "secret-token")

    for path in ("/docs", "/redoc", "/openapi.json"):
        response = client.get(
            path,
            headers={"Authorization": "Bearer secret-token"},
        )
        assert response.status_code == 200


def test_public_paths_require_no_key_when_enabled(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "secret-token")

    for path in PUBLIC_PATHS:
        assert client.get(path).status_code == 200


def test_protected_paths_reject_without_key(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "secret-token")

    for path in PROTECTED_PATHS:
        response = client.get(path)
        assert response.status_code == 401


def test_protected_paths_accept_bearer_token(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "secret-token")

    for path in PROTECTED_PATHS:
        response = client.get(
            path,
            headers={"Authorization": "Bearer secret-token"},
        )
        assert response.status_code == 200


def test_bearer_scheme_is_case_insensitive(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "secret-token")

    response = client.get(
        "/providers",
        headers={"Authorization": "bearer secret-token"},
    )
    assert response.status_code == 200


def test_protected_paths_accept_x_relay_api_key_header(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "secret-token")

    for path in PROTECTED_PATHS:
        response = client.get(
            path,
            headers={"X-Relay-API-Key": "secret-token"},
        )
        assert response.status_code == 200


def test_protected_paths_reject_wrong_token(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "secret-token")

    for path in PROTECTED_PATHS:
        response = client.get(
            path,
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert response.status_code == 401


def test_unauthorized_response_does_not_leak_key(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "secret-token")

    response = client.get("/providers")

    assert response.status_code == 401
    body = response.json()
    assert "secret-token" not in str(body)
    assert body["detail"] == "Unauthorized"


def test_chat_endpoint_is_protected(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "secret-token")

    response = client.post("/chat", json={"message": "hello"})
    assert response.status_code == 401


def test_openai_endpoint_is_protected(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "secret-token")

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "meta/llama-3-70b",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 401


def test_health_stays_minimal_when_auth_enabled(client, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "secret-token")

    response = client.get("/health")
    assert response.status_code == 200
    assert set(response.json().keys()) == {"status"}


# ------------------------------------------------------------ auth scheme


def test_auth_scheme_public_paths():
    from app.security.auth import auth_scheme

    for path in PUBLIC_PATHS:
        assert (
            auth_scheme(
                path=path,
                authorization=None,
                x_api_key=None,
                auth_enabled=True,
            )
            == "public"
        )


def test_auth_scheme_disabled_is_none():
    from app.security.auth import auth_scheme

    assert (
        auth_scheme(
            path="/providers",
            authorization=None,
            x_api_key=None,
            auth_enabled=False,
        )
        == "none"
    )


def test_auth_scheme_missing_credentials_is_none():
    from app.security.auth import auth_scheme

    assert (
        auth_scheme(
            path="/providers",
            authorization=None,
            x_api_key=None,
            auth_enabled=True,
        )
        == "none"
    )


def test_auth_scheme_bearer_prefix():
    from app.security.auth import auth_scheme

    assert (
        auth_scheme(
            path="/providers",
            authorization="Bearer token",
            x_api_key=None,
            auth_enabled=True,
        )
        == "bearer"
    )


def test_auth_scheme_bearer_is_case_insensitive():
    from app.security.auth import auth_scheme

    assert (
        auth_scheme(
            path="/providers",
            authorization="bearer token",
            x_api_key=None,
            auth_enabled=True,
        )
        == "bearer"
    )


def test_auth_scheme_header_key():
    from app.security.auth import auth_scheme

    assert (
        auth_scheme(
            path="/providers",
            authorization=None,
            x_api_key="token",
            auth_enabled=True,
        )
        == "header"
    )


def test_auth_scheme_other_authorization_is_none():
    from app.security.auth import auth_scheme

    assert (
        auth_scheme(
            path="/providers",
            authorization="Basic dXNlcjpwYXNz",
            x_api_key=None,
            auth_enabled=True,
        )
        == "none"
    )


@pytest.mark.parametrize(
    "host",
    [
        "relay.invalid/?",
        "relay.invalid/#fragment",
        "relay.invalid/?path=/",
        "relay.invalid/%2f?path=/",
    ],
)
def test_malformed_host_cannot_bypass_protected_route(
    client, monkeypatch, host
):
    """Host parsing must not turn a protected route into a public one."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "stage1-auth-secret")

    response = client.get(
        "/admin/events",
        headers={"Host": host},
    )

    assert response.status_code == 401


def test_valid_host_and_credentials_preserve_protected_routing(
    client, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "stage1-auth-secret")

    unauthenticated = client.get(
        "/admin/events",
        headers={"Host": "relay.invalid"},
    )
    authenticated = client.get(
        "/admin/events",
        headers={
            "Host": "relay.invalid",
            "Authorization": "Bearer stage1-auth-secret",
        },
    )

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200


def test_malformed_host_with_valid_credentials_still_routes_normally(
    client, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "stage1-auth-secret")

    response = client.get(
        "/admin/events",
        headers={
            "Host": "relay.invalid/?",
            "Authorization": "Bearer stage1-auth-secret",
            "X-Forwarded-Host": "relay.invalid/?",
            "X-Forwarded-Proto": "https",
        },
    )

    assert response.status_code == 200


def test_malformed_host_does_not_change_public_route_behavior(
    client, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_api_key", "stage1-auth-secret")

    response = client.get(
        "/health",
        headers={"Host": "relay.invalid/?"},
    )

    assert response.status_code == 200


def test_constant_time_eq_uses_hmac_compare_digest(monkeypatch):
    """
    N-13 security regression guard.

    The auth comparison must route through ``hmac.compare_digest`` so content
    and length differences do not leak through a short-circuiting ``==``.
    This is a deterministic structural check: if the implementation ever
    regresses to a naive ``==`` it never calls ``compare_digest`` and the
    assertion on ``calls`` fails (timing-based checks are load-flaky, so a
    call-routing assertion is used instead).
    """
    import app.security.auth as auth_module

    class _FakeHmac:
        def __init__(self):
            self.calls = 0

        def compare_digest(self, left, right):
            self.calls += 1
            return left == right

    fake = _FakeHmac()
    monkeypatch.setattr(auth_module, "hmac", fake)

    assert auth_module._constant_time_eq("a" * 32, "a" * 32) is True
    assert auth_module._constant_time_eq("a" * 32, "b" * 32) is False
    assert auth_module._constant_time_eq("a" * 31, "a" * 32) is False
    assert fake.calls >= 3

