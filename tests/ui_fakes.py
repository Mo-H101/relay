"""
Shared fake Relay components for TUI unit tests.

The fakes mirror the real facade surface (`app/core/relay.py` +
`app/services/provider_manager.py` + `app/services/health_store.py`) so
``ServiceFacade`` and the screens can be exercised without touching the
module-level singleton or doing any network I/O.
"""

from __future__ import annotations


class FakeProvider:
    def __init__(
        self,
        name: str,
        *,
        base_url: str = "http://fake",
        enabled: bool = True,
        priority: int = 0,
        requires_api_key: bool = True,
        api_key: str = "",
        models: list[str] | None = None,
        priority_models: list[str] | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url
        self.enabled = enabled
        self.priority = priority
        self.requires_api_key = requires_api_key
        self.api_key = api_key
        self.models = list(models or [])
        self.priority_models = list(priority_models or [])

    def has_api_key(self) -> bool:
        return bool(self.api_key.strip())


class FakeHealthModel:
    def __init__(
        self,
        name: str,
        status: str,
        *,
        latency_ms: int | None = None,
        error: str = "",
    ) -> None:
        self.name = name
        self.status = status
        self.latency_ms = latency_ms
        self.error = error


class FakeReport:
    def __init__(self, name: str, models: list[FakeHealthModel] | None = None) -> None:
        self.name = name
        self.models = list(models or [])


class FakeHealthStore:
    def __init__(self) -> None:
        self._reports: dict[str, FakeReport] = {}

    def set(self, report: FakeReport) -> None:
        self._reports[report.name] = report

    def get(self, name: str):
        return self._reports.get(name)

    def export_learned_state(self) -> dict:
        return {}


class FakeProviderManager:
    def __init__(self) -> None:
        self._providers: list[FakeProvider] = []

    def register(self, provider: FakeProvider) -> None:
        self._providers.append(provider)

    def all(self) -> list[FakeProvider]:
        return list(self._providers)

    def enabled(self) -> list[FakeProvider]:
        return [p for p in self._providers if p.enabled]

    def ranked(self) -> list[FakeProvider]:
        return sorted(self.enabled(), key=lambda p: p.priority)

    def get(self, name: str) -> FakeProvider | None:
        for provider in self._providers:
            if provider.name == name:
                return provider
        return None


class FakeTelemetry:
    """Empty per-(provider, model) telemetry surface for diagnostics."""

    def all(self) -> list:
        return []

    def record(self, *args, **kwargs) -> None:
        pass


class FakeClient:
    def __init__(self, probe_result=None) -> None:
        self._probe_result = probe_result

    def probe_model(self, provider, model):
        return self._probe_result


class FakeRegistry:
    def __init__(self) -> None:
        self._clients: dict[str, FakeClient] = {}

    def register(self, provider_name: str, client: FakeClient) -> None:
        self._clients[provider_name] = client

    def get(self, provider_name: str) -> FakeClient | None:
        return self._clients.get(provider_name)


class FakeChatService:
    def __init__(self) -> None:
        self.registry = FakeRegistry()
        self.chat_across_calls: list[tuple] = []
        self.stream_calls: list[tuple] = []

    def chat_across(self, candidates, message, **kwargs) -> dict:
        self.chat_across_calls.append((candidates, message, kwargs))
        provider = candidates[0][0] if candidates else None
        model = candidates[0][1] if candidates else ""
        return {
            "success": True,
            "provider": provider.name if provider else "",
            "model": model,
            "response": f"echo: {message}",
            "latency_ms": 12,
            "attempts": [],
        }

    def chat_across_stream_messages(self, candidates, payload, **kwargs) -> dict:
        self.stream_calls.append((candidates, payload, kwargs))
        provider, model = candidates[0]

        def gen():
            yield {"choices": [{"delta": {"content": "hello"}}]}
            yield {"choices": [{"delta": {"content": " world"}}]}

        return {
            "success": True,
            "provider": provider.name,
            "model": model,
            "stream_gen": gen(),
            "error": None,
            "attempts": [],
        }


class FakeRelay:
    def __init__(self) -> None:
        self.provider_manager = FakeProviderManager()
        self.health_store = FakeHealthStore()
        self.chat_service = FakeChatService()
        self.telemetry = FakeTelemetry()
        self.state_store = None
        self.state_flusher = None
        self.persistence_init_error: str | None = None

    def choose_provider(self) -> FakeProvider | None:
        for provider in self.provider_manager.enabled():
            return provider
        return None

    def health(self, deep: bool = False) -> dict:
        return {"deep": deep, "providers": []}


def make_relay(providers: list[FakeProvider]) -> FakeRelay:
    """
    Build a fake relay and register health reports matching each
    provider's model list (all "healthy").
    """
    relay = FakeRelay()
    for provider in providers:
        relay.provider_manager.register(provider)
        relay.health_store.set(
            FakeReport(
                provider.name,
                [
                    FakeHealthModel(model, "healthy", latency_ms=42)
                    for model in provider.models
                ],
            )
        )
    return relay


class FakeStore:
    """
    Recording stand-in for ``app.services.config_store``. ``get_env``
    returns the recorded value (or the default); the write surface is
    recorded so tests can assert what would be persisted.
    """

    def __init__(self) -> None:
        self.writes: list[tuple[str, dict]] = []
        self.env_writes: list[tuple[str, str | None]] = []
        self.env_file = None
        self._values: dict[str, str] = {}

    def get_env(self, key: str, default: str = "") -> str:
        return self._values.get(key, default)

    def set_env(self, key: str, value: str) -> None:
        self._values[key] = value
        self.env_writes.append((key, value))

    def unset_env(self, key: str) -> None:
        self._values.pop(key, None)
        self.env_writes.append((key, None))

    def set_provider_config(self, defn, **kwargs) -> None:
        self.writes.append((defn.id, kwargs))


class FakeReloader:
    """
    Recording stand-in for ``app.services.reload.reload_config``.
    """

    def __init__(self, report: dict | None = None) -> None:
        self.report = report or {
            "reloaded": True,
            "dry_run": False,
            "applied": ["nvidia_model_priority"],
            "unchanged": [],
            "failures": [],
        }
        self.calls: list = []
        self.kwargs: list[dict] = []

    def __call__(self, relay, **kwargs) -> dict:
        self.calls.append(relay)
        self.kwargs.append(kwargs)
        return dict(self.report)
