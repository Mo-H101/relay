"""
Proxy support tests (Phase 6E).

Covers the proxy behavior matrix: per-provider override wins, an empty
override explicitly bypasses, and the global PROXY_ENABLED /
HTTP_PROXY / HTTPS_PROXY / NO_PROXY settings drive scheme-specific
selection. Also verifies the httpx call sites receive the computed
proxy kwargs.
"""

import pytest

from app.core.config import settings
from app.providers.base import Provider
from app.providers.openai_compat_client import (
    _matches_no_proxy,
    proxy_request_kwargs,
)


@pytest.fixture(autouse=True)
def clean_proxy_env(monkeypatch):
    for var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    ):
        monkeypatch.delenv(var, raising=False)


def make_provider(proxy=None):
    return Provider(
        name="Test",
        base_url="https://provider.invalid",
        api_key="test-key",
        proxy=proxy,
        models=["m1"],
    )


class TestProviderOverride:
    def test_explicit_proxy_forces_proxy(self):
        provider = make_provider(proxy="http://proxy.internal:8080")

        kwargs = proxy_request_kwargs(
            provider, "https://provider.invalid/v1/chat/completions"
        )

        assert kwargs["proxy"] == "http://proxy.internal:8080"
        assert kwargs["trust_env"] is False

    def test_explicit_proxy_wins_over_global_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "proxy_enabled", True)
        monkeypatch.setattr(settings, "https_proxy", "http://global:8080")
        provider = make_provider(proxy="http://provider-proxy:3128")

        kwargs = proxy_request_kwargs(
            provider, "https://provider.invalid/v1/chat/completions"
        )

        assert kwargs["proxy"] == "http://provider-proxy:3128"
        assert kwargs["trust_env"] is False

    def test_empty_proxy_explicitly_bypasses(self, monkeypatch):
        monkeypatch.setattr(settings, "proxy_enabled", True)
        monkeypatch.setattr(settings, "https_proxy", "http://global:8080")
        provider = make_provider(proxy="")

        kwargs = proxy_request_kwargs(
            provider, "https://provider.invalid/v1/chat/completions"
        )

        assert kwargs["proxy"] is None
        assert kwargs["trust_env"] is False


class TestGlobalSelection:
    def test_disabled_global_proxy_bypasses(self, monkeypatch):
        monkeypatch.setattr(settings, "proxy_enabled", False)
        monkeypatch.setattr(settings, "http_proxy", "http://proxy:8080")
        monkeypatch.setattr(settings, "https_proxy", "http://proxy:8080")
        provider = make_provider()

        kwargs = proxy_request_kwargs(
            provider, "https://provider.invalid/v1/chat/completions"
        )

        assert kwargs["proxy"] is None
        assert kwargs["trust_env"] is False

    def test_scheme_specific_selection(self, monkeypatch):
        monkeypatch.setattr(settings, "proxy_enabled", True)
        monkeypatch.setattr(settings, "http_proxy", "http://proxy-http:8080")
        monkeypatch.setattr(settings, "https_proxy", "http://proxy-https:8443")
        monkeypatch.setattr(settings, "no_proxy", "")
        provider = make_provider()

        http_kwargs = proxy_request_kwargs(
            provider, "http://provider.invalid/x"
        )
        https_kwargs = proxy_request_kwargs(
            provider, "https://provider.invalid/x"
        )

        assert http_kwargs["proxy"] == "http://proxy-http:8080"
        assert https_kwargs["proxy"] == "http://proxy-https:8443"

    def test_no_proxy_matches_exact_and_suffix_hosts(self, monkeypatch):
        monkeypatch.setattr(settings, "proxy_enabled", True)
        monkeypatch.setattr(settings, "http_proxy", "http://proxy:8080")
        monkeypatch.setattr(settings, "https_proxy", "http://proxy:8080")
        monkeypatch.setattr(
            settings, "no_proxy", "internal.invalid, .corp.example"
        )
        provider = make_provider()

        internal = proxy_request_kwargs(
            provider, "https://api.internal.invalid/x"
        )
        corp = proxy_request_kwargs(
            provider, "https://db.corp.example/x"
        )
        external = proxy_request_kwargs(
            provider, "https://provider.invalid/x"
        )

        assert internal["proxy"] is None
        assert corp["proxy"] is None
        assert external["proxy"] == "http://proxy:8080"

    def test_no_proxy_wildcard_bypasses_everything(self, monkeypatch):
        monkeypatch.setattr(settings, "proxy_enabled", True)
        monkeypatch.setattr(settings, "https_proxy", "http://proxy:8080")
        monkeypatch.setattr(settings, "no_proxy", "*")
        provider = make_provider()

        kwargs = proxy_request_kwargs(
            provider, "https://provider.invalid/x"
        )

        assert kwargs["proxy"] is None

    def test_no_proxy_configured_keeps_httpx_default(self, monkeypatch):
        monkeypatch.setattr(settings, "proxy_enabled", True)
        monkeypatch.setattr(settings, "http_proxy", "")
        monkeypatch.setattr(settings, "https_proxy", "")
        monkeypatch.setattr(settings, "no_proxy", "")
        provider = make_provider()

        kwargs = proxy_request_kwargs(
            provider, "https://provider.invalid/x"
        )

        assert kwargs["proxy"] is None
        assert kwargs["trust_env"] is True


class TestMatchesNoProxy:
    def test_helper_matching(self):
        assert (
            _matches_no_proxy("https://api.internal.invalid/x", "internal.invalid")
            is True
        )
        assert (
            _matches_no_proxy(
                "https://sub.api.internal.invalid/x", ".internal.invalid"
            )
            is True
        )
        assert (
            _matches_no_proxy("https://provider.invalid/x", "internal.invalid")
            is False
        )
        assert _matches_no_proxy("https://x.invalid/x", "*") is True
        assert _matches_no_proxy("https://x.invalid/x", "") is False
        assert (
            _matches_no_proxy(
                "https://internal.invalid.attacker.com/x", "internal.invalid"
            )
            is False
        )


class TestCallSites:
    def test_chat_passes_computed_proxy_kwargs(self, monkeypatch):
        import app.providers.openai_compat_client as occ

        recorded = {}

        def fake_post(url, **kwargs):
            recorded["kwargs"] = kwargs
            response = FakeResponse()
            return response

        monkeypatch.setattr(occ.httpx, "post", fake_post)
        monkeypatch.setattr(settings, "proxy_enabled", True)
        monkeypatch.setattr(settings, "http_proxy", "http://proxy:8080")
        monkeypatch.setattr(settings, "https_proxy", "http://proxy:8080")
        monkeypatch.setattr(settings, "no_proxy", "")

        occ.OpenAICompatibleClient().chat(make_provider(), "m1", "hello")

        assert recorded["kwargs"]["proxy"] == "http://proxy:8080"
        assert recorded["kwargs"]["trust_env"] is False

    def test_chat_empty_override_bypasses(self, monkeypatch):
        import app.providers.openai_compat_client as occ

        recorded = {}

        def fake_post(url, **kwargs):
            recorded["kwargs"] = kwargs
            return FakeResponse()

        monkeypatch.setattr(occ.httpx, "post", fake_post)
        monkeypatch.setattr(settings, "proxy_enabled", True)
        monkeypatch.setattr(settings, "https_proxy", "http://global:8080")

        occ.OpenAICompatibleClient().chat(
            make_provider(proxy=""), "m1", "hello"
        )

        assert recorded["kwargs"]["proxy"] is None
        assert recorded["kwargs"]["trust_env"] is False

    def test_list_models_passes_computed_proxy_kwargs(self, monkeypatch):
        import app.providers.openai_compat_client as occ

        recorded = {}

        def fake_get(url, **kwargs):
            recorded["kwargs"] = kwargs
            return FakeResponse(
                payload={"data": [{"id": "m1"}, {"id": "m2"}]}
            )

        monkeypatch.setattr(occ.httpx, "get", fake_get)
        monkeypatch.setattr(settings, "proxy_enabled", True)
        monkeypatch.setattr(settings, "https_proxy", "http://proxy:8080")

        models = occ.OpenAICompatibleClient().list_models(make_provider())

        assert models == ["m1", "m2"]
        assert recorded["kwargs"]["proxy"] == "http://proxy:8080"


class FakeResponse:
    def __init__(self, payload=None):
        self.status_code = 200
        self._payload = payload or {
            "choices": [{"message": {"content": "hi"}}]
        }

    def json(self):
        return self._payload
