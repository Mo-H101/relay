"""
P4.3.4 Provider conformance test suite (Focus E).

Registry-driven, capability-matrix-paced parametrized suite covering all
six RUNTIME_READY providers against mocked wire endpoints. Tests never
branch per provider: the capability matrix (PROVIDER_CAPABILITIES) plus
the wire family (the only secondary axis) selects behavior.

The mock harness in tests/conformance_helpers.py monkeypatches the
module-level httpx entry points shared by every client, so one scripted
handler set exercises sync and async surfaces alike with a recorded
request log for wire-level assertions.
"""

import json

import httpx
import pytest

from app.core.config import settings
from app.providers.exceptions import ProviderHTTPError, ProviderTimeout
from app.providers.registry import RUNTIME_READY
from app.services.client_registry import ClientRegistry
from app.services.failure_classifier import RETRYABLE, FailureKind, classify

from tests.conformance_helpers import (
    ANTHROPIC_WIRE,
    DEFAULT_MODEL,
    GEMINI_WIRE,
    OLLAMA_WIRE,
    OPENAI_WIRE,
    MockStreamResponse,
    build_handlers,
    build_provider_instance,
    cap,
    chat_body,
    chat_stream_lines,
    endpoint_pattern,
    error_response,
    install_http_mocks,
    json_response,
    models_body,
    wire_of,
)

RUNTIME_PROVIDER_IDS = sorted(RUNTIME_READY)

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


@pytest.fixture(params=RUNTIME_PROVIDER_IDS)
def provider_id(request):
    return request.param


@pytest.fixture
def provider(provider_id):
    return build_provider_instance(provider_id)


@pytest.fixture
def client(provider_id):
    return ClientRegistry().get(provider_id)


def chat_ok(provider_id, body=None, lines=None):
    """Script the chat endpoint: JSON for non-stream, SSE/NDJSON otherwise."""
    def handler(method, url, **kwargs):
        payload = kwargs.get("json") or {}
        is_stream = bool(payload.get("stream", False))
        if wire_of(provider_id) == GEMINI_WIRE:
            is_stream = is_stream or "streamGenerateContent" in url
        if is_stream:
            return MockStreamResponse(
                chat_stream_lines(provider_id) if lines is None else lines
            )
        return json_response(chat_body(provider_id) if body is None else body)
    return handler


def discovery_ok(provider_id):
    return lambda method, url, **kwargs: json_response(models_body(provider_id))


def full_payload(stream=False):
    return {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hi there"},
        ],
        "tools": [TOOL_DEF],
        "tool_choice": "auto",
        "stream": stream,
    }


def tool_payload(stream=False):
    return {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": "weather?"}],
        "tools": [TOOL_DEF],
        "tool_choice": "auto",
        "stream": stream,
    }


def assert_translated_payload(provider_id, sent, payload):
    """Wire-family assertions on a natively translated chat payload."""
    wire = wire_of(provider_id)
    if wire != GEMINI_WIRE:
        assert sent["model"] == DEFAULT_MODEL
    if wire == OLLAMA_WIRE:
        assert [m["role"] for m in sent["messages"]] == ["system", "user"]
        assert sent["tools"][0]["type"] == "function"
    elif wire == ANTHROPIC_WIRE:
        assert sent["system"] == "You are helpful."
        assert [m["role"] for m in sent["messages"]] == ["user"]
        assert sent["tools"][0]["name"] == "get_weather"
        assert sent["max_tokens"] == payload.get("max_tokens") or 512
    else:  # GEMINI_WIRE
        assert sent["systemInstruction"]["parts"][0]["text"] == "You are helpful."
        assert [c["role"] for c in sent["contents"]] == ["user"]
        assert sent["tools"]


def collect_text(chunks):
    parts = []
    for chunk in chunks:
        delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
        if delta.get("content"):
            parts.append(delta["content"])
    return "".join(parts)


def collect_tool_calls(chunks):
    calls = []
    for chunk in chunks:
        delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
        for call in delta.get("tool_calls") or []:
            calls.append(call)
    return calls


def last_finish_reason(chunks):
    for chunk in reversed(chunks):
        choices = chunk.get("choices") or []
        if choices and choices[0].get("finish_reason"):
            return choices[0]["finish_reason"]
    return None


# ---------------------------------------------------------------------------
# Area 3: RUNTIME_READY providers implement the full contract surface
# ---------------------------------------------------------------------------

SURFACE_METHODS = [
    "chat",
    "chat_messages",
    "chat_stream",
    "chat_stream_messages",
    "list_models",
    "alist_models",
    "key_check",
    "probe_model",
    "aprobe_model",
    "connectivity_probe",
    "achat",
    "achat_messages",
    "achat_stream",
    "achat_stream_messages",
    "proxy_request_kwargs",
]


class TestContractSurface:
    def test_full_contract_surface(self, provider_id):
        instance = ClientRegistry().get(provider_id)
        for name in SURFACE_METHODS:
            assert callable(getattr(instance, name, None)), (
                f"{provider_id} is missing {name}"
            )
        if cap(provider_id, "check_model"):
            assert callable(instance.check_model)

    def test_registry_and_capability_matrix_are_consistent(self, provider_id):
        definition = ClientRegistry().get(provider_id)
        assert definition is not None
        assert wire_of(provider_id) in {
            OPENAI_WIRE, OLLAMA_WIRE, ANTHROPIC_WIRE, GEMINI_WIRE,
        }
        assert cap(provider_id, "discovery_endpoint")


# ---------------------------------------------------------------------------
# Area 4: chat() returns the assistant content string
# ---------------------------------------------------------------------------


class TestChat:
    def test_chat_returns_assistant_text(self, provider_id, provider, client, monkeypatch):
        recorder = install_http_mocks(
            monkeypatch,
            build_handlers(provider_id, chat_factory=chat_ok(provider_id)),
        )
        text = client.chat(provider, DEFAULT_MODEL, "hi")
        assert text == "Hello from the relay!"

    def test_chat_wire_contract(self, provider_id, provider, client, monkeypatch):
        recorder = install_http_mocks(
            monkeypatch,
            build_handlers(provider_id, chat_factory=chat_ok(provider_id)),
        )
        text = client.chat(provider, DEFAULT_MODEL, "hi")
        assert text == "Hello from the relay!"
        assert len(recorder.requests) == 1
        req = recorder.requests[0]
        assert req["method"] == "POST"
        assert req["timeout"] == settings.request_timeout
        chat_pattern = endpoint_pattern(provider_id)["chat"]
        if isinstance(chat_pattern, (list, tuple)):
            assert any(p in req["url"] for p in chat_pattern)
        else:
            assert chat_pattern in req["url"]
        if wire_of(provider_id) == GEMINI_WIRE:
            assert f"{DEFAULT_MODEL}:generateContent" in req["url"]
        else:
            assert req["json"]["model"] == DEFAULT_MODEL
        sent_text = (
            req["json"]["contents"][0]["parts"][0]["text"]
            if wire_of(provider_id) == GEMINI_WIRE
            else req["json"]["messages"][0]["content"]
        )
        assert "hi" in sent_text

    def test_gen_params_forwarding(self, provider_id, provider, client, monkeypatch):
        recorder = install_http_mocks(
            monkeypatch,
            build_handlers(provider_id, chat_factory=chat_ok(provider_id)),
        )
        client.chat(
            provider,
            DEFAULT_MODEL,
            "hi",
            temperature=0.7,
            top_p=0.9,
            max_tokens=64,
            stop="END",
        )
        sent = recorder.requests[0]["json"]
        if cap(provider_id, "gen_params"):
            assert sent["temperature"] == 0.7
            assert sent["top_p"] == 0.9
            assert sent["max_tokens"] == 64
            assert sent["stop"] == "END"
        else:
            assert "hi" in json.dumps(sent)


# ---------------------------------------------------------------------------
# Area 5: chat_messages() returns OpenAI-shaped responses and translates
# ---------------------------------------------------------------------------


class TestChatMessages:
    def test_chat_messages_openai_shape(self, provider_id, provider, client, monkeypatch):
        recorder = install_http_mocks(
            monkeypatch,
            build_handlers(provider_id, chat_factory=chat_ok(provider_id)),
        )
        response = client.chat_messages(provider, full_payload(stream=False))
        assert response["choices"][0]["message"]["content"] == "Hello from the relay!"
        assert response["choices"][0]["message"]["role"] == "assistant"
        assert response["choices"][0]["finish_reason"] == "stop"
        assert response["usage"]["total_tokens"] == 16
        assert recorder.requests[0]["timeout"] == settings.request_timeout

    def test_chat_messages_payload_forwarding(self, provider_id, provider, client, monkeypatch):
        recorder = install_http_mocks(
            monkeypatch,
            build_handlers(provider_id, chat_factory=chat_ok(provider_id)),
        )
        payload = full_payload(stream=False)
        client.chat_messages(provider, payload)
        sent = recorder.requests[0]["json"]
        if cap(provider_id, "chat_forwarded_verbatim"):
            assert sent == payload
        else:
            assert_translated_payload(provider_id, sent, payload)

    def test_chat_messages_translates_user_content(self, provider_id, provider, client, monkeypatch):
        recorder = install_http_mocks(
            monkeypatch,
            build_handlers(provider_id, chat_factory=chat_ok(provider_id)),
        )
        client.chat_messages(
            provider,
            {
                "model": DEFAULT_MODEL,
                "messages": [{"role": "user", "content": "hi there"}],
                "stream": False,
            },
        )
        sent = recorder.requests[0]["json"]
        assert "hi there" in json.dumps(sent)


# ---------------------------------------------------------------------------
# Area 6: tool-call round-trip and finish_reason per matrix
# ---------------------------------------------------------------------------


class TestToolCalls:
    def test_tool_call_round_trip(self, provider_id, provider, client, monkeypatch):
        handlers = build_handlers(
            provider_id,
            chat_factory=chat_ok(
                provider_id,
                body=chat_body(provider_id, tool_calls=True),
            ),
        )
        install_http_mocks(monkeypatch, handlers)
        response = client.chat_messages(provider, tool_payload(stream=False))
        message = response["choices"][0]["message"]
        assert message["tool_calls"]
        call = message["tool_calls"][0]
        assert call["type"] == "function"
        assert call["function"]["name"] == "get_weather"
        assert json.loads(call["function"]["arguments"]) == {"city": "Paris"}
        assert call["id"]
        assert response["choices"][0]["finish_reason"] == cap(
            provider_id, "tool_finish_reason"
        )

    def test_stream_tool_call_round_trip(self, provider_id, provider, client, monkeypatch):
        handlers = build_handlers(
            provider_id,
            chat_factory=chat_ok(
                provider_id,
                lines=chat_stream_lines(provider_id, tool_calls=True),
            ),
        )
        install_http_mocks(monkeypatch, handlers)
        chunks = list(client.chat_stream_messages(provider, tool_payload(stream=True)))
        calls = collect_tool_calls(chunks)
        assert calls
        names = [c["function"]["name"] for c in calls if c["function"].get("name")]
        assert "get_weather" in names
        arguments = "".join(
            c["function"]["arguments"] for c in calls if c["function"].get("arguments")
        )
        assert json.loads(arguments) == {"city": "Paris"}
        assert last_finish_reason(chunks) == cap(provider_id, "tool_finish_reason")


# ---------------------------------------------------------------------------
# Area 7: streaming deltas
# ---------------------------------------------------------------------------


class TestStreamDeltas:
    def test_chat_stream_joins_to_full_text(self, provider_id, provider, client, monkeypatch):
        recorder = install_http_mocks(
            monkeypatch,
            build_handlers(provider_id, chat_factory=chat_ok(provider_id)),
        )
        deltas = list(client.chat_stream(provider, DEFAULT_MODEL, "hi"))
        assert "".join(deltas) == "Hello world!"
        if wire_of(provider_id) == GEMINI_WIRE:
            assert "streamGenerateContent" in recorder.requests[0]["url"]
        else:
            assert recorder.requests[0]["json"]["stream"] is True
        assert recorder.requests[0]["timeout"] == settings.request_timeout


# ---------------------------------------------------------------------------
# Area 8: streaming chunk shape
# ---------------------------------------------------------------------------


class TestStreamChunks:
    def test_chat_stream_messages_shape(self, provider_id, provider, client, monkeypatch):
        handlers = build_handlers(
            provider_id,
            chat_factory=chat_ok(provider_id),
        )
        recorder = install_http_mocks(monkeypatch, handlers)
        chunks = list(
            client.chat_stream_messages(provider, full_payload(stream=True))
        )
        assert chunks
        assert all("choices" in chunk for chunk in chunks)
        assert collect_text(chunks) == "Hello world!"
        assert last_finish_reason(chunks) == "stop"
        assert recorder.requests[0]["timeout"] == settings.request_timeout


# ---------------------------------------------------------------------------
# Area 9: streamed usage is reported exactly once
# ---------------------------------------------------------------------------


class TestStreamUsage:
    def test_usage_emitted_exactly_once(self, provider_id, provider, client, monkeypatch):
        handlers = build_handlers(
            provider_id,
            chat_factory=chat_ok(provider_id),
        )
        install_http_mocks(monkeypatch, handlers)
        chunks = list(
            client.chat_stream_messages(provider, full_payload(stream=True))
        )
        usage_chunks = [c for c in chunks if c.get("usage")]
        assert len(usage_chunks) == 1
        assert usage_chunks[0]["usage"]["total_tokens"] == 16

    def test_nonstream_usage_reported(self, provider_id, provider, client, monkeypatch):
        install_http_mocks(
            monkeypatch,
            build_handlers(provider_id, chat_factory=chat_ok(provider_id)),
        )
        response = client.chat_messages(provider, full_payload(stream=False))
        assert response["usage"] == {
            "prompt_tokens": 7,
            "completion_tokens": 9,
            "total_tokens": 16,
        }


# ---------------------------------------------------------------------------
# Area 10: error handling, timeouts, and classification
# ---------------------------------------------------------------------------


class TestErrors:
    def test_http_error_raises_provider_http_error(self, provider_id, provider, client, monkeypatch):
        handlers = build_handlers(
            provider_id,
            chat_factory=lambda m, u, **k: error_response(
                provider_id, status=429, message="rate limited", retry_after="3"
            ),
        )
        install_http_mocks(monkeypatch, handlers)
        with pytest.raises(ProviderHTTPError) as exc:
            client.chat(provider, DEFAULT_MODEL, "hi")
        assert exc.value.status_code == 429
        assert exc.value.retry_after == 3.0
        assert "rate limited" in exc.value.message
        assert classify(exc.value) == FailureKind.RATE_LIMIT

    def test_timeout_raises_provider_timeout(self, provider_id, provider, client, monkeypatch):
        def timeout_handler(method, url, **kwargs):
            raise httpx.ReadTimeout("mock timeout")

        handlers = build_handlers(provider_id, chat_factory=timeout_handler)
        install_http_mocks(monkeypatch, handlers)
        with pytest.raises(ProviderTimeout) as exc:
            client.chat(provider, DEFAULT_MODEL, "hi")
        assert classify(exc.value) == FailureKind.TIMEOUT

    def test_failure_classifier_auth(self, provider_id, provider, client, monkeypatch):
        handlers = build_handlers(
            provider_id,
            chat_factory=lambda m, u, **k: error_response(
                provider_id, status=401, message="bad key", retry_after=None
            ),
        )
        install_http_mocks(monkeypatch, handlers)
        with pytest.raises(ProviderHTTPError) as exc:
            client.chat(provider, DEFAULT_MODEL, "hi")
        assert classify(exc.value) == FailureKind.AUTH_ERROR

    def test_failure_classifier_request_timeout_408(self, provider_id, provider, client, monkeypatch):
        handlers = build_handlers(
            provider_id,
            chat_factory=lambda m, u, **k: error_response(
                provider_id, status=408, message="request timeout", retry_after=None
            ),
        )
        install_http_mocks(monkeypatch, handlers)
        with pytest.raises(ProviderHTTPError) as exc:
            client.chat(provider, DEFAULT_MODEL, "hi")
        assert exc.value.status_code == 408
        assert classify(exc.value) == FailureKind.TIMEOUT


class TestErrorRedaction:
    def test_error_body_redacts_api_key(self, provider_id, provider, client, monkeypatch):
        leaked = f"upstream {provider.api_key} leaked"
        handlers = build_handlers(
            provider_id,
            chat_factory=lambda m, u, **k: error_response(
                provider_id, status=500, message=leaked, retry_after=None
            ),
        )
        install_http_mocks(monkeypatch, handlers)
        with pytest.raises(ProviderHTTPError) as exc:
            client.chat(provider, DEFAULT_MODEL, "hi")
        assert provider.api_key not in exc.value.message
        assert "[REDACTED]" in exc.value.message
        assert classify(exc.value) == FailureKind.SERVER_ERROR

    def test_501_not_implemented_not_retryable(self):
        """HTTP 501 Not Implemented is a permanent failure — retrying is
        always futile; failover to the next candidate is the correct path."""
        exc = ProviderHTTPError(501, "Not Implemented")
        assert classify(exc) == FailureKind.INVALID_REQUEST
        assert classify(exc) not in RETRYABLE


class TestRetryAfter:
    def test_retry_after_chat(self, provider_id, provider, client, monkeypatch):
        handlers = build_handlers(
            provider_id,
            chat_factory=lambda m, u, **k: error_response(
                provider_id, status=429, message="slow down", retry_after="5"
            ),
        )
        install_http_mocks(monkeypatch, handlers)
        with pytest.raises(ProviderHTTPError) as exc:
            client.chat(provider, DEFAULT_MODEL, "hi")
        assert exc.value.retry_after == 5.0

    def test_retry_after_chat_messages(self, provider_id, provider, client, monkeypatch):
        handlers = build_handlers(
            provider_id,
            chat_factory=lambda m, u, **k: error_response(
                provider_id, status=429, message="slow down", retry_after="5"
            ),
        )
        install_http_mocks(monkeypatch, handlers)
        with pytest.raises(ProviderHTTPError) as exc:
            client.chat_messages(provider, full_payload(stream=False))
        assert exc.value.retry_after == 5.0

    def test_retry_after_chat_stream(self, provider_id, provider, client, monkeypatch):
        stream_error = MockStreamResponse(
            lines=[],
            status_code=429,
            text=json.dumps(
                {
                    "error": {"message": "slow down"},
                }
            ),
            headers={"Retry-After": "5"},
        )
        handlers = build_handlers(
            provider_id,
            chat_factory=lambda m, u, **k: stream_error,
        )
        install_http_mocks(monkeypatch, handlers)
        gen = client.chat_stream(provider, DEFAULT_MODEL, "hi")
        with pytest.raises(ProviderHTTPError) as exc:
            next(gen)
        assert exc.value.status_code == 429
        assert exc.value.retry_after == 5.0

    @pytest.mark.asyncio
    async def test_retry_after_achat(self, provider_id, provider, client, monkeypatch):
        handlers = build_handlers(
            provider_id,
            chat_factory=lambda m, u, **k: error_response(
                provider_id, status=429, message="slow down", retry_after="5"
            ),
        )
        install_http_mocks(monkeypatch, handlers)
        with pytest.raises(ProviderHTTPError) as exc:
            await client.achat(provider, DEFAULT_MODEL, "hi")
        assert exc.value.retry_after == 5.0

    @pytest.mark.asyncio
    async def test_retry_after_achat_stream(self, provider_id, provider, client, monkeypatch):
        stream_error = MockStreamResponse(
            lines=[],
            status_code=429,
            text=json.dumps({"error": "slow down"}),
            headers={"Retry-After": "5"},
        )
        handlers = build_handlers(
            provider_id,
            chat_factory=lambda m, u, **k: stream_error,
        )
        install_http_mocks(monkeypatch, handlers)
        gen = client.achat_stream(provider, DEFAULT_MODEL, "hi")
        with pytest.raises(ProviderHTTPError) as exc:
            async for _ in gen:
                pass
        assert exc.value.retry_after == 5.0


# ---------------------------------------------------------------------------
# Area 11: auth conventions per wire family
# ---------------------------------------------------------------------------


class TestAuth:
    def test_chat_auth_convention(self, provider_id, provider, client, monkeypatch):
        recorder = install_http_mocks(
            monkeypatch,
            build_handlers(provider_id, chat_factory=chat_ok(provider_id)),
        )
        client.chat(provider, DEFAULT_MODEL, "hi")
        headers = recorder.requests[0].get("headers") or {}
        auth = cap(provider_id, "auth")
        if auth == "bearer":
            assert headers.get("Authorization") == "Bearer sk-test"
        elif auth == "x-api-key":
            assert headers.get("x-api-key") == "sk-test"
            assert "Authorization" not in headers
        elif auth == "x-goog-api-key":
            assert headers.get("x-goog-api-key") == "sk-test"
            assert "key=sk-test" not in recorder.requests[0]["url"]
            assert "Authorization" not in headers
        else:
            assert "Authorization" not in headers

    def test_keyless_chat_sends_no_bearer(self, provider_id, monkeypatch):
        keyless = build_provider_instance(provider_id, api_key="")
        recorder = install_http_mocks(
            monkeypatch,
            build_handlers(provider_id, chat_factory=chat_ok(provider_id)),
        )
        ClientRegistry().get(provider_id).chat(keyless, DEFAULT_MODEL, "hi")
        headers = recorder.requests[0].get("headers") or {}
        assert "Authorization" not in headers


# ---------------------------------------------------------------------------
# Area 12: discovery, connectivity, key check, model probing
# ---------------------------------------------------------------------------

EXPECTED_DISCOVERED = {
    OPENAI_WIRE: "gpt-4",
    OLLAMA_WIRE: "llama3:8b",
    ANTHROPIC_WIRE: "claude-3-5-sonnet",
    GEMINI_WIRE: "gemini-1.5-pro",
}


class TestDiscovery:
    def test_list_models_returns_normalized_ids(self, provider_id, provider, client, monkeypatch):
        recorder = install_http_mocks(
            monkeypatch,
            build_handlers(provider_id, discovery_factory=discovery_ok(provider_id)),
        )
        models = client.list_models(provider)
        assert models
        assert EXPECTED_DISCOVERED[wire_of(provider_id)] in models
        assert all("models/" not in model for model in models)
        assert recorder.requests[0]["timeout"] == 30
        assert endpoint_pattern(provider_id)["discovery"] in recorder.requests[0]["url"]

    def test_connectivity_probe(self, provider_id, provider, client, monkeypatch):
        recorder = install_http_mocks(
            monkeypatch,
            build_handlers(provider_id, discovery_factory=discovery_ok(provider_id)),
        )
        ok, details, latency = client.connectivity_probe(provider)
        assert ok is True
        assert details == "HTTP 200"
        assert recorder.requests[0]["timeout"] == 10

    def test_key_check(self, provider_id, provider, client, monkeypatch):
        if cap(provider_id, "auth") == "none":
            assert client.key_check(provider) == (None, "no api key required")
            return
        install_http_mocks(
            monkeypatch,
            build_handlers(provider_id, discovery_factory=discovery_ok(provider_id)),
        )
        status, _text = client.key_check(provider)
        assert status == 200

    def test_probe_model_healthy(self, provider_id, provider, client, monkeypatch):
        recorder = install_http_mocks(
            monkeypatch,
            build_handlers(provider_id, chat_factory=chat_ok(provider_id)),
        )
        probe = client.probe_model(provider, DEFAULT_MODEL)
        assert probe.healthy is True
        assert probe.status_code == 200
        assert recorder.requests[0]["timeout"] == 10

    def test_probe_model_unhealthy(self, provider_id, provider, client, monkeypatch):
        handlers = build_handlers(
            provider_id,
            chat_factory=lambda m, u, **k: error_response(
                provider_id, status=503, message="down", retry_after=None
            ),
        )
        install_http_mocks(monkeypatch, handlers)
        probe = client.probe_model(provider, DEFAULT_MODEL)
        assert probe.healthy is False
        assert probe.status_code == 503
        assert probe.error

    def test_check_model_only_where_supported(self, provider_id, provider, client, monkeypatch):
        if not cap(provider_id, "check_model"):
            pytest.skip("provider has no check_model()")
        install_http_mocks(
            monkeypatch,
            build_handlers(provider_id, chat_factory=chat_ok(provider_id)),
        )
        assert client.check_model(provider, DEFAULT_MODEL) is True


# ---------------------------------------------------------------------------
# Area 13: sync/async parity
# ---------------------------------------------------------------------------


class TestAsyncParity:
    @pytest.mark.asyncio
    async def test_achat_matches_chat(self, provider_id, provider, client, monkeypatch):
        install_http_mocks(
            monkeypatch,
            build_handlers(provider_id, chat_factory=chat_ok(provider_id)),
        )
        assert await client.achat(provider, DEFAULT_MODEL, "hi") == client.chat(
            provider, DEFAULT_MODEL, "hi"
        )

    @pytest.mark.asyncio
    async def test_achat_messages_matches_chat_messages(self, provider_id, provider, client, monkeypatch):
        install_http_mocks(
            monkeypatch,
            build_handlers(provider_id, chat_factory=chat_ok(provider_id)),
        )
        payload = full_payload(stream=False)
        assert await client.achat_messages(provider, payload) == client.chat_messages(
            provider, payload
        )

    @pytest.mark.asyncio
    async def test_achat_stream_matches_chat_stream(self, provider_id, provider, client, monkeypatch):
        install_http_mocks(
            monkeypatch,
            build_handlers(provider_id, chat_factory=chat_ok(provider_id)),
        )
        sync_deltas = list(client.chat_stream(provider, DEFAULT_MODEL, "hi"))
        async_deltas = [d async for d in client.achat_stream(provider, DEFAULT_MODEL, "hi")]
        assert async_deltas == sync_deltas

    @pytest.mark.asyncio
    async def test_achat_stream_messages_matches_chat_stream_messages(self, provider_id, provider, client, monkeypatch):
        install_http_mocks(
            monkeypatch,
            build_handlers(provider_id, chat_factory=chat_ok(provider_id)),
        )
        payload = full_payload(stream=True)
        sync_chunks = list(client.chat_stream_messages(provider, payload))
        async_chunks = [
            c async for c in client.achat_stream_messages(provider, payload)
        ]
        assert async_chunks == sync_chunks

    @pytest.mark.asyncio
    async def test_alist_models_matches_list_models(self, provider_id, provider, client, monkeypatch):
        install_http_mocks(
            monkeypatch,
            build_handlers(provider_id, discovery_factory=discovery_ok(provider_id)),
        )
        assert await client.alist_models(provider) == client.list_models(provider)
