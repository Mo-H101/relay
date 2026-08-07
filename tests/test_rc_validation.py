"""
Release Candidate Validation Phase: production-profile gateway validation.

Brings up the REAL Relay application code (settings object, routers, HTTP
client stack, telemetry/health/quality stores, SQLite persistence) in the
documented production configuration and exercises it against scripted
loopback upstreams standing in for NVIDIA and OpenAI. The full reliability
matrix (timeout, 429, invalid response, provider unavailable, mid-stream
failure, retry/failover) is deterministic and repeatable; the live cloud
smoke test against real keys is a separate opt-in script:
``tests/run_live_smoke.py``.

The production profile mirrors docs/configuration.md + docs/deployment.md:
both cloud providers enabled, API-key auth, health-aware routing, adaptive
routing, quality feedback, the decision engine, telemetry, and SQLite
persistence. Local providers stay disabled (deferred after the cloud
gateway is stable).

This is a release gate: if the ``openai`` SDK (a pinned dev dependency)
is missing, this suite fails collection with ``RuntimeError`` instead of
silently skipping, so a broken test environment can never pass the gate.
"""

import asyncio
import time

import pytest

try:
    import openai  # required gate dependency (pinned in requirements-dev.txt)
except ImportError as exc:  # pragma: no cover - release-gate failure path
    raise RuntimeError(
        "RC validation gate requires the 'openai' SDK (pinned in "
        "requirements-dev.txt as openai==2.52.0). The gate must never skip "
        "silently: install the dev dependencies and re-run this suite."
    ) from exc

import httpx
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core import relay as relay_module
from app.main import app as fastapi_app
from app.providers.base import Provider
from app.services.ops_store import ops_store

from tests.conformance_helpers import (
    MockOpenAIProvider,
    parse_sse,
)

import app.api.admin
import app.api.chat
import app.api.decision
import app.api.diagnostics
import app.api.feedback
import app.api.health
import app.api.metrics
import app.api.openai
import app.api.providers

NVIDIA_MODELS = [
    "deepseek-ai/deepseek-r1",
    "meta/llama-3.3-70b-instruct",
]
OPENAI_MODELS = [
    "gpt-3.5-turbo",
    "gpt-4o-mini",
    "meta/llama-3.3-70b-instruct",
]
SHARED_MODEL = "meta/llama-3.3-70b-instruct"

AUTH_KEY = "rc-test-key"
AUTH_HEADERS = {"Authorization": f"Bearer {AUTH_KEY}"}


def _completion(
    content,
    model="rc-model",
    finish_reason="stop",
    usage=None,
    completion_id="chatcmpl-rc",
):
    body = {
        "id": completion_id,
        "object": "chat.completion",
        "created": 1700000000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    else:
        body["usage"] = {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        }
    return body


def _chunk(delta, finish_reason=None, model="rc-model"):
    return {
        "id": "chatcmpl-rc",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


def _wire_relay(relay_obj, monkeypatch):
    """
    Point every API module's imported ``relay`` name at a fresh Relay so
    requests exercise the production router code against the new instance.
    """
    for module in (
        app.api.admin,
        app.api.chat,
        app.api.decision,
        app.api.diagnostics,
        app.api.feedback,
        app.api.health,
        app.api.metrics,
        app.api.openai,
        app.api.providers,
    ):
        monkeypatch.setattr(module, "relay", relay_obj)


@pytest.fixture
def nvidia_mock():
    mock = MockOpenAIProvider()
    mock.start()
    yield mock
    mock.stop()


@pytest.fixture
def openai_mock():
    mock = MockOpenAIProvider()
    mock.start()
    yield mock
    mock.stop()


@pytest.fixture
def prod_profile(monkeypatch, tmp_path):
    """
    Apply the documented production configuration to the shared settings
    object. Each test gets a private persistence database under tmp_path.
    """
    monkeypatch.setattr(settings, "nvidia_enabled", True)
    monkeypatch.setattr(settings, "openai_enabled", True)
    monkeypatch.setattr(settings, "relay_api_key", AUTH_KEY)
    monkeypatch.setattr(settings, "telemetry_enabled", True)
    monkeypatch.setattr(settings, "health_feedback_enabled", True)
    monkeypatch.setattr(settings, "health_aware_routing", True)
    monkeypatch.setattr(settings, "adaptive_routing_enabled", True)
    monkeypatch.setattr(settings, "adaptive_min_samples", 2)
    monkeypatch.setattr(settings, "quality_feedback_enabled", True)
    monkeypatch.setattr(settings, "quality_feedback_min_samples", 2)
    monkeypatch.setattr(settings, "task_classification_enabled", True)
    monkeypatch.setattr(settings, "task_catalog_enabled", True)
    monkeypatch.setattr(settings, "decision_engine_enabled", True)
    monkeypatch.setattr(settings, "persistence_enabled", True)
    monkeypatch.setattr(
        settings,
        "persistence_path",
        str(tmp_path / "platform.db"),
    )
    monkeypatch.setattr(settings, "persistence_flush_interval_seconds", 1)
    monkeypatch.setattr(settings, "max_retries", 1)
    monkeypatch.setattr(settings, "request_timeout", 10)
    ops_store.clear()
    return settings


@pytest.fixture
def prod_components(prod_profile, nvidia_mock, openai_mock, monkeypatch):
    """
    Build fresh production-profile Relay instances wired into the API
    modules, with NVIDIA/OpenAI pointing at the scripted loopback upstreams
    (proxy bypassed, production names/priorities kept).
    """
    relays = []

    def _build(nvidia_models=None, openai_models=None):
        def nvidia_provider():
            return Provider(
                name="NVIDIA",
                base_url=nvidia_mock.base_url,
                api_key="rc-nvidia-key",
                priority=10,
                models=list(nvidia_models or NVIDIA_MODELS),
                proxy="",
            )

        def openai_provider():
            return Provider(
                name="OpenAI",
                base_url=openai_mock.base_url,
                api_key="rc-openai-key",
                priority=5,
                models=list(openai_models or OPENAI_MODELS),
                proxy="",
            )

        # Registry-driven loading builds providers from real settings; the
        # production profile injects scripted loopback providers instead, so
        # the registry build is bypassed and the mocks are registered directly
        # (P6.3, matching the test_openai_sdk_compat registry/manager pattern).
        monkeypatch.setattr(
            relay_module.Relay, "_load_providers", lambda self: None
        )

        relay_obj = relay_module.Relay()
        relay_obj.provider_manager.register(nvidia_provider())
        relay_obj.provider_manager.register(openai_provider())
        _wire_relay(relay_obj, monkeypatch)
        relays.append(relay_obj)
        return relay_obj

    yield _build

    for relay_obj in relays:
        try:
            if relay_obj.state_flusher is not None:
                relay_obj.state_flusher.stop()
        except Exception:
            pass
        try:
            if relay_obj.state_store is not None:
                relay_obj.state_store.close()
        except Exception:
            pass


@pytest.fixture
def rc_client():
    with TestClient(fastapi_app) as client:
        yield client


def _post(client, path, json=None, headers=None, **kwargs):
    merged = dict(AUTH_HEADERS)
    merged.update(headers or {})
    return client.post(path, json=json, headers=merged, **kwargs)


def _get(client, path, headers=None, **kwargs):
    merged = dict(AUTH_HEADERS)
    merged.update(headers or {})
    return client.get(path, headers=merged, **kwargs)


def _sdk_client():
    transport = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fastapi_app),
        base_url="http://relay-rc-test",
    )
    return openai.AsyncOpenAI(
        base_url="http://relay-rc-test/v1",
        api_key=AUTH_KEY,
        http_client=transport,
    )


class TestProductionGatewayWorkflow:
    """OpenAI surface (/v1/chat/completions) as a production drop-in."""

    def test_sdk_non_stream_round_trip(
        self, prod_components, nvidia_mock, rc_client
    ):
        prod_components()
        model = "deepseek-ai/deepseek-r1"
        nvidia_mock.script(
            json_body=_completion("from-nvidia", model=model)
        )

        async def run():
            client = _sdk_client()
            try:
                return await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "hello"}],
                )
            finally:
                await client.close()

        resp = asyncio.run(run())
        assert resp.model == model
        assert resp.choices[0].message.role == "assistant"
        assert resp.choices[0].message.content == "from-nvidia"
        assert resp.choices[0].finish_reason == "stop"
        assert resp.usage.total_tokens == 2

    def test_sdk_stream_with_usage(
        self, prod_components, nvidia_mock, rc_client
    ):
        prod_components()
        model = "deepseek-ai/deepseek-r1"
        nvidia_mock.script(
            stream=[
                _chunk({"role": "assistant", "content": "Hel"}, model=model),
                _chunk({"content": "lo"}, model=model),
                _chunk({}, finish_reason="stop", model=model),
                {
                    "id": "chatcmpl-rc",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": model,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 1,
                        "total_tokens": 3,
                    },
                },
            ]
        )

        async def run():
            client = _sdk_client()
            try:
                chunks = []
                stream = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "hi"}],
                    stream=True,
                    stream_options={"include_usage": True},
                )
                async for chunk in stream:
                    chunks.append(chunk)
                return chunks
            finally:
                await client.close()

        chunks = asyncio.run(run())
        text = "".join(
            c.choices[0].delta.content or ""
            for c in chunks
            if c.choices
        )
        assert text == "Hello"
        usage_chunks = [c for c in chunks if c.usage is not None]
        assert len(usage_chunks) == 1
        assert usage_chunks[0].usage.total_tokens == 3
        ids = {c.id for c in chunks}
        assert len(ids) == 1, "SDK stream must see one stable chunk id"

    def test_sdk_tool_calling_round_trip(
        self, prod_components, nvidia_mock, rc_client
    ):
        prod_components()
        model = "deepseek-ai/deepseek-r1"
        nvidia_mock.script(
            json_body={
                "id": "chatcmpl-rc-tool",
                "object": "chat.completion",
                "created": 1700000000,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_2",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": '{"city": "Lyon"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 6,
                    "total_tokens": 14,
                },
            }
        )

        async def run():
            client = _sdk_client()
            try:
                return await client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "user", "content": "Weather in Paris?"},
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": '{"city": "Paris"}',
                                    },
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "call_1",
                            "content": '{"temp": 20}',
                        },
                        {"role": "user", "content": "Thanks!"},
                    ],
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "description": "Get weather.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {},
                                },
                            },
                        }
                    ],
                    tool_choice="auto",
                )
            finally:
                await client.close()

        resp = asyncio.run(run())
        msg = resp.choices[0].message
        assert resp.choices[0].finish_reason == "tool_calls"
        assert msg.tool_calls is not None
        tool_call = msg.tool_calls[0]
        assert tool_call.id == "call_2"
        assert tool_call.function.name == "get_weather"
        assert tool_call.function.arguments == '{"city": "Lyon"}'

        recorded = nvidia_mock.requests[0]["body"]
        assert recorded["tool_choice"] == "auto"
        assert recorded["messages"][1]["tool_calls"][0]["id"] == "call_1"
        assert recorded["messages"][2]["tool_call_id"] == "call_1"
        assert recorded["messages"][2]["content"] == '{"temp": 20}'

    def test_sdk_streaming_tool_calls(
        self, prod_components, nvidia_mock, rc_client
    ):
        prod_components()
        model = "deepseek-ai/deepseek-r1"
        nvidia_mock.script(
            stream=[
                _chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_2",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": "",
                                },
                            }
                        ],
                    },
                    model=model,
                ),
                _chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": '{"ci'},
                            }
                        ]
                    },
                    model=model,
                ),
                _chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": 'ty": "Lyon"}'},
                            }
                        ]
                    },
                    model=model,
                ),
                _chunk({}, finish_reason="tool_calls", model=model),
            ]
        )

        async def run():
            client = _sdk_client()
            try:
                chunks = []
                stream = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "hi"}],
                    stream=True,
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "description": "Get weather.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {},
                                },
                            },
                        }
                    ],
                )
                async for chunk in stream:
                    chunks.append(chunk)
                return chunks
            finally:
                await client.close()

        chunks = asyncio.run(run())
        last = chunks[-1]
        assert last.choices[0].finish_reason == "tool_calls"
        tool_calls = chunks[0].choices[0].delta.tool_calls
        assert tool_calls is not None
        assert tool_calls[0].id == "call_2"
        args = "".join(
            c.choices[0].delta.tool_calls[0].function.arguments or ""
            for c in chunks
            if c.choices and c.choices[0].delta.tool_calls
        )
        assert args == '{"city": "Lyon"}'

    def test_verbatim_payload_forwarding(self, prod_components, nvidia_mock, rc_client):
        prod_components()
        model = "deepseek-ai/deepseek-r1"
        nvidia_mock.script(json_body=_completion("ok", model=model))

        request_body = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are precise."},
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "earlier answer"},
                {"role": "user", "content": "second"},
            ],
            "temperature": 0.3,
            "top_p": 0.9,
            "max_tokens": 64,
            "seed": 7,
            "stop": ["STOP"],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": "auto",
        }

        response = _post(
            rc_client, "/v1/chat/completions", json=request_body
        )

        assert response.status_code == 200
        assert (
            response.json()["choices"][0]["message"]["content"] == "ok"
        )

        recorded = nvidia_mock.requests[0]["body"]
        assert recorded["messages"] == request_body["messages"]
        assert recorded["model"] == model
        assert recorded["temperature"] == 0.3
        assert recorded["top_p"] == 0.9
        assert recorded["max_tokens"] == 64
        assert recorded["seed"] == 7
        assert recorded["stop"] == ["STOP"]
        assert recorded["tool_choice"] == "auto"
        assert recorded["tools"] == request_body["tools"]
        # Verbatim passthrough: fields the caller did not send are not
        # invented by Relay.
        assert "frequency_penalty" not in recorded
        assert "presence_penalty" not in recorded
        assert "stream" not in recorded
        assert "user" not in recorded

    def test_openai_error_shape_unknown_model(self, prod_components, rc_client):
        prod_components()
        response = _post(
            rc_client,
            "/v1/chat/completions",
            json={
                "model": "no-such-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 400
        body = response.json()
        assert "error" in body and "detail" not in body
        assert body["error"]["code"] == "model_not_found"
        assert response.headers["X-Relay-Correlation-Id"]

    def test_tool_choice_without_tools_is_400(self, prod_components, rc_client):
        prod_components()
        response = _post(
            rc_client,
            "/v1/chat/completions",
            json={
                "model": "deepseek-ai/deepseek-r1",
                "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": "auto",
            },
        )
        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "invalid_request"

    def test_models_listing(self, prod_components, rc_client):
        prod_components()
        response = _get(rc_client, "/v1/models")
        assert response.status_code == 200
        ids = {entry["id"] for entry in response.json()["data"]}
        assert ids == set(NVIDIA_MODELS) | set(OPENAI_MODELS)

    def test_auth_enforced(self, prod_components, rc_client):
        prod_components()
        unauth = rc_client.post(
            "/v1/chat/completions",
            json={
                "model": "deepseek-ai/deepseek-r1",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert unauth.status_code == 401
        # Public allowlist stays reachable without a key.
        assert rc_client.get("/health").status_code == 200
        assert rc_client.get("/").status_code == 200

    def test_streaming_stable_id_and_correlation(
        self, prod_components, nvidia_mock, rc_client
    ):
        prod_components()
        model = "deepseek-ai/deepseek-r1"
        nvidia_mock.script(
            stream=[
                _chunk({"role": "assistant", "content": "a"}, model=model),
                _chunk({"content": "b"}, model=model),
                _chunk({}, finish_reason="stop", model=model),
            ]
        )

        response = _post(
            rc_client,
            "/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        events = parse_sse(response.text)
        assert events[-1] == "[DONE]"
        chunks = [e for e in events if isinstance(e, dict)]
        ids = {c["id"] for c in chunks}
        assert len(ids) == 1
        assert response.headers["X-Relay-Correlation-Id"]


class TestNativeChatWorkflow:
    """Native /chat endpoint under the production profile."""

    def test_chat_happy_path_routes_to_priority_provider(
        self, prod_components, nvidia_mock, rc_client
    ):
        prod_components()
        nvidia_mock.script(
            json_body=_completion("hello from nvidia", model="deepseek-ai/deepseek-r1")
        )

        response = _post(rc_client, "/chat", json={"message": "hello"})

        assert response.status_code == 200
        body = response.json()
        assert body["provider"] == "NVIDIA"
        assert body["response"] == "hello from nvidia"
        assert response.headers["X-Relay-Correlation-Id"]

    def test_chat_classifies_task(
        self, prod_components, nvidia_mock, rc_client, monkeypatch
    ):
        relay = prod_components()
        nvidia_mock.script(json_body=_completion("ok", model="deepseek-ai/deepseek-r1"))
        captured = {}
        original = relay.achat

        async def wrapped(message, task=None, **kwargs):
            captured["task"] = task
            return await original(message, task=task, **kwargs)

        monkeypatch.setattr(relay, "achat", wrapped)

        response = _post(
            rc_client, "/chat", json={"message": "describe this image"}
        )

        assert response.status_code == 200
        assert captured["task"] == "vision"

    def test_chat_502_when_all_providers_fail(
        self, prod_components, nvidia_mock, openai_mock, rc_client
    ):
        prod_components(nvidia_models=["deepseek-ai/deepseek-r1"], openai_models=[])
        nvidia_mock.script(
            error=500,
            body={
                "error": {
                    "message": "boom",
                    "type": "server_error",
                    "code": "server_error",
                }
            },
        )

        response = _post(rc_client, "/chat", json={"message": "hi"})

        assert response.status_code == 502
        assert response.headers["X-Relay-Correlation-Id"]


class TestReliabilityMatrix:
    """Deterministic failure/recovery behavior via scripted upstreams."""

    def test_retry_on_5xx_then_success(
        self, prod_components, nvidia_mock, rc_client
    ):
        relay = prod_components()
        model = "deepseek-ai/deepseek-r1"
        nvidia_mock.script(
            error=500,
            body={
                "error": {
                    "message": "server hiccup",
                    "type": "server_error",
                    "code": "server_error",
                }
            },
        )
        nvidia_mock.script(json_body=_completion("recovered", model=model))

        response = _post(
            rc_client,
            "/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "recovered"
        assert len(nvidia_mock.requests) == 2
        # The /v1 surface records every attempt, matching the /chat
        # pipeline: the failed attempt feeds a real failure signal and
        # the retried success records its own success.
        stats = relay.telemetry.get("NVIDIA", model)
        assert stats.request_count == 2
        assert stats.success_count == 1
        assert stats.failure_count == 1

    def test_retry_on_429_ignores_retry_after(
        self, prod_components, nvidia_mock, rc_client
    ):
        prod_components()
        model = "deepseek-ai/deepseek-r1"
        nvidia_mock.script(
            error=429,
            body={
                "error": {
                    "message": "rate limited",
                    "type": "rate_limit_error",
                    "code": "rate_limit_exceeded",
                }
            },
            headers={"Retry-After": "1"},
        )
        nvidia_mock.script(json_body=_completion("after-ratelimit", model=model))

        started = time.monotonic()
        response = _post(
            rc_client,
            "/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        elapsed = time.monotonic() - started

        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "after-ratelimit"
        assert len(nvidia_mock.requests) == 2
        # Relay retries immediately and does not honor Retry-After. This
        # is a documented known limitation; the test proves current
        # behavior so the RC deliverables can state it precisely.
        assert elapsed < 1.0, "Retry must not sleep for the 1s Retry-After"

    def test_timeout_then_retry_success(
        self, prod_components, nvidia_mock, rc_client, monkeypatch
    ):
        relay = prod_components()
        monkeypatch.setattr(settings, "request_timeout", 1)
        model = "deepseek-ai/deepseek-r1"
        nvidia_mock.script(delay=3, json_body=_completion("too-late", model=model))
        nvidia_mock.script(json_body=_completion("on-time", model=model))

        response = _post(
            rc_client,
            "/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "on-time"
        assert len(nvidia_mock.requests) == 2
        stats = relay.telemetry.get("NVIDIA", model)
        assert stats.request_count == 2
        assert stats.success_count == 1
        assert stats.failure_count == 1

    def test_malformed_provider_response_retries(
        self, prod_components, nvidia_mock, rc_client
    ):
        relay = prod_components()
        model = "deepseek-ai/deepseek-r1"
        nvidia_mock.script(raw_body="<html>not json at all</html>")
        nvidia_mock.script(json_body=_completion("clean", model=model))

        response = _post(
            rc_client,
            "/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "clean"
        assert len(nvidia_mock.requests) == 2
        stats = relay.telemetry.get("NVIDIA", model)
        assert stats.request_count == 2
        assert stats.success_count == 1
        assert stats.failure_count == 1

    def test_provider_unavailable_fails_over_across_providers(
        self, prod_components, nvidia_mock, openai_mock, rc_client
    ):
        prod_components()
        nvidia_mock.stop()
        openai_mock.script(
            json_body=_completion("from-openai", model=SHARED_MODEL)
        )

        response = _post(
            rc_client,
            "/v1/chat/completions",
            json={
                "model": SHARED_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "from-openai"
        assert len(openai_mock.requests) == 1

    def test_all_providers_fail_returns_502_error_shape(
        self, prod_components, nvidia_mock, openai_mock, rc_client
    ):
        prod_components()
        error_body = {
            "error": {
                "message": "down",
                "type": "server_error",
                "code": "server_error",
            }
        }
        nvidia_mock.script(error=500, body=error_body)
        nvidia_mock.script(error=500, body=error_body)
        openai_mock.script(error=500, body=error_body)
        openai_mock.script(error=500, body=error_body)

        response = _post(
            rc_client,
            "/v1/chat/completions",
            json={
                "model": SHARED_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 502
        body = response.json()
        assert "error" in body and "detail" not in body
        assert body["error"]["code"] == "provider_error"
        assert response.headers["X-Relay-Correlation-Id"]

    def test_mid_stream_failure_emits_error_and_records(
        self, prod_components, nvidia_mock, rc_client, monkeypatch
    ):
        relay = prod_components()
        monkeypatch.setattr(settings, "request_timeout", 1)
        nvidia_mock.script(
            stream_then_hang=[
                _chunk({"role": "assistant", "content": "partial"}, model=SHARED_MODEL),
                _chunk({"content": " text"}, model=SHARED_MODEL),
            ]
        )

        response = _post(
            rc_client,
            "/v1/chat/completions",
            json={
                "model": SHARED_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        events = parse_sse(response.text)
        assert events[-1] == "[DONE]"
        error_chunks = [
            e
            for e in events
            if isinstance(e, dict) and "error" in e
        ]
        assert len(error_chunks) == 1
        assert error_chunks[0]["error"]["type"] == "stream_error"
        # Mid-stream failure is recorded as a timeout for the streaming pair.
        stats = relay.telemetry.get("NVIDIA", SHARED_MODEL)
        assert stats is not None
        assert stats.failure_count >= 1
        assert stats.recent_failures[0].failure_type == "timeout"

    def test_stream_start_error_surfaces_provider_body(
        self, prod_components, nvidia_mock, rc_client
    ):
        # A streamed /v1 request whose upstream rejects it must surface the
        # provider's actual error body, not httpx's internal "Attempted to
        # access streaming response content" ResponseNotRead message.
        prod_components()
        nvidia_mock.script(
            error=500,
            body={
                "error": {
                    "message": "stream rejected",
                    "type": "server_error",
                    "code": "server_error",
                }
            },
        )

        response = _post(
            rc_client,
            "/v1/chat/completions",
            json={
                "model": "deepseek-ai/deepseek-r1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

        assert response.status_code == 502
        body = response.json()
        assert body["error"]["code"] == "provider_error"
        assert "stream rejected" in body["error"]["message"]
        assert "Attempted to access" not in body["error"]["message"]


class TestRoutingIntelligence:
    """Learning, feedback, persistence, and diagnostics under the profile."""

    def test_health_learning_reroutes_away_from_degraded_model(
        self, prod_components, nvidia_mock, openai_mock, rc_client
    ):
        # NVIDIA hosts only the shared model; OpenAI hosts it too plus its
        # own models. After NVIDIA's shared model takes a server error it
        # is marked degraded and dropped from /chat candidates.
        # NOTE: the failure goes through /chat, which records per-attempt
        # health feedback. The /v1 surface records per-attempt feedback too
        # (see TestV1HealthLearning in test_openai_api.py).
        prod_components(
            nvidia_models=[SHARED_MODEL],
            openai_models=["gpt-3.5-turbo", SHARED_MODEL],
        )
        error_body = {
            "error": {
                "message": "boom",
                "type": "server_error",
                "code": "server_error",
            }
        }
        nvidia_mock.script(error=500, body=error_body)
        nvidia_mock.script(error=500, body=error_body)
        openai_mock.script(json_body=_completion("gpt-ok", model="gpt-3.5-turbo"))
        openai_mock.script(json_body=_completion("gpt-ok-2", model="gpt-3.5-turbo"))

        # 1. Trip the server error on NVIDIA's shared model via /chat
        # (retried once, then failed over to OpenAI).
        first = _post(rc_client, "/chat", json={"message": "hello"})
        assert first.status_code == 200
        assert first.json()["provider"] == "OpenAI"
        assert len(nvidia_mock.requests) == 2

        # 2. /chat must now avoid NVIDIA entirely: no new upstream calls.
        response = _post(rc_client, "/chat", json={"message": "hello"})
        assert response.status_code == 200
        assert response.json()["provider"] == "OpenAI"
        assert len(nvidia_mock.requests) == 2, (
            "degraded NVIDIA must not receive new calls"
        )

        # 3. Learned health is visible in diagnostics.
        snapshot = _get(rc_client, "/diagnostics").json()
        learned_providers = {
            entry["provider"]: entry for entry in snapshot["learned_health"]["providers"]
        }
        nvidia_entry = learned_providers.get("NVIDIA")
        assert nvidia_entry is not None
        assert SHARED_MODEL in nvidia_entry["degraded_models"]

    def test_adaptive_telemetry_learns_ewma(
        self, prod_components, rc_client
    ):
        relay = prod_components()
        model = "deepseek-ai/deepseek-r1"
        for i in range(4):
            relay.telemetry.record_attempt(
                "NVIDIA", model, success=(i % 2 == 0), latency_ms=100
            )

        stats = relay.telemetry.get("NVIDIA", model)
        assert stats.request_count == 4
        assert stats.success_count == 2
        assert stats.failure_count == 2
        assert 0.0 < stats.ewma_success < 1.0

        snapshot = _get(rc_client, "/diagnostics").json()
        adaptive = {
            (entry["provider"], entry["model"]): entry
            for entry in snapshot["adaptive"]["state"]
        }
        entry = adaptive.get(("NVIDIA", model))
        assert entry is not None
        assert entry["request_count"] == 4
        assert entry["ewma_success"] == stats.ewma_success

    def test_quality_feedback_recorded_and_exposed(
        self, prod_components, rc_client
    ):
        prod_components()
        pair = {"provider": "NVIDIA", "model": "deepseek-ai/deepseek-r1"}

        r1 = _post(
            rc_client,
            "/feedback",
            json={"provider": pair["provider"], "model": pair["model"], "rating": 5},
        )
        assert r1.status_code == 202
        assert r1.json()["stored"] is True

        r2 = _post(
            rc_client,
            "/feedback",
            json={"provider": pair["provider"], "model": pair["model"], "rating": 4},
        )
        assert r2.status_code == 202
        assert r2.json()["stored"] is True

        snapshot = _get(rc_client, "/diagnostics").json()
        quality_pairs = {
            (entry["provider"], entry["model"]): entry
            for entry in snapshot["quality"]["pairs"]
        }
        aggregate = quality_pairs.get(("NVIDIA", "deepseek-ai/deepseek-r1"))
        assert aggregate is not None
        assert aggregate["sample_count"] == 2
        assert aggregate["positive_rate"] == 1.0
        assert aggregate["confidence"] == 1.0
        assert aggregate["ewma_score"] > 0.0

    def test_quality_feedback_duplicate_correlation_deduped(
        self, prod_components, rc_client
    ):
        prod_components()
        payload = {
            "provider": "OpenAI",
            "model": "gpt-3.5-turbo",
            "rating": 4,
            "correlation_id": "same-id",
        }
        first = _post(rc_client, "/feedback", json=payload)
        second = _post(rc_client, "/feedback", json=payload)
        assert first.status_code == 202
        assert first.json()["stored"] is True
        assert second.status_code == 202
        assert second.json()["stored"] is False

    def test_quality_feedback_rejects_content_fields(
        self, prod_components, rc_client
    ):
        prod_components()
        response = _post(
            rc_client,
            "/feedback",
            json={
                "provider": "OpenAI",
                "model": "gpt-3.5-turbo",
                "rating": 5,
                "message": "nope",
            },
        )
        assert response.status_code in (400, 422)

    def test_persistence_survives_restart(
        self, prod_components, rc_client
    ):
        model = "deepseek-ai/deepseek-r1"
        relay1 = prod_components()
        relay1.telemetry.record_attempt(
            "NVIDIA", model, success=True, latency_ms=50
        )
        relay1.telemetry.record_attempt(
            "NVIDIA", model, success=False, latency_ms=30, failure_type="server_error"
        )
        relay1.health_store.record_failure("NVIDIA", model, "server_error")
        relay1.quality_store.record("OpenAI", "gpt-3.5-turbo", 5)

        relay1.state_flusher.flush()
        relay1.state_flusher.stop()
        relay1.state_store.close()

        relay2 = prod_components()

        stats = relay2.telemetry.get("NVIDIA", model)
        assert stats is not None
        assert stats.request_count == 2
        assert stats.success_count == 1
        assert stats.failure_count == 1

        learned = relay2.health_store.learned("NVIDIA")
        assert learned is not None
        assert model in learned.degraded_models

        quality = relay2.quality_store.aggregate("OpenAI", "gpt-3.5-turbo")
        assert quality is not None
        assert quality["sample_count"] == 1

    def test_diagnostics_accuracy(self, prod_components, nvidia_mock, rc_client):
        relay = prod_components()
        model = "deepseek-ai/deepseek-r1"
        nvidia_mock.script(json_body=_completion("ok", model=model))
        nvidia_mock.script(json_body=_completion("ok2", model=model))

        r1 = _post(
            rc_client,
            "/v1/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": "a"}]},
        )
        r2 = _post(
            rc_client,
            "/v1/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": "b"}]},
        )
        assert r1.status_code == 200
        assert r2.status_code == 200

        # Flush so diagnostics reads persisted state (telemetry flushes on a
        # 1s interval; the previous requests completed well before that).
        relay.state_flusher.flush()

        snapshot = _get(rc_client, "/diagnostics").json()

        telemetry = snapshot["telemetry"]
        assert telemetry["summary"]["total_requests"] == 2
        assert telemetry["summary"]["total_successes"] == 2

        providers = snapshot["providers"]["providers"]
        assert {p["name"] for p in providers} == {"NVIDIA", "OpenAI"}
        for entry in providers:
            # Diagnostics is passive: it must never probe providers.
            assert entry["status"] == "not_checked"

        persistence = snapshot["persistence"]
        assert persistence["enabled"] is True
        assert persistence["available"] is True
        assert persistence["storage_status"] == "ok"
        assert persistence["schema_version"] is not None
        assert persistence["learned_memory"]["telemetry_pairs"] == 1

        operations = snapshot["operations"]
        assert operations["requests"] >= 2
        assert operations["successes"] >= 2

        assert relay.telemetry.get("NVIDIA", model).request_count == 2
