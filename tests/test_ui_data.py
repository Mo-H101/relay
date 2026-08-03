"""
Unit tests for the TUI view-model layer (app/ui/data.py).

Textual-free by design: these run headlessly and exercise ServiceFacade
against fake Relay components.
"""

import json

import pytest

from app.providers.base import ModelProbe
from app.setup.scan import ScanResult
from app.setup.wizard import SetupResult
from app.ui.data import ServiceFacade, ChatCandidate, candidate_glyph
from tests.ui_fakes import (
    FakeProvider,
    FakeRelay,
    FakeHealthModel,
    FakeReport,
    FakeClient,
    FakeStore,
    FakeReloader,
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


# ------------------------------------------------------------------ P2c facade


@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    from app.services import setup_state
    from app.setup import persistence

    monkeypatch.setattr(setup_state, "state_dir", tmp_path)
    monkeypatch.setattr(persistence, "state_dir", tmp_path)
    return tmp_path


def test_models_merge_availability_snapshot(isolated_state):
    from app.setup import persistence

    relay = FakeRelay()
    relay.provider_manager.register(
        FakeProvider("NVIDIA", api_key="k", models=["m1", "m2"])
    )
    persistence.write_snapshot(
        "nvidia",
        [
            ScanResult("m1", "available", latency_ms=10, status_code=200),
            ScanResult("m2", "unavailable", status_code=403, error="denied"),
        ],
    )

    models = _facade(relay).models()

    assert {(m.name, m.status) for m in models} == {
        ("m1", "healthy"),
        ("m2", "unavailable"),
    }


def test_models_health_store_beats_snapshot(isolated_state):
    from app.setup import persistence

    relay = FakeRelay()
    relay.provider_manager.register(FakeProvider("NVIDIA", api_key="k", models=["m1"]))
    relay.health_store.set(
        FakeReport("NVIDIA", [FakeHealthModel("m1", "degraded")])
    )
    persistence.write_snapshot(
        "nvidia", [ScanResult("m1", "available", latency_ms=10, status_code=200)]
    )

    models = _facade(relay).models()

    assert models[0].status == "degraded"


def test_provider_catalog_merges_registry_and_runtime():
    relay = FakeRelay()
    relay.provider_manager.register(
        FakeProvider("NVIDIA", api_key="sec", enabled=True, models=["a"])
    )

    entries = _facade(relay).provider_catalog()

    assert len(entries) == 6
    by_id = {entry.id: entry for entry in entries}
    nvidia = by_id["nvidia"]
    assert nvidia.configured
    assert nvidia.has_api_key
    assert nvidia.enabled
    assert nvidia.model_count == 1
    assert nvidia.display_name == "NVIDIA NIM"
    assert by_id["ollama"].configured is False
    assert by_id["ollama"].requires_api_key is False
    assert "sec" not in repr(entries)


def test_provider_defn_id_mapping():
    facade = _facade(FakeRelay())

    assert facade.provider_defn_id("NVIDIA") == "nvidia"
    assert facade.provider_defn_id("ghost") is None


def test_model_priority_uses_runtime_priority_models():
    relay = make_relay(
        [
            FakeProvider(
                "p1",
                api_key="k",
                models=["a", "b", "c"],
                priority_models=["c", "a"],
            )
        ]
    )

    assert _facade(relay).model_priority("p1") == ["c", "a"]


def test_model_priority_falls_back_to_available_models():
    relay = FakeRelay()
    provider = FakeProvider("p1", api_key="k", models=["a", "b"])
    relay.provider_manager.register(provider)
    relay.health_store.set(
        FakeReport(
            "p1",
            [
                FakeHealthModel("a", "healthy"),
                FakeHealthModel("b", "unavailable"),
            ],
        )
    )

    assert _facade(relay).model_priority("p1") == ["a"]


def test_set_provider_enabled_persists_and_reloads():
    store = FakeStore()
    reloader = FakeReloader()
    relay = make_relay([FakeProvider("NVIDIA", api_key="k", models=["m1"])])
    facade = ServiceFacade(relay_instance=relay, store=store, reloader=reloader)

    report = facade.set_provider_enabled("nvidia", False)

    assert store.writes == [("nvidia", {"enabled": False})]
    assert reloader.calls == [relay]
    assert report["reloaded"] is True


def test_set_provider_enabled_unknown_id():
    facade = ServiceFacade(
        relay_instance=FakeRelay(), store=FakeStore(), reloader=FakeReloader()
    )

    report = facade.set_provider_enabled("ghost", True)

    assert report["ok"] is False
    assert "Unknown provider" in report["error"]


def test_set_model_priority_persists_and_reloads():
    store = FakeStore()
    reloader = FakeReloader()
    relay = make_relay([FakeProvider("NVIDIA", api_key="k", models=["m1", "m2"])])
    facade = ServiceFacade(relay_instance=relay, store=store, reloader=reloader)

    report = facade.set_model_priority("nvidia", ["m2", "m1"])

    assert store.writes == [("nvidia", {"priority_models": ["m2", "m1"]})]
    assert reloader.calls == [relay]
    assert report["reloaded"] is True


def test_rescan_models_writes_snapshot(isolated_state):
    from app.setup import persistence

    relay = make_relay([FakeProvider("NVIDIA", api_key="k", models=["m1", "m2"])])
    relay.chat_service.registry.register(
        "NVIDIA", FakeClient(probe_result=ModelProbe(True, 12))
    )

    report = _facade(relay).rescan_models("nvidia")

    assert report["ok"] is True
    assert report["models"] == 2
    assert report["available"] == 2

    snapshot = json.loads(
        (isolated_state / "availability.json").read_text(encoding="utf-8")
    )
    assert len(snapshot["providers"]["nvidia"]["models"]) == 2


def test_rescan_not_configured():
    report = _facade(make_relay([])).rescan_models("openai")

    assert report["ok"] is False
    assert "not configured" in report["error"]


def test_run_setup_forwards_args(monkeypatch):
    captured = {}

    def fake_run_setup(ui, *, menu=None, store=None):
        captured["ui"] = ui
        captured["menu"] = menu
        captured["store"] = store
        return SetupResult(
            completed=True,
            usable=True,
            configured=["nvidia"],
            state="configured",
        )

    monkeypatch.setattr("app.setup.wizard.run_setup", fake_run_setup)
    store = FakeStore()
    facade = ServiceFacade(relay_instance=FakeRelay(), store=store)

    result = facade.run_setup("ui-obj", menu=["m"], store="my-store")

    assert captured == {"ui": "ui-obj", "menu": ["m"], "store": "my-store"}
    assert result.configured == ["nvidia"]


def test_configure_provider_routes_to_wizard(monkeypatch):
    captured = {}

    def fake_configure_provider(ui, defn, store):
        captured["defn"] = defn
        captured["store"] = store
        return True

    monkeypatch.setattr("app.setup.wizard._configure_provider", fake_configure_provider)
    store = FakeStore()
    facade = ServiceFacade(relay_instance=FakeRelay(), store=store)

    assert facade.configure_provider("ui-obj", "openai") is True
    assert captured["defn"].id == "openai"
    assert captured["store"] is store
    assert facade.configure_provider("ui-obj", "ghost") is False
