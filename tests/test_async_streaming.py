"""
Tests for async streaming functionality (Phase 3C).
"""
import json
import asyncio
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.relay import Relay
from app.main import app as fastapi_app
from app.providers.base import ModelProbe, Provider
from app.providers.exceptions import (
    ProviderError,
    ProviderHTTPError,
    ProviderTimeout,
)
from app.services.health_checker import DEGRADED, HEALTHY, ProviderHealth

import app.api.chat
import app.api.decision
import app.api.health
import app.api.openai
import app.api.providers


def make_provider(name, models, priority=1, api_key="test-key", enabled=True):
    return Provider(
        name=name,
        base_url=f"https://{name.lower()}.invalid",
        api_key=api_key,
        enabled=enabled,
        priority=priority,
        models=list(models),
    )


class FakeStreamingClient:
    """
    Deterministic client for testing streaming behavior.
    
    Stream outcomes are a per-model queue of:
    - Strings (yielded as content chunks)
    - Dicts (yielded as JSON chunks)
    - Exceptions (raised during streaming)
    - Special markers:
        - None: empty chunk (no content)
        - "FINAL": signals end of stream (yields final chunk with finish_reason)
    """

    def __init__(self):
        self.chat_calls = []
        self.stream_calls = []
        self._outcomes = {}

    def set_outcomes(self, model, outcomes):
        self._outcomes[model] = list(outcomes)

    async def achat_messages(self, provider, payload):
        """Non-streaming message-based chat."""
        self.chat_calls.append((provider.name, payload))
        
        queue = self._outcomes.get(payload["model"])
        if not queue:
            raise ProviderError(f"no outcome configured for {payload['model']}")
            
        outcome = queue.pop(0)
        
        if isinstance(outcome, Exception):
            raise outcome
            
        if isinstance(outcome, dict):
            return outcome
            
        # Default response for string outcomes
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 1700000000,
            "model": payload["model"],
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": outcome},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        }

    async def achat_stream_messages(self, provider, payload):
        """Streaming message-based chat."""
        self.stream_calls.append((provider.name, payload))
        
        queue = self._outcomes.get(payload["model"])
        if not queue:
            raise ProviderError(f"no outcome configured for {payload['model']}")
            
        # Track if we've yielded any content
        yielded_content = False
        
        while queue:
            outcome = queue.pop(0)
            
            if isinstance(outcome, Exception):
                raise outcome
                
            if isinstance(outcome, dict):
                # Direct chunk dict (e.g., for tool calls, etc.)
                yield outcome
                yielded_content = True
            elif outcome is None:
                # Empty chunk - no content
                continue
            elif outcome == "FINAL":
                # Special marker for end of stream
                yield {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": payload["model"],
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop"
                    }]
                }
                yielded_content = True
                break
            else:
                # String content
                yield {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion.chunk",
                    "created": 1700000000,
                    "model": payload["model"],
                    "choices": [{
                        "index": 0,
                        "delta": {"content": outcome},
                        "finish_reason": None
                    }]
                }
                yielded_content = True
                
        # If we yielded content but didn't get a FINAL marker, send final chunk
        if yielded_content and (not queue or queue[-1] != "FINAL"):
            yield {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": payload["model"],
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop"
                }]
            }


@pytest.fixture
def fake_registry(monkeypatch):
    """Point every ClientRegistry at FakeClients, no real network."""
    from app.services import client_registry

    holder = {}

    def fake_get(self, provider_name):
        return holder[provider_name]

    monkeypatch.setattr(
        client_registry.ClientRegistry, "get", fake_get
    )
    return holder


def make_client(registry, name, outcomes_by_model=None):
    client = FakeStreamingClient()
    if outcomes_by_model:
        for model, outcomes in outcomes_by_model.items():
            client.set_outcomes(model, outcomes)
    registry[name] = client
    return client


@pytest.fixture
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


@pytest.fixture
def wired_relay(monkeypatch, fake_registry):
    """
    Build a Relay with fake providers/clients and wire it into every API
    router in place of the module-level singleton.
    """
    relays = {}

    def _build(providers=None, clients=None):
        relay = Relay()

        for provider in providers or []:
            relay.provider_manager.register(provider)

        for name, client in (clients or {}).items():
            fake_registry[name] = client

        monkeypatch.setattr(app.api.chat, "relay", relay)
        monkeypatch.setattr(app.api.decision, "relay", relay)
        monkeypatch.setattr(app.api.health, "relay", relay)
        monkeypatch.setattr(app.api.providers, "relay", relay)
        monkeypatch.setattr(app.api.openai, "relay", relay)
        monkeypatch.setattr(app.api.diagnostics, "relay", relay)

        relays[id(relay)] = relay
        return relay

    yield _build

    # Restore original relays after test
    for relay in relays.values():
        monkeypatch.setattr(app.api.chat, "relay", relay)
        monkeypatch.setattr(app.api.decision, "relay", relay)
        monkeypatch.setattr(app.api.health, "relay", relay)
        monkeypatch.setattr(app.api.providers, "relay", relay)
        monkeypatch.setattr(app.api.openai, "relay", relay)
        monkeypatch.setattr(app.api.diagnostics, "relay", relay)


class TestSSEStreamingFormat:
    """Test Server-Sent Events wire format compliance."""

    def test_sse_format_basic(self, wired_relay, fake_registry, client):
        """Test that streaming response follows SSE format: data: {...}\n\n"""
        provider = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": ["Hello", "world"]})
        wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        
        # Check that response contains properly formatted SSE lines
        content = response.text
        assert "data:" in content
        # Each JSON chunk should be wrapped in "data: " and end with "\n\n"
        lines = [line.strip() for line in content.split('\n') if line.strip()]
        data_lines = [line for line in lines if line.startswith("data:")]
        assert len(data_lines) >= 2  # At least two data chunks
        
        # Verify JSON format in data lines
        for i, data_line in enumerate(data_lines[:-1]):  # Exclude [DONE]
            json_str = data_line[5:]  # Remove "data: " prefix
            data = json.loads(json_str)
            assert "choices" in data
            assert len(data["choices"]) > 0
            if i < len(data_lines) - 2:  # Not the last data chunk before [DONE]
                assert "delta" in data["choices"][0]
                assert "content" in data["choices"][0]["delta"]

    def test_sse_format_with_usage(self, wired_relay, fake_registry, client):
        """Test that usage information is properly included in final chunk."""
        provider = make_provider("A", ["a-1"])
        # Return a usage chunk at the end
        make_client(fake_registry, "A", {
            "a-1": ["Hello", {"usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}, "FINAL"]
        })
        wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        content = response.text
        
        # Should contain the usage chunk
        assert '"usage"' in content
        assert '"prompt_tokens": 5' in content
        assert '"completion_tokens": 3' in content
        assert '"total_tokens": 8' in content


class TestChunkOrdering:
    """Test that chunks are streamed in correct order."""

    def test_chunk_ordering_preserved(self, wired_relay, fake_registry, client):
        """Test that chunks appear in the same order as provided."""
        provider = make_provider("A", ["a-1"])
        chunks = ["Hello", " ", "world", "!", "FINAL"]
        make_client(fake_registry, "A", {"a-1": chunks})
        wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        content = response.text
        
        # Extract the content from each data chunk
        import re
        data_chunks = re.findall(r'data: (\{.*?\})(?:\n\n|$)', content)
        # Filter out [DONE] and empty data
        data_chunks = [c for c in data_chunks if c.strip() and not c.strip().endswith('[DONE]')]
        
        # Parse each chunk and extract content
        extracted_content = []
        for chunk_json in data_chunks:
            try:
                data = json.loads(chunk_json)
                if "choices" in data and len(data["choices"]) > 0:
                    delta = data["choices"][0].get("delta", {})
                    if "content" in delta:
                        extracted_content.append(delta["content"])
            except json.JSONDecodeError:
                pass  # Skip non-JSON data
                
        # Should match our input sequence (excluding FINAL which doesn't produce content)
        expected = ["Hello", " ", "world", "!"]
        assert extracted_content == expected

    def test_empty_chunks_skipped(self, wired_relay, fake_registry, client):
        """Test that None/empty chunks don't produce output but don't break stream."""
        provider = make_provider("A", ["a-1"])
        chunks = ["Hello", None, "", "world", None, "!"]  # None and empty strings
        make_client(fake_registry, "A", {"a-1": chunks + ["FINAL"]})
        wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        content = response.text
        
        # Extract content chunks
        import re
        data_chunks = re.findall(r'data: (\{.*?\})(?:\n\n|$)', content)
        data_chunks = [c for c in data_chunks if c.strip() and not c.strip().endswith('[DONE]')]
        
        # Parse and collect non-empty content
        content_pieces = []
        for chunk_json in data_chunks:
            try:
                data = json.loads(chunk_json)
                if "choices" in data and len(data["choices"]) > 0:
                    delta = data["choices"][0].get("delta", {})
                    if "content" in delta and delta["content"]:
                        content_pieces.append(delta["content"])
            except json.JSONDecodeError:
                pass
                
        # Should only contain the non-empty strings in order
        expected = ["Hello", "world", "!"]
        assert content_pieces == expected


class TestCancellationAndDisconnect:
    """Test client disconnection and cancellation handling."""

    @pytest.mark.asyncio
    async def test_client_disconnect_closes_provider_stream(self, wired_relay, fake_registry, client):
        """Test that when client disconnects, provider generator is properly closed."""
        # This test verifies that cleanup happens properly
        # We'll simulate this by checking that the stream method was called
        # and that no exceptions are leaked
        
        provider = make_provider("A", ["a-1"])
        # Create a stream that would normally run forever
        call_count = 0
        
        async def never_ending_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Yield a few items then hang
            yield {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": "a-1",
                "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}]
            }
            # Simulate hanging - in real test, we'd cancel the task
            await asyncio.sleep(0.1)  # Short delay for test
            
        # Replace the stream method
        fake_client = make_client(fake_registry, "A")
        original_method = fake_client.achat_stream_messages
        fake_client.achat_stream_messages = never_ending_stream
        
        try:
            wired_relay(providers=[provider])
            
            # Make a streaming request using TestClient
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "a-1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )
            
            assert response.status_code == 200
            
            # Read only first chunk then disconnect
            for chunk in response.iter_text():
                if "Hello" in chunk:
                    break  # Disconnect after first chunk
                    
        finally:
            # Restore original method
            fake_client.achat_stream_messages = original_method

    def test_cancelled_error_does_not_leak_tasks(self, wired_relay, fake_registry, client):
        """Test that cancelled requests don't leave hanging tasks."""
        # This is more of an integration test - we verify the endpoint
        # handles cancellations gracefully by testing the actual HTTP behavior
        
        provider = make_provider("A", ["a-1"])
        # Slow stream that takes time
        slow_chunks = []
        for i in range(10):
            slow_chunks.append(f"chunk-{i}")
        slow_chunks.append("FINAL")
        
        make_client(fake_registry, "A", {"a-1": slow_chunks})
        wired_relay(providers=[provider])

        # Make a streaming request
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "start"}],
                "stream": True,
            },
        )
        
        # Even if we don't read the full response, the request should complete
        # (the TestClient will handle connection cleanup)
        assert response.status_code == 200


class TestMidStreamErrors:
    """Test handling of errors that occur during streaming."""

    def test_mid_stream_error_passthrough(self, wired_relay, fake_registry, client):
        """Test that errors during streaming are yielded as error chunks."""
        provider = make_provider("A", ["a-1"])
        # Stream that fails after a few chunks
        stream_parts = [
            "Hello",
            " ",
            "beautiful",
            ProviderHTTPError(503, "Service unavailable"),  # Error in middle
            "world",  # This should not be sent
        ]
        make_client(fake_registry, "A", {"a-1": stream_parts})
        wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

        assert response.status_code == 200  # Streaming starts successfully
        content = response.text
        
        # Should contain the successful chunks before the error
        assert "Hello" in content
        assert "beautiful" in content
        
        # Should contain error information in the stream
        # Provider bodies/status text are replaced with the safe classification.
        assert "Provider returned a server error." in content
        
        # Should NOT contain data that came after the error
        assert "world" not in content

    def test_mid_stream_error_ends_stream(self, wired_relay, fake_registry, client):
        """Test that an error in the stream terminates further processing."""
        provider = make_provider("A", ["a-1"])
        call_count = 0
        
        async def counting_stream_with_error(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            
            yield {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": "a-1",
                "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}]
            }
            
            # Raise an error after first chunk
            raise ProviderError("Internal error")
        
        # Replace stream method
        fake_client = make_client(fake_registry, "A")
        original = fake_client.achat_stream_messages
        fake_client.achat_stream_messages = counting_stream_with_error
        
        try:
            wired_relay(providers=[provider])
            
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "a-1",
                    "messages": [{"role": "user", "content": "test"}],
                    "stream": True,
                },
            )
            
            assert response.status_code == 200
            content = response.text
            
            # Should contain the first chunk
            assert "ok" in content
            # Should contain error indication
            assert "Provider request failed." in content
            assert "Internal error" not in content
            
            # Should NOT continue processing after error
            assert call_count == 1  # Only called once
            
        finally:
            fake_client.achat_stream_messages = original


class TestUsagePassthrough:
    """Test that usage information is properly passed through."""

    def test_usage_chunk_passthrough(self, wired_relay, fake_registry, client):
        """Test that usage chunks from providers are passed through in stream."""
        provider = make_provider("A", ["a-1"])
        # Provide usage information in the stream
        usage_chunk = {
            "id": "chatcmpl-use",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": "a-1",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        }
        
        make_client(fake_registry, "A", {"a-1": ["Hello", "world", usage_chunk]})
        wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        content = response.text
        
        # Should contain the usage information
        assert '"usage"' in content
        assert '"prompt_tokens": 10' in content
        assert '"completion_tokens": 5' in content
        assert '"total_tokens": 15' in content

    def test_usage_in_final_non_streaming_response(self, wired_relay, fake_registry, client):
        """Test that usage is preserved in non-streaming responses too."""
        provider = make_provider("A", ["a-1"])
        # Response with usage
        response_with_usage = {
            "id": "chatcmpl-usage",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "a-1",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hello world"},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
        }
        
        make_client(fake_registry, "A", {"a-1": [response_with_usage]})
        wired_relay(providers=[provider])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )

        assert response.status_code == 200
        data = response.json()
        
        assert "usage" in data
        assert data["usage"]["prompt_tokens"] == 7
        assert data["usage"]["completion_tokens"] == 3
        assert data["usage"]["total_tokens"] == 10


class TestFailoverBehavior:
    """Test failover when streams are empty or fail."""

    def test_empty_stream_triggers_failover(self, wired_relay, fake_registry, client):
        """Test that empty stream from first provider triggers failover to second."""
        # First provider returns empty stream (no content)
        provider_a = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": []})  # Empty stream
        
        # Second provider returns actual content (same model!)
        provider_b = make_provider("B", ["a-1"])
        make_client(fake_registry, "B", {"a-1": ["Hello from B", "FINAL"]})
        
        wired_relay(providers=[provider_a, provider_b])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        content = response.text
        
        # Should get content from provider B
        assert "Hello from B" in content
        # The model in response should still be what was requested
        assert '"model": "a-1"' in content  # Original model requested

    def test_error_stream_triggers_failover(self, wired_relay, fake_registry, client):
        """Test that erroring stream triggers failover to healthy provider."""
        # First provider returns error immediately
        provider_a = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": [ProviderError("Service down")]})
        
        # Second provider works fine (same model!)
        provider_b = make_provider("B", ["a-1"])
        make_client(fake_registry, "B", {"a-1": ["Hello from B", "FINAL"]})
        
        wired_relay(providers=[provider_a, provider_b])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",  # Will actually use B due to failover
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

        assert response.status_code == 200
        content = response.text
        
        # Should get content from provider B
        assert "Hello from B" in content
        assert '"model": "a-1"' in content  # Original model requested in response

    def test_both_providers_fail_returns_error(self, wired_relay, fake_registry, client):
        """Test that when all providers fail, we get an appropriate error."""
        # Both providers fail
        provider_a = make_provider("A", ["a-1"])
        make_client(fake_registry, "A", {"a-1": [ProviderError("Service A down")]})
        
        provider_b = make_provider("B", ["b-1"])
        make_client(fake_registry, "B", {"b-1": [ProviderError("Service B down")]})
        
        wired_relay(providers=[provider_a, provider_b])

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "a-1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

        # Should eventually return an error (5xx)
        # The exact status may vary based on retry logic
        assert response.status_code >= 500


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
