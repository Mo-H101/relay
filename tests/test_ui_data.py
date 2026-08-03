"""
Unit tests for the TUI view-model layer (app/ui/data.py).

Textual-free by design: these run headlessly and exercise ServiceFacade
against fake Relay components.
"""

from app.ui.data import ServiceFacade, ChatCandidate, candidate_glyph
from tests.ui_fakes import (
    FakeProvider,
    FakeRelay,
    FakeHealthModel,
    FakeReport,
    FakeClient,
    make_relay,
)


def _facade(relay: FakeRelay) -> ServiceFacade:
    return ServiceFacade(relay_instance=relay)


def test_providers_projection():
    relay = make_relay(
        [
            FakeProvider(
                "cloud",
                requires_api_key=True,
                api_key="secret",
                models=["a", "b"],
            ),
            FakeProvider(
                "local",
                enabled=False,
                requires_api_key=False,
                api_key="",
                models=["c"],
            ),
        ]
    )

    providers = _facade(relay).providers()

    assert [p.name for p in providers] == ["cloud", "local"]
    cloud, local = providers
    assert cloud.enabled and cloud.has_api_key and cloud.models == ["a", "b"]
    assert not local.enabled and not local.has_api_key


def test_models_union_with_unknown_status():
    relay = make_relay(
        [FakeProvider("p1", api_key="k", models=["m1", "m2"])]
    )

    models = _facade(relay).models()

    assert [(m.provider, m.name, m.status) for m in models] == [
        ("p1", "m1", "healthy"),
        ("p1", "m2", "healthy"),
    ]


def test_models_missing_health_record_are_unknown():
    relay = FakeRelay()
    relay.provider_manager.register(FakeProvider("p1", api_key="k", models=["m1"]))

    models = _facade(relay).models()

    assert models[0].status == "unknown"


def test_models_dedup_health_first():
    relay = FakeRelay()
    provider = FakeProvider("p1", api_key="k", models=["m1"])
    relay.provider_manager.register(provider)
    relay.health_store.set(FakeReport("p1", [FakeHealthModel("m1", "degraded")]))

    models = _facade(relay).models()

    assert len(models) == 1
    assert models[0].status == "degraded"


def test_dashboard_summary_shape(monkeypatch):
    from app.services.ops_store import ops_store

    relay = make_relay(
        [
            FakeProvider(
                "p1", api_key="k", models=["m1", "m2"], priority=5
            )
        ]
    )
    ops_store.clear()
    ops_store.record_http("GET", "/health", 200, 12.0)
    ops_store.record_chat("/chat", False, "p1", "m1", True, False, 80.0, attempts=1)

    facade = _facade(relay)
    summary = facade.dashboard_summary()

    assert summary.provider_count == 1
    assert summary.enabled_providers == 1
    assert summary.model_count == 2
    assert summary.healthy_models == 2
    assert summary.requests == 2
    assert summary.successes == 2
    assert summary.failures == 0
    assert summary.success_rate == 1.0
    assert summary.chats == 1
    assert summary.chat_attempts == 1
    assert summary.persistence_enabled is False
    assert summary.persistence_error == ""
    assert summary.server.url.startswith("http://")

    ops_store.clear()


def test_dashboard_server_url_loopback(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "relay_host", "0.0.0.0")
    monkeypatch.setattr(settings, "relay_port", 8123)

    summary = _facade(FakeRelay()).dashboard_summary()

    assert summary.server.url == "http://127.0.0.1:8123"
    assert summary.server.port == 8123


def test_server_running_reflects_marker(monkeypatch):
    relay = FakeRelay()
    facade = _facade(relay)

    assert facade.server_running() is False

    relay._embedded_server_running = True
    assert facade.server_running() is True


# --------------------------------------------------------------- chat facade


def test_specific_model_candidates_filters_chat_models():
    relay = FakeRelay()
    provider = FakeProvider("p1", api_key="k", models=["gpt-4", "embedding-1"])
    relay.provider_manager.register(provider)
    relay.health_store.set(
        FakeReport(
            "p1",
            [
                FakeHealthModel("gpt-4", "healthy"),
                FakeHealthModel("embedding-1", "degraded"),
            ],
        )
    )

    candidates = _facade(relay).specific_model_candidates()

    assert candidates == [ChatCandidate(provider="p1", model="gpt-4", status="healthy")]


def test_candidate_missing_health_is_unknown():
    relay = FakeRelay()
    relay.provider_manager.register(FakeProvider("p1", api_key="k", models=["gpt-4"]))

    candidates = _facade(relay).specific_model_candidates()

    assert candidates[0].status == "unknown"


def test_candidate_glyph_mapping():
    assert candidate_glyph("healthy") == "✓"
    assert candidate_glyph("degraded") == "⚠"
    assert candidate_glyph("unavailable") == "✗"
    assert candidate_glyph("unsupported") == "?"
    assert candidate_glyph("anything-else") == "-"


def test_random_chat_uses_choose_provider():
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])

    result = _facade(relay).random_chat("hi")

    assert result["success"] is True
    assert result["provider"] == "p1"
    assert result["model"] == "m1"
    assert result["response"] == "echo: hi"


def test_random_chat_no_provider_returns_error():
    relay = FakeRelay()

    result = _facade(relay).random_chat("hi")

    assert result["success"] is False
    assert "No provider" in result["error"]


def test_random_chat_no_chat_models_returns_error():
    relay = make_relay(
        [FakeProvider("p1", api_key="k", models=["embedding-1"])]
    )

    result = _facade(relay).random_chat("hi")

    assert result["success"] is False
    assert "chat-testable" in result["error"]


def test_specific_chat_targets_provider_and_model():
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1", "m2"])])

    result = _facade(relay).specific_chat("p1", "m2", "hi")

    assert result["success"] is True
    assert result["model"] == "m2"
    assert result["response"] == "echo: hi"


def test_specific_chat_unknown_provider_returns_error():
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])

    result = _facade(relay).specific_chat("ghost", "m1", "hi")

    assert result["success"] is False
    assert "Unknown provider" in result["error"]


def test_start_stream_builds_payload_and_yields_chunks():
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])
    facade = _facade(relay)

    result = facade.start_stream("p1", "m1", "hi", temperature=0.5)

    assert result["success"] is True
    assert result["provider"] == "p1"
    assert result["model"] == "m1"
    assert [c["choices"][0]["delta"]["content"] for c in result["stream_gen"]] == [
        "hello",
        " world",
    ]

    candidates, payload, kwargs = relay.chat_service.stream_calls[0]
    assert payload["model"] == "m1"
    assert payload["stream"] is True
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["temperature"] == 0.5


def test_start_stream_unknown_provider_returns_error():
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])

    result = _facade(relay).start_stream("ghost", "m1", "hi")

    assert result["success"] is False
    assert result["stream_gen"] is None
    assert "Unknown provider" in result["error"]


def test_probe_model_unknown_provider_returns_none():
    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])

    assert _facade(relay).probe_model("ghost", "m1") is None


def test_probe_model_runs_scan_against_registered_client():
    from app.providers.base import ModelProbe

    relay = make_relay([FakeProvider("p1", api_key="k", models=["m1"])])
    relay.chat_service.registry.register(
        "p1", FakeClient(probe_result=ModelProbe(healthy=True, latency_ms=12))
    )

    result = _facade(relay).probe_model("p1", "m1")

    assert result.status == "available"
    assert result.latency_ms == 12
