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
    ) -> None:
        self.name = name
        self.base_url = base_url
        self.enabled = enabled
        self.priority = priority
        self.requires_api_key = requires_api_key
        self.api_key = api_key
        self.models = list(models or [])

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


class FakeProviderManager:
    def __init__(self) -> None:
        self._providers: list[FakeProvider] = []

    def register(self, provider: FakeProvider) -> None:
        self._providers.append(provider)

    def all(self) -> list[FakeProvider]:
        return list(self._providers)

    def enabled(self) -> list[FakeProvider]:
        return [p for p in self._providers if p.enabled]


class FakeRelay:
    def __init__(self) -> None:
        self.provider_manager = FakeProviderManager()
        self.health_store = FakeHealthStore()
        self.persistence_init_error: str | None = None

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
