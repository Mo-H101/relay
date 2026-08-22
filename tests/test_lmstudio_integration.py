"""
Integration tests for the LM Studio provider against an in-process fake
LM Studio server speaking the OpenAI-compatible wire protocol.

No LM Studio installation is required: the fake server runs on an
ephemeral localhost port and exercises the real clients, Relay, health
checks, telemetry, scoring, explanations, and failover over real HTTP.

A separate, env-gated file (test_lmstudio_real.py) validates against an
actual LM Studio instance.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, settings
from app.core.relay import Relay
from app.main import app as fastapi_app
from app.providers.base import Provider
from app.providers.lmstudio import create_provider
from app.services.capabilities import ModelCapability, detect_capability

import app.api.chat
import app.api.decision
import app.api.health
import app.api.providers

DEFAULT_MODELS = [
    "qwen2.5-7b-instruct",
    "TheBloke/Mistral-7B-Instruct-v0.2-GGUF",
    "llama-3.2-3b-instruct",
]


class FakeLMStudioServer:
    """
    Minimal OpenAI-compatible server for integration testing.

    Serves GET /v1/models and POST /v1/chat/completions (also at the
    root paths so base URLs with or without the /v1 prefix both work).
    Per-model behaviors can simulate failures, delays, and unloaded
    models. Every request is recorded with its headers and body.
    """

    def __init__(self, models=None):
        self.state = {
            "models": list(models) if models is not None else list(DEFAULT_MODELS),
            "chat_status": 200,
            "chat_text": "",
            "chat_delay": 0.0,
            "behaviors": {},
            "requests": [],
        }
        self._server = None
        self._thread = None
        self.base_url = None
        self.root_url = None

    def start(self):
        handler = self._make_handler()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()
        port = self._server.server_address[1]
        self.base_url = f"http://127.0.0.1:{port}/v1"
        self.root_url = f"http://127.0.0.1:{port}"

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def set_behavior(self, model, status=200, text="", delay=0.0):
        self.state["behaviors"][model] = {
            "status": status,
            "text": text,
            "delay": delay,
        }

    def set_default(self, status=200, text="", delay=0.0):
        self.state["chat_status"] = status
        self.state["chat_text"] = text
        self.state["chat_delay"] = delay

    def recorded(self, method=None, path=None):
        return [
            request
            for request in self.state["requests"]
            if (method is None or request["method"] == method)
            and (path is None or request["path"] == path)
        ]

    def _make_handler(self):
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                state["requests"].append(
                    {
                        "method": "GET",
                        "path": self.path,
                        "headers": dict(self.headers),
                    }
                )

                path = self.path.rstrip("/") or "/"

                if path in ("/models", "/v1/models"):
                    self._respond(
                        200,
                        {"data": [{"id": model} for model in state["models"]]},
                    )
                else:
                    self._respond(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length)
                body = {}

                if raw:
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        body = {}

                state["requests"].append(
                    {
                        "method": "POST",
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": body,
                    }
                )

                path = self.path.rstrip("/") or "/"

                if path not in ("/chat/completions", "/v1/chat/completions"):
                    self._respond(404, {"error": "not found"})
                    return

                model = body.get("model")
                behavior = state["behaviors"].get(model)

                if behavior is None:
                    behavior = {
                        "status": state["chat_status"],
                        "text": state["chat_text"],
                        "delay": state["chat_delay"],
                    }

                if behavior.get("delay"):
                    time.sleep(behavior["delay"])

                if behavior["status"] == 200:
                    content = behavior.get("text") or f"{model} reply"
                    self._respond(
                        200,
                        {"choices": [{"message": {"content": content}}]},
                    )
                else:
                    text = behavior.get("text") or (
                        f"HTTP error {behavior['status']}"
                    )
                    self._respond(
                        behavior["status"],
                        {"error": {"message": text}},
                    )

            def _respond(self, code, obj):
                data = json.dumps(obj).encode("utf-8")

                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        return Handler


@pytest.fixture
def fake_lmstudio():
    server = FakeLMStudioServer()
    server.start()
    yield server
    server.stop()


def _patch_lm_settings(
    monkeypatch,
    server,
    api_key="",
    priority=10,
    model_priority=None,
    base_url=None,
):
    monkeypatch.setattr(settings, "lmstudio_enabled", True)
    monkeypatch.setattr(settings, "lmstudio_base_url", base_url or server.base_url)
    monkeypatch.setattr(settings, "lmstudio_api_key", api_key)
    monkeypatch.setattr(settings, "lmstudio_priority", priority)
    monkeypatch.setattr(settings, "lmstudio_model_priority", list(model_priority or []))
    monkeypatch.setattr(settings, "telemetry_enabled", True)
    monkeypatch.setattr(settings, "health_feedback_enabled", False)


def build_relay(
    monkeypatch,
    server,
    api_key="",
    priority=10,
    model_priority=None,
    base_url=None,
    extra_providers=None,
):
    _patch_lm_settings(
        monkeypatch,
        server,
        api_key=api_key,
        priority=priority,
        model_priority=model_priority,
        base_url=base_url,
    )

    relay = Relay()

    for provider in extra_providers or []:
        relay.provider_manager.register(provider)

    monkeypatch.setattr(app.api.chat, "relay", relay)
    monkeypatch.setattr(app.api.decision, "relay", relay)
    monkeypatch.setattr(app.api.health, "relay", relay)
    monkeypatch.setattr(app.api.providers, "relay", relay)

    return relay


@pytest.fixture
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


class TestDiscovery:
    def test_keyless_discovery_sends_no_auth_header(
        self, fake_lmstudio, monkeypatch
    ):
        _patch_lm_settings(monkeypatch, fake_lmstudio, api_key="")

        provider = create_provider()

        assert provider.name == "LM Studio"
        assert provider.models == DEFAULT_MODELS

        get_requests = fake_lmstudio.recorded(method="GET")
        assert get_requests
        assert "Authorization" not in get_requests[0]["headers"]

    def test_optional_key_discovery_sends_auth_header(
        self, fake_lmstudio, monkeypatch
    ):
        _patch_lm_settings(monkeypatch, fake_lmstudio, api_key="lm-key")

        create_provider()

        get_requests = fake_lmstudio.recorded(method="GET")
        assert get_requests[0]["headers"]["Authorization"] == "Bearer lm-key"

    def test_local_model_name_formats_are_discovered(
        self, fake_lmstudio, monkeypatch
    ):
        _patch_lm_settings(monkeypatch, fake_lmstudio)

        provider = create_provider()

        assert "qwen2.5-7b-instruct" in provider.models
        assert "TheBloke/Mistral-7B-Instruct-v0.2-GGUF" in provider.models
        assert "llama-3.2-3b-instruct" in provider.models

    def test_local_instruct_models_classify_as_chat(
        self, fake_lmstudio, monkeypatch
    ):
        for model in DEFAULT_MODELS:
            assert detect_capability(model) == ModelCapability.CHAT

        assert detect_capability("qwen2.5-vl-7b-instruct") == (
            ModelCapability.VISION
        )

    def test_trailing_slash_base_url_is_normalized(
        self, fake_lmstudio, monkeypatch
    ):
        monkeypatch.setenv("LMSTUDIO_BASE_URL", f"{fake_lmstudio.root_url}/v1/")
        cfg = Settings()

        assert cfg.lmstudio_base_url == fake_lmstudio.base_url

        relay = build_relay(
            monkeypatch,
            fake_lmstudio,
            base_url=cfg.lmstudio_base_url,
        )

        assert relay.provider_manager.get("LM Studio").models == DEFAULT_MODELS

    def test_base_url_without_v1_prefix_works(
        self, fake_lmstudio, monkeypatch
    ):
        _patch_lm_settings(
            monkeypatch,
            fake_lmstudio,
            base_url=fake_lmstudio.root_url,
        )

        provider = create_provider()

        assert provider.models == DEFAULT_MODELS
        assert fake_lmstudio.recorded(method="GET")[0]["path"] == "/models"


class TestApiEndpoints:
    def test_provider_endpoint_selects_lmstudio(self, fake_lmstudio, monkeypatch, client):
        build_relay(monkeypatch, fake_lmstudio)

        response = client.get("/provider")

        assert response.status_code == 200
        payload = response.json()
        assert payload["name"] == "LM Studio"
        assert payload["models"] == DEFAULT_MODELS

    def test_providers_endpoint_lists_lmstudio(self, fake_lmstudio, monkeypatch, client):
        build_relay(monkeypatch, fake_lmstudio)

        response = client.get("/providers")

        names = [p["name"] for p in response.json()["providers"]]
        assert "LM Studio" in names

    def test_health_endpoint_reports_lmstudio_healthy(
        self, fake_lmstudio, monkeypatch, client
    ):
        relay = build_relay(monkeypatch, fake_lmstudio)

        response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

        lm = next(
            p for p in relay.health(deep=False)["providers"]
            if p["name"] == "LM Studio"
        )
        assert lm["status"] == "healthy"
        assert set(lm["healthy_models"]) == set(DEFAULT_MODELS)

    def test_chat_success_via_api(self, fake_lmstudio, monkeypatch, client):
        build_relay(monkeypatch, fake_lmstudio)

        response = client.post("/chat", json={"message": "hello"})

        assert response.status_code == 200
        payload = response.json()
        assert payload["provider"] == "LM Studio"
        assert payload["model"] == DEFAULT_MODELS[0]
        assert payload["response"] == f"{DEFAULT_MODELS[0]} reply"

    def test_decision_explain_via_api(self, fake_lmstudio, monkeypatch, client):
        monkeypatch.setattr(settings, "decision_explanations_enabled", True)
        build_relay(monkeypatch, fake_lmstudio)

        response = client.get("/decision/explain")

        assert response.status_code == 200
        payload = response.json()
        assert payload["selected"]["provider"] == "LM Studio"
        assert payload["candidates"]
        assert any(
            "Health band" in reason
            for reason in payload["candidates"][0]["reasons"]
        )


class TestFailoverAndErrors:
    def test_failover_when_lmstudio_fails(self, fake_lmstudio, monkeypatch, client):
        fake_lmstudio.state["models"] = ["bad-model"]
        fake_lmstudio.set_behavior("bad-model", status=500, text="boom")

        backup = Provider(
            name="OpenAI",
            base_url=fake_lmstudio.base_url,
            api_key="sk",
            models=["backup-model"],
            priority=1,
        )
        build_relay(
            monkeypatch,
            fake_lmstudio,
            extra_providers=[backup],
        )

        response = client.post("/chat", json={"message": "hello"})

        assert response.status_code == 200
        payload = response.json()
        assert payload["provider"] == "OpenAI"
        assert payload["model"] == "backup-model"
        assert payload["response"] == "backup-model reply"

    def test_unloaded_model_error_is_handled(self, fake_lmstudio, monkeypatch, client):
        fake_lmstudio.state["models"] = ["phantom-model"]
        fake_lmstudio.set_behavior(
            "phantom-model",
            status=400,
            text="Model not loaded: phantom-model",
        )
        build_relay(monkeypatch, fake_lmstudio)

        response = client.post("/chat", json={"message": "hello"})

        assert response.status_code == 502
        assert response.json()["detail"] == "Provider rejected the request."

    def test_unavailable_model_health(self, fake_lmstudio, monkeypatch):
        fake_lmstudio.state["models"] = ["broken-model"]
        fake_lmstudio.set_behavior("broken-model", status=404, text="not found")
        relay = build_relay(monkeypatch, fake_lmstudio)

        report = relay.health()["providers"][0]

        assert report["name"] == "LM Studio"
        assert report["status"] == "unavailable"
        assert "broken-model" in report["unavailable_models"]

    def test_empty_model_list_behavior(self, fake_lmstudio, monkeypatch, client):
        fake_lmstudio.state["models"] = []
        build_relay(monkeypatch, fake_lmstudio)

        providers = client.get("/providers").json()["providers"]
        lm = next(p for p in providers if p["name"] == "LM Studio")
        assert lm["models"] == []

        response = client.post("/chat", json={"message": "hello"})

        assert response.status_code == 502
        assert "No candidates to try." in response.json()["detail"]

    def test_chat_timeout_is_classified(self, fake_lmstudio, monkeypatch):
        fake_lmstudio.state["models"] = ["slow-model"]
        fake_lmstudio.set_default(delay=1.0)
        monkeypatch.setattr(settings, "request_timeout", 0.1)
        relay = build_relay(monkeypatch, fake_lmstudio)

        result = relay.chat("hello")

        assert result["success"] is False
        assert all(
            attempt["failure_type"] == "timeout"
            for attempt in result["attempts"]
        )

        stats = relay.telemetry.get("LM Studio", "slow-model")
        assert stats is not None
        assert stats.failure_count == len(result["attempts"])


class TestAuthBehavior:
    def test_keyless_chat_omits_auth_header(self, fake_lmstudio, monkeypatch):
        relay = build_relay(monkeypatch, fake_lmstudio, api_key="")

        relay.chat("hello")

        posts = fake_lmstudio.recorded(method="POST")
        assert "Authorization" not in posts[0]["headers"]

    def test_optional_key_chat_sends_bearer_header(
        self, fake_lmstudio, monkeypatch
    ):
        relay = build_relay(monkeypatch, fake_lmstudio, api_key="lm-key")

        relay.chat("hello")

        posts = fake_lmstudio.recorded(method="POST")
        assert posts[0]["headers"]["Authorization"] == "Bearer lm-key"


class TestTelemetryScoringExplanation:
    def test_telemetry_recorded_after_success(self, fake_lmstudio, monkeypatch):
        relay = build_relay(monkeypatch, fake_lmstudio)

        relay.chat("hello")

        stats = relay.telemetry.get("LM Studio", DEFAULT_MODELS[0])
        assert stats is not None
        assert stats.request_count == 1
        assert stats.success_count == 1
        assert stats.average_latency_ms >= 0

    def test_scoring_interacts_with_telemetry(self, fake_lmstudio, monkeypatch):
        relay = build_relay(monkeypatch, fake_lmstudio)

        relay.chat("hello")

        ranked = relay.candidate_builder.ranked_candidates(
            relay.provider_manager.ranked()
        )

        assert ranked[0].provider == "LM Studio"
        assert ranked[0].breakdown["success"] == pytest.approx(1.0)

    def test_explanation_service_works_with_lmstudio(self, fake_lmstudio, monkeypatch):
        relay = build_relay(monkeypatch, fake_lmstudio)

        relay.chat("hello")

        ranked = relay.candidate_builder.ranked_candidates(
            relay.provider_manager.ranked()
        )
        from app.services.explanation import ExplanationService

        explanation = ExplanationService().explain(ranked, task=None)

        assert explanation["selected"]["provider"] == "LM Studio"
        assert explanation["candidates"][0]["reasons"]
