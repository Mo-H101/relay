import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.relay import Relay
from app.main import app as fastapi_app
from app.providers.exceptions import ProviderHTTPError
from app.providers.registry import PROVIDER_REGISTRY
from app.services.diagnostics import DiagnosticsService
from app.services.provider_manager import ProviderManager
from app.ui.data import ServiceFacade

import app.api.health
import app.core.relay as relay_module


RAW_ERROR = (
    "provider body must not escape "
    "RELAY_AUDIT_SECRET_REGISTRATION_123456"
)


def test_provider_manager_registration_status_is_safe_and_structured():
    manager = ProviderManager()
    manager.record_registration(
        "optional",
        provider_name="Optional",
        status="discovery_failed",
        stage="model_discovery",
        enabled=True,
        error_kind="server_error",
    )

    status = manager.registration_status()
    assert status == [
        {
            "id": "optional",
            "name": "Optional",
            "status": "discovery_failed",
            "stage": "model_discovery",
            "enabled": True,
            "error_kind": "server_error",
        }
    ]
    assert RAW_ERROR not in json.dumps(status)

    manager.record_registration(
        "unsafe",
        provider_name="Optional",
        status="initialization_failed",
        stage="runtime",
        enabled=True,
        error_kind=RAW_ERROR,
    )
    unsafe = manager.registration_status_for("unsafe")
    assert unsafe["status"] == "initialization_failed"
    assert unsafe["error_kind"] == "unknown"
    assert RAW_ERROR not in json.dumps(unsafe)


def _relay_with_optional_discovery_failure(monkeypatch):
    defn = PROVIDER_REGISTRY["openai"]
    monkeypatch.setattr(
        relay_module,
        "PROVIDER_REGISTRY",
        {"openai": defn},
    )
    monkeypatch.setattr(relay_module, "RUNTIME_READY", {"openai"})
    monkeypatch.setattr(settings, "openai_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", "synthetic-key")

    def fail_discovery(self, provider):
        raise ProviderHTTPError(503, RAW_ERROR)

    monkeypatch.setattr(
        "app.providers.openai_client.OpenAIClient.list_models",
        fail_discovery,
    )
    return Relay()


def test_optional_provider_discovery_failure_is_nonfatal_and_visible(monkeypatch):
    relay = _relay_with_optional_discovery_failure(monkeypatch)

    statuses = relay.provider_manager.registration_status()
    assert len(statuses) == 1
    assert statuses[0]["id"] == "openai"
    assert statuses[0]["status"] == "discovery_failed"
    assert statuses[0]["stage"] == "model_discovery"
    assert statuses[0]["error_kind"] == "server_error"
    assert relay.provider_manager.get("openai") is not None
    assert RAW_ERROR not in json.dumps(statuses)

    diagnostics = DiagnosticsService()._provider_registration(relay)
    assert diagnostics == statuses
    assert RAW_ERROR not in json.dumps(diagnostics)

    facade = ServiceFacade(relay_instance=relay)
    entry = next(item for item in facade.provider_catalog() if item.id == "openai")
    assert entry.registration_status == "discovery_failed"
    assert entry.registration_error_kind == "server_error"
    assert RAW_ERROR not in repr(entry)


def test_optional_provider_initialization_failure_is_nonfatal_and_visible(
    monkeypatch,
):
    defn = PROVIDER_REGISTRY["openai"]
    monkeypatch.setattr(
        relay_module,
        "PROVIDER_REGISTRY",
        {"openai": defn},
    )
    monkeypatch.setattr(relay_module, "RUNTIME_READY", {"openai"})
    monkeypatch.setattr(settings, "openai_enabled", True)

    def fail_initialization(_definition):
        raise ProviderHTTPError(503, RAW_ERROR)

    monkeypatch.setattr(
        relay_module,
        "build_runtime_provider_detailed",
        fail_initialization,
    )

    relay = Relay()
    statuses = relay.provider_manager.registration_status()

    assert statuses[0]["status"] == "initialization_failed"
    assert statuses[0]["stage"] == "runtime"
    assert statuses[0]["error_kind"] == "server_error"
    assert relay.provider_manager.get("openai") is None
    assert RAW_ERROR not in json.dumps(statuses)


def test_disabled_provider_registration_is_visible_without_loading_provider(
    monkeypatch,
):
    defn = PROVIDER_REGISTRY["openai"]
    monkeypatch.setattr(
        relay_module,
        "PROVIDER_REGISTRY",
        {"openai": defn},
    )
    monkeypatch.setattr(relay_module, "RUNTIME_READY", {"openai"})
    monkeypatch.setattr(settings, "openai_enabled", False)

    relay = Relay()
    statuses = relay.provider_manager.registration_status()

    assert statuses == [
        {
            "id": "openai",
            "name": "OpenAI",
            "status": "disabled",
            "stage": "configuration",
            "enabled": False,
            "error_kind": None,
        }
    ]
    assert relay.provider_manager.all() == []


def test_public_health_remains_minimal_while_deep_health_exposes_safe_status(
    monkeypatch,
):
    registration = [
        {
            "id": "optional",
            "name": "Optional",
            "status": "initialization_failed",
            "stage": "runtime",
            "enabled": True,
            "error_kind": "unknown",
        }
    ]
    relay = SimpleNamespace(
        health=lambda deep=False: {
            "deep": deep,
            "providers": [],
            "registrations": registration,
        }
    )
    monkeypatch.setattr(app.api.health, "relay", relay)

    with TestClient(fastapi_app) as client:
        public = client.get("/health")
        deep = client.get("/health/deep")

    assert public.status_code == 200
    assert public.json() == {"status": "unavailable"}
    assert deep.status_code == 200
    assert deep.json()["registrations"] == registration
    assert set(deep.json()) == {"deep", "providers", "registrations"}
