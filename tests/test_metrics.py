"""
Tests for the self-contained Prometheus metrics registry, the ASGI
metrics middleware, and the /metrics endpoint.
"""

import pytest
from collections import Counter
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.providers.exceptions import ProviderError
from app.services.metrics import MetricsRegistry, relay_metrics


@pytest.fixture(autouse=True)
def reset_metrics():
    relay_metrics.reset()
    yield
    relay_metrics.reset()


@pytest.fixture
def registry():
    return MetricsRegistry()


@pytest.fixture
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


class TestRegistry:
    def test_counter_inc_and_value(self, registry):
        counter = registry.counter("test_count_total", "doc", ("kind",))
        counter.inc(kind="a")
        counter.inc(2, kind="a")
        counter.inc(kind="b")
        assert counter.value(kind="a") == 3
        assert counter.value(kind="b") == 1
        assert counter.total() == 4

    def test_counter_rejects_negative(self, registry):
        counter = registry.counter("test_count_total", "doc")
        with pytest.raises(ValueError):
            counter.inc(-1)

    def test_gauge_set_inc_dec(self, registry):
        gauge = registry.gauge("test_gauge", "doc")
        assert gauge.value() == 0.0
        gauge.inc()
        gauge.inc()
        gauge.dec()
        assert gauge.value() == 1.0
        gauge.set(5)
        assert gauge.value() == 5.0

    def test_histogram_bucket_counts(self, registry):
        histogram = registry.histogram("test_histogram", "doc")
        for value in (0.1, 0.5, 2.0, 60.0):
            histogram.observe(value)
        text = registry.render()
        assert 'test_histogram_bucket{le="0.25"} 1' in text
        assert 'test_histogram_bucket{le="0.5"} 2' in text
        assert 'test_histogram_bucket{le="2.5"} 3' in text
        assert 'test_histogram_bucket{le="+Inf"} 4' in text
        assert "test_histogram_count 4" in text
        assert "test_histogram_sum" in text

    def test_histogram_with_labels(self, registry):
        histogram = registry.histogram("test_hist", "doc", ("kind",))
        histogram.observe(0.1, kind="a")
        text = registry.render()
        assert 'test_hist_bucket{kind="a",le="0.25"} 1' in text
        assert 'test_hist_sum{kind="a"} 0.1' in text
        assert 'test_hist_count{kind="a"} 1' in text

    def test_render_help_and_type(self, registry):
        registry.counter("test_count_total", "A counter doc.", ("label",))
        text = registry.render()
        assert "# HELP test_count_total A counter doc." in text
        assert "# TYPE test_count counter" in text
        assert "# TYPE test_gauge gauge" not in text

    def test_counter_type_strips_total(self, registry):
        registry.counter("relay_http_requests_total", "doc")
        text = registry.render()
        assert "# TYPE relay_http_requests counter" in text

    def test_label_value_escaping(self, registry):
        counter = registry.counter("test_escape_total", "doc", ("label",))
        counter.inc(label='a"b\\c\nd')
        text = registry.render()
        assert 'label="a\\"b\\\\c\\nd"' in text


class TestMetricsEndpoint:
    def test_endpoint_returns_prometheus_text(self, client):
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        body = response.text
        assert "relay_http_requests_total" in body
        assert "relay_process_uptime_seconds" in body
        assert "relay_persistence_enabled" in body

    def test_health_request_recorded(self, client):
        client.get("/health")
        client.get("/health")
        body = client.get("/metrics").text
        assert (
            'relay_http_requests_total{method="GET",route="/health",status="200"} 2.0'
            in body
        )
        assert 'relay_http_success_total{method="GET",route="/health"} 2.0' in body

    def test_failed_request_recorded_with_unmatched_route(self, client):
        client.get("/does-not-exist")
        body = client.get("/metrics").text
        assert (
            'relay_http_requests_total{method="GET",route="unmatched",status="404"} 1.0'
            in body
        )
        assert 'relay_http_failure_total{method="GET",route="unmatched"} 1.0' in body

    def test_active_requests_returns_to_zero(self, client):
        client.get("/health")
        assert relay_metrics.http_active.value() == 0
        client.get("/metrics")
        assert relay_metrics.http_active.value() == 0

    def test_metrics_protected_when_auth_enabled(self, client, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "relay_api_key", "secret")
        assert client.get("/metrics").status_code == 401
        assert (
            client.get("/metrics", headers={"Authorization": "Bearer secret"}).status_code
            == 200
        )

    def test_auth_metrics_recorded(self, client, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "relay_api_key", "secret")
        client.get("/diagnostics")
        client.get("/diagnostics", headers={"Authorization": "Bearer wrong"})
        client.get("/diagnostics", headers={"Authorization": "Bearer secret"})
        body = client.get(
            "/metrics", headers={"Authorization": "Bearer secret"}
        ).text
        assert 'relay_auth_failures_total{reason="missing"} 1.0' in body
        assert 'relay_auth_failures_total{reason="invalid"} 1.0' in body
        assert 'relay_auth_success_total{method="bearer"} 2.0' in body
        assert "relay_auth_enabled 1.0" in body

    def test_public_path_counts_as_authenticated(self, client, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "relay_api_key", "secret")
        client.get("/health")
        body = client.get(
            "/metrics", headers={"Authorization": "Bearer secret"}
        ).text
        assert 'relay_auth_success_total{method="public"} 1.0' in body

    def test_metrics_never_leaks_secrets_or_payloads(self, client, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "relay_api_key", "super-secret-token")
        client.post("/chat", json={"message": "TOP-SECRET-PROMPT"})
        client.get("/diagnostics")
        body = client.get(
            "/metrics", headers={"Authorization": "Bearer super-secret-token"}
        ).text
        assert "super-secret-token" not in body
        assert "TOP-SECRET-PROMPT" not in body


def make_provider(name, models, priority=1, api_key="test-key", enabled=True):
    from app.providers.base import Provider

    return Provider(
        name=name,
        base_url=f"https://{name.lower()}.invalid",
        api_key=api_key,
        enabled=enabled,
        priority=priority,
        models=list(models),
    )


class FakeClient:
    """Deterministic chat client: per-model outcome queues and streams."""

    def __init__(self):
        self._outcomes = {}
        self._streams = {}

    def set_outcomes(self, model, outcomes):
        self._outcomes[model] = list(outcomes)

    def set_stream(self, model, deltas):
        self._streams[model] = list(deltas)

    def chat(self, provider, model, message, **kwargs):
        queue = self._outcomes.get(model)
        if not queue:
            raise ProviderError(f"no outcome configured for {model}")
        outcome = queue[0]
        if len(queue) > 1:
            queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def chat_stream(self, provider, model, message, **kwargs):
        deltas = self._streams.get(model)
        if deltas is None:
            raise ProviderError(f"no stream configured for {model}")
        for delta in deltas:
            yield delta

    async def achat(self, provider, model, message, **kwargs):
        """Async version of chat()."""
        queue = self._outcomes.get(model)
        if not queue:
            raise ProviderError(f"no outcome configured for {model}")
        outcome = queue[0]
        if len(queue) > 1:
            queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def achat_stream(self, provider, model, message, **kwargs):
        """Async version of chat_stream()."""
        deltas = self._streams.get(model)
        if deltas is None:
            raise ProviderError(f"no stream configured for {model}")
        for delta in deltas:
            yield delta

    def _default_response(self, model, content):
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 1700000000,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    def chat_messages(self, provider, payload):
        queue = self._outcomes.get(payload["model"])
        if not queue:
            raise ProviderError(f"no outcome configured for {payload['model']}")
        outcome = queue[0]
        if len(queue) > 1:
            queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, dict):
            return outcome
        return self._default_response(payload["model"], outcome)

    def chat_stream_messages(self, provider, payload):
        deltas = self._streams.get(payload["model"])
        if deltas is None:
            raise ProviderError(f"no stream configured for {payload['model']}")
        for delta in deltas:
            yield {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": delta},
                        "finish_reason": None,
                    }
                ],
            }
        yield {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": payload["model"],
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }

    async def achat_messages(self, provider, payload):
        """Async version of chat_messages()."""
        queue = self._outcomes.get(payload["model"])
        if not queue:
            raise ProviderError(f"no outcome configured for {payload['model']}")
        outcome = queue[0]
        if len(queue) > 1:
            queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, dict):
            return outcome
        return self._default_response(payload["model"], outcome)

    async def achat_stream_messages(self, provider, payload):
        """Async version of chat_stream_messages()."""
        deltas = self._streams.get(payload["model"])
        if deltas is None:
            raise ProviderError(f"no stream configured for {payload['model']}")
        for delta in deltas:
            yield {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": delta},
                        "finish_reason": None,
                    }
                ],
            }
        yield {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": payload["model"],
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }


@pytest.fixture
def fake_registry(monkeypatch):
    from app.services import client_registry

    holder = {}

    def fake_get(self, provider_name):
        return holder[provider_name]

    monkeypatch.setattr(client_registry.ClientRegistry, "get", fake_get)
    return holder


@pytest.fixture
def wired_relay(monkeypatch, fake_registry):
    from app.core.relay import Relay
    import app.api.chat
    import app.api.openai

    relays = {}

    def _build(providers, clients):
        relay = Relay()

        for provider in providers:
            relay.provider_manager.register(provider)

        for name, client in clients.items():
            fake_registry[name] = client

        monkeypatch.setattr(app.api.chat, "relay", relay)
        monkeypatch.setattr(app.api.openai, "relay", relay)

        relays[id(relay)] = relay
        return relay

    yield _build

    for relay in relays.values():
        monkeypatch.setattr(app.api.chat, "relay", relay)
        monkeypatch.setattr(app.api.openai, "relay", relay)


class TestChatMetrics:
    def test_chat_success_metrics(self, wired_relay, fake_registry, client):
        provider = make_provider("A", ["a-1"])
        fake_client = FakeClient()
        fake_client.set_outcomes("a-1", ["hello"])
        wired_relay([provider], {"A": fake_client})

        response = client.post(
            "/chat", json={"message": "hi", "max_tokens": 2000}
        )

        assert response.status_code == 200
        body = client.get("/metrics").text
        assert (
            'relay_chat_requests_total{endpoint="/chat",stream="false",'
            'max_tokens_band="<=2k"} 1.0' in body
        )
        assert (
            'relay_chat_outcomes_total{endpoint="/chat",stream="false",'
            'success="true",fallback="false"} 1.0' in body
        )
        assert 'relay_routing_selected_provider_total{provider="A"} 1.0' in body

    def test_chat_parameter_metrics(self, wired_relay, fake_registry, client):
        provider = make_provider("A", ["a-1"])
        fake_client = FakeClient()
        fake_client.set_outcomes("a-1", ["hello"])
        wired_relay([provider], {"A": fake_client})

        client.post(
            "/chat",
            json={"message": "hi", "temperature": 0.7, "max_tokens": 100},
        )

        body = client.get("/metrics").text
        assert (
            'relay_chat_parameter_used_total{endpoint="/chat",parameter="temperature"} 1.0'
            in body
        )
        assert (
            'relay_chat_parameter_used_total{endpoint="/chat",parameter="max_tokens"} 1.0'
            in body
        )

    def test_chat_failure_metrics(self, wired_relay, fake_registry, client):
        provider = make_provider("A", ["a-1"])
        fake_client = FakeClient()
        fake_client.set_outcomes("a-1", [ProviderError("boom")])
        wired_relay([provider], {"A": fake_client})

        response = client.post("/chat", json={"message": "hi"})

        assert response.status_code == 502
        body = client.get("/metrics").text
        assert (
            'relay_chat_outcomes_total{endpoint="/chat",stream="false",'
            'success="false",fallback="false"} 1.0' in body
        )

    def test_chat_ops_event_recorded(self, wired_relay, fake_registry, client):
        from app.services.ops_store import ops_store

        ops_store.clear()
        provider = make_provider("A", ["a-1"])
        fake_client = FakeClient()
        fake_client.set_outcomes("a-1", ["hello"])
        wired_relay([provider], {"A": fake_client})

        client.post("/chat", json={"message": "hi"})

        stats = ops_store.stats()
        assert stats["chats"] == 1
        assert stats["providers"][0]["provider"] == "A"
        assert stats["providers"][0]["success_rate"] == 1.0


class TestOpenAIMetrics:
    def test_openai_non_streaming_metrics(self, wired_relay, fake_registry, client):
        provider = make_provider("A", ["a-1"])
        fake_client = FakeClient()
        fake_client.set_outcomes("a-1", ["hello"])
        wired_relay([provider], {"A": fake_client})

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 200
        body = client.get("/metrics").text
        assert (
            'relay_chat_requests_total{endpoint="/v1/chat/completions",'
            'stream="false",max_tokens_band="unset"} 1.0' in body
        )
        assert (
            'relay_chat_outcomes_total{endpoint="/v1/chat/completions",'
            'stream="false",success="true",fallback="false"} 1.0' in body
        )

    def test_openai_streaming_metrics(self, wired_relay, fake_registry, client):
        provider = make_provider("A", ["a-1"])
        fake_client = FakeClient()
        fake_client.set_stream("a-1", ["hello ", "world"])
        wired_relay([provider], {"A": fake_client})

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        body = client.get("/metrics").text
        assert (
            'relay_chat_requests_total{endpoint="/v1/chat/completions",'
            'stream="true",max_tokens_band="unset"} 1.0' in body
        )
        assert (
            'relay_chat_outcomes_total{endpoint="/v1/chat/completions",'
            'stream="true",success="true",fallback="false"} 1.0' in body
        )


class TestProviderMetrics:
    def test_provider_chat_success(self, monkeypatch, client):
        from app.providers import openai_compat_client as occ

        responses = []

        def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
            responses.append((url, json))
            return FakeResponse(
                200, {"choices": [{"message": {"content": "hi"}}]}
            )

        monkeypatch.setattr(occ, "bounded_post", fake_post)

        provider = make_provider("A", ["a-1"])
        occ.OpenAICompatibleClient().chat(provider, "a-1", "hello")

        body = client.get("/metrics").text
        assert 'relay_provider_requests_total{provider="A",operation="chat"} 1.0' in body
        assert (
            'relay_provider_outcomes_total{provider="A",operation="chat",status="success"} 1.0'
            in body
        )
        assert responses[0][1]["model"] == "a-1"

    def test_provider_chat_http_error(self, monkeypatch, client):
        from app.providers import openai_compat_client as occ
        from app.providers.exceptions import ProviderHTTPError

        def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
            return FakeResponse(429, {}, text="rate limited")

        monkeypatch.setattr(occ, "bounded_post", fake_post)

        provider = make_provider("A", ["a-1"])
        with pytest.raises(ProviderHTTPError):
            occ.OpenAICompatibleClient().chat(provider, "a-1", "hello")

        body = client.get("/metrics").text
        assert (
            'relay_provider_outcomes_total{provider="A",operation="chat",status="http_4xx"} 1.0'
            in body
        )

    def test_provider_chat_timeout(self, monkeypatch, client):
        import httpx

        from app.providers import openai_compat_client as occ
        from app.providers.exceptions import ProviderTimeout

        def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
            raise httpx.ReadTimeout("timed out", request=None)

        monkeypatch.setattr(occ, "bounded_post", fake_post)

        provider = make_provider("A", ["a-1"])
        with pytest.raises(ProviderTimeout):
            occ.OpenAICompatibleClient().chat(provider, "a-1", "hello")

        body = client.get("/metrics").text
        assert (
            'relay_provider_outcomes_total{provider="A",operation="chat",status="timeout"} 1.0'
            in body
        )

    def test_provider_network_error(self, monkeypatch, client):
        import httpx

        from app.providers import openai_compat_client as occ
        from app.providers.exceptions import ProviderHTTPError

        def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(occ, "bounded_post", fake_post)

        provider = make_provider("A", ["a-1"])
        with pytest.raises(ProviderHTTPError):
            occ.OpenAICompatibleClient().chat(provider, "a-1", "hello")

        body = client.get("/metrics").text
        assert (
            'relay_provider_outcomes_total{provider="A",operation="chat",status="network"} 1.0'
            in body
        )

    def test_provider_list_models_metrics(self, monkeypatch, client):
        from app.providers import openai_compat_client as occ

        def fake_get(url, headers=None, timeout=None, **kwargs):
            return FakeResponse(
                200, {"data": [{"id": "a-1"}, {"id": "a-2"}]}
            )

        monkeypatch.setattr(occ, "bounded_get", fake_get)

        provider = make_provider("A", ["a-1", "a-2"])
        models = occ.OpenAICompatibleClient().list_models(provider)

        assert models == ["a-1", "a-2"]
        body = client.get("/metrics").text
        assert 'relay_provider_requests_total{provider="A",operation="list_models"} 1.0' in body


class TestHealthMetrics:
    def test_provider_health_gauge_updates(self, client):
        from app.services.health_checker import ProviderHealth

        healthy = ProviderHealth(
            name="A", status="healthy", latency_ms=5, last_checked="now",
            details="ok", connectivity=True, rate_limit_status="ok",
            last_successful_request=None,
        )
        degraded = ProviderHealth(
            name="A", status="degraded", latency_ms=5, last_checked="now",
            details="ok", connectivity=True, rate_limit_status="ok",
            last_successful_request=None,
        )

        relay_metrics.update_provider_health(healthy)
        relay_metrics.update_provider_health(degraded)

        body = client.get("/metrics").text
        assert 'relay_provider_health_info{provider="A",status="healthy"} 0.0' in body
        assert 'relay_provider_health_info{provider="A",status="degraded"} 1.0' in body
        assert 'relay_provider_connectivity{provider="A"} 1.0' in body


class TestClientTracking:
    @pytest.fixture(autouse=True)
    def isolated_request_log(self, monkeypatch, tmp_path):
        from app.services import request_log as request_log_module

        store = request_log_module.RequestLogStore(
            str(tmp_path / "reqlog.db"), flush_interval_seconds=0
        )
        monkeypatch.setattr(request_log_module, "request_log", lambda: store)
        yield store
        store.close()

    def test_middleware_records_client_activity(
        self, client, isolated_request_log
    ):
        client.get("/health", headers={"User-Agent": "Cline/3.0 (VS Code)"})
        client.get("/health", headers={"User-Agent": "opencode/0.1"})
        client.get("/health", headers={"User-Agent": "curl/8.6"})

        isolated_request_log.flush()
        counts = Counter(r["client_bucket"] for r in isolated_request_log.query())
        assert counts["cline"] == 1
        assert counts["opencode"] == 1
        assert counts["other"] == 1

    def test_middleware_captures_auth_scheme_labels(
        self, client, isolated_request_log, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "relay_api_key", "secret")
        client.get("/providers", headers={"Authorization": "Bearer secret"})
        client.get("/providers", headers={"X-Relay-API-Key": "secret"})
        client.get("/health")

        isolated_request_log.flush()
        auth = isolated_request_log.auth_totals()
        assert auth.get("bearer") == 1
        assert auth.get("header") == 1
        assert auth.get("public") == 1

    def test_middleware_never_records_authorization_value(
        self, client, isolated_request_log
    ):
        client.get("/health", headers={"Authorization": "Bearer super-secret-xyz"})

        isolated_request_log.flush()
        rendered = repr(isolated_request_log.query())
        assert "super-secret-xyz" not in rendered
        assert "Bearer" not in rendered


class TestBodySizeLimit:
    def test_declared_content_length_over_limit_rejected(self):
        import asyncio

        from app.api.middleware import BodySizeLimitMiddleware

        async def run():
            received = []

            async def receive():
                received.append(True)
                return {"type": "http.request", "body": b"", "more_body": False}

            sent = []

            async def send(message):
                sent.append(message)

            async def inner(scope, recv, send):
                await send(
                    {"type": "http.response.start", "status": 200, "headers": []}
                )
                await send({"type": "http.response.body", "body": b"ok"})

            scope = {
                "type": "http",
                "method": "POST",
                "headers": [(b"content-length", b"1000000")],
            }
            mw = BodySizeLimitMiddleware(inner, max_bytes=1024)
            await mw(scope, receive, send)
            return sent, received

        sent, received = asyncio.run(run())

        assert sent[0]["status"] == 413
        assert received == []

    def test_chunked_body_over_limit_rejected(self):
        import asyncio

        from app.api.middleware import BodySizeLimitMiddleware

        async def run():
            sent = []

            async def receive():
                return {"type": "http.request", "body": b"x" * 4096, "more_body": False}

            async def send(message):
                sent.append(message)

            async def inner(scope, recv, send):
                await recv()
                await send(
                    {"type": "http.response.start", "status": 200, "headers": []}
                )
                await send({"type": "http.response.body", "body": b"ok"})

            scope = {"type": "http", "method": "POST", "headers": []}
            mw = BodySizeLimitMiddleware(inner, max_bytes=1024)
            await mw(scope, receive, send)
            return sent

        sent = asyncio.run(run())

        assert sent[0]["status"] == 413

    def test_body_under_limit_passes_through(self):
        import asyncio

        from app.api.middleware import BodySizeLimitMiddleware

        async def run():
            sent = []

            async def receive():
                return {"type": "http.request", "body": b"small", "more_body": False}

            async def send(message):
                sent.append(message)

            async def inner(scope, recv, send):
                await send(
                    {"type": "http.response.start", "status": 200, "headers": []}
                )
                await send({"type": "http.response.body", "body": b"ok"})

            scope = {
                "type": "http",
                "method": "POST",
                "headers": [(b"content-length", b"5")],
            }
            mw = BodySizeLimitMiddleware(inner, max_bytes=1024)
            await mw(scope, receive, send)
            return sent

        sent = asyncio.run(run())

        assert sent[0]["status"] == 200


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json
