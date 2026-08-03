"""
View-model layer for the Relay TUI.

This module is deliberately **Textual-free**: it only imports core and
service modules, so it can be unit-tested headlessly and so the import
boundary between the TUI and the rest of Relay stays verifiable. Screens
call ``ServiceFacade`` and never touch ``app.core.relay`` directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.relay import relay
from app.providers.availability import GLYPH
from app.services import setup_state
from app.services.capabilities import is_chat_testable
from app.services.ops_store import ops_store
from app.setup.scan import ScanEngine


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    base_url: str
    enabled: bool
    priority: int
    requires_api_key: bool
    has_api_key: bool
    models: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ModelInfo:
    provider: str
    name: str
    status: str  # "healthy" | "degraded" | "unavailable" | "unsupported" | "unknown"
    latency_ms: int | None = None
    error: str = ""


@dataclass(frozen=True)
class ServerStatus:
    running: bool
    host: str
    port: int
    url: str


@dataclass(frozen=True)
class ChatCandidate:
    """
    A chat-testable (provider, model) pair for the specific-model picker,
    tagged with its last known availability status.
    """

    provider: str
    model: str
    status: str  # healthy | degraded | unavailable | unsupported | unknown


# Health-status -> availability glyph mapping shared by the picker and the
# inline probe. Probe statuses (available/overloaded/unavailable) come from
# app.providers.availability.GLYPH directly.
_HEALTH_GLYPH = {
    "healthy": GLYPH["available"],
    "degraded": GLYPH["overloaded"],
    "unavailable": GLYPH["unavailable"],
    "unsupported": "\u003f",
    "unknown": "-",
}


def candidate_glyph(status: str) -> str:
    return _HEALTH_GLYPH.get(status, "-")


@dataclass(frozen=True)
class DashboardSummary:
    relay_name: str
    server: ServerStatus
    setup_state: str
    provider_count: int
    enabled_providers: int
    default_provider: str
    model_count: int
    healthy_models: int
    requests: int
    successes: int
    failures: int
    success_rate: float | None
    average_latency_ms: float | None
    chats: int
    chat_attempts: int
    persistence_enabled: bool
    persistence_error: str
    env_file: str
    state_dir: str


class ServiceFacade:
    """
    Read-only projection of the shared ``relay``/``settings`` singletons.
    The TUI's screens use this; it never mutates configuration.
    """

    def __init__(self, relay_instance: Any = relay) -> None:
        self._relay = relay_instance

    # ------------------------------------------------------------- server

    def server_running(self) -> bool:
        return getattr(self._relay, "_embedded_server_running", False)

    def server_url(self) -> str:
        host = settings.relay_host
        port = settings.relay_port
        display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        return f"http://{display_host}:{port}"

    # ------------------------------------------------------------ settings

    def relay_name(self) -> str:
        return settings.relay_name

    def default_provider(self) -> str:
        return settings.default_provider

    def persistence_enabled(self) -> bool:
        return settings.persistence_enabled

    def env_file_path(self) -> str:
        from app.core.config import env_file
        return str(env_file)

    def state_dir_path(self) -> str:
        from app.core.config import state_dir
        return str(state_dir)

    def setup_state(self) -> str:
        return setup_state.read_setup_state()

    # ------------------------------------------------------------- health

    def health(self, deep: bool = False) -> dict:
        return self._relay.health(deep=deep)

    # ------------------------------------------------------------ providers

    def providers(self) -> list[ProviderInfo]:
        return [
            ProviderInfo(
                name=provider.name,
                base_url=provider.base_url,
                enabled=provider.enabled,
                priority=provider.priority,
                requires_api_key=provider.requires_api_key,
                has_api_key=provider.has_api_key(),
                models=list(provider.models),
            )
            for provider in self._relay.provider_manager.all()
        ]

    # --------------------------------------------------------------- models

    def models(self) -> list[ModelInfo]:
        """
        Union of models across providers, tagged with availability from
        the health store. Providers with no health record contribute
        "unknown" entries.
        """
        results: list[ModelInfo] = []
        seen: set[tuple[str, str]] = set()

        for provider in self._relay.provider_manager.all():
            report = self._relay.health_store.get(provider.name)
            reported: set[str] = set()

            if report is not None:
                for model in report.models:
                    key = (provider.name, model.name)
                    if key in seen:
                        continue
                    seen.add(key)
                    reported.add(model.name)
                    results.append(
                        ModelInfo(
                            provider=provider.name,
                            name=model.name,
                            status=model.status,
                            latency_ms=model.latency_ms,
                            error=model.error,
                        )
                    )

            for model_name in provider.models:
                key = (provider.name, model_name)
                if key in seen:
                    continue
                seen.add(key)
                results.append(
                    ModelInfo(
                        provider=provider.name,
                        name=model_name,
                        status="unknown",
                    )
                )

        return results

    # ------------------------------------------------------------ activity

    def ops_stats(self) -> dict:
        return ops_store.stats()

    def ops_events(self) -> list:
        return ops_store.events()

    # ---------------------------------------------------------------- chat

    def specific_model_candidates(self) -> list[ChatCandidate]:
        """
        Chat-testable (provider, model) pairs across all providers, tagged
        with the health store's last known status.
        """
        results: list[ChatCandidate] = []

        for provider in self._relay.provider_manager.all():
            report = self._relay.health_store.get(provider.name)
            status_by_model: dict[str, str] = {}

            if report is not None:
                status_by_model = {
                    model.name: model.status for model in report.models
                }

            for model in provider.models:
                if not is_chat_testable(model):
                    continue
                results.append(
                    ChatCandidate(
                        provider=provider.name,
                        model=model,
                        status=status_by_model.get(model, "unknown"),
                    )
                )

        return results

    def _chat_candidates(self, provider, model: str | None):
        """
        Build the candidate list for a chat request: a specific model when
        given, otherwise every chat-testable model of the provider.
        """
        if model is not None:
            return [(provider, model)]

        return [
            (provider, candidate_model)
            for candidate_model in provider.models
            if is_chat_testable(candidate_model)
        ]

    def random_chat(self, message: str, **generation_kwargs: Any) -> dict:
        """
        Chat against the provider Relay would select first (same candidate
        path as /chat), failing over across its chat-testable models.
        Returns the chat_across result dict; never raises.
        """
        provider = self._relay.choose_provider()

        if provider is None:
            return {
                "success": False,
                "error": "No provider available. Configure a provider first.",
            }

        candidates = self._chat_candidates(provider, None)

        if not candidates:
            return {
                "success": False,
                "error": f"No chat-testable models for {provider.name}.",
            }

        return self._relay.chat_service.chat_across(
            candidates,
            message,
            max_retries=settings.max_retries,
            **generation_kwargs,
        )

    def specific_chat(
        self,
        provider_name: str,
        model: str,
        message: str,
        **generation_kwargs: Any,
    ) -> dict:
        """
        Non-streaming chat against a specific (provider, model). Returns
        the chat_across result dict; never raises.
        """
        provider = self._relay.provider_manager.get(provider_name)

        if provider is None:
            return {
                "success": False,
                "error": f"Unknown provider '{provider_name}'.",
            }

        return self._relay.chat_service.chat_across(
            [(provider, model)],
            message,
            max_retries=settings.max_retries,
            **generation_kwargs,
        )

    def start_stream(
        self,
        provider_name: str,
        model: str,
        message: str,
        **generation_kwargs: Any,
    ) -> dict:
        """
        Start a streaming chat against a specific (provider, model).

        Returns the chat_across_stream_messages result dict with a
        ``stream_gen`` (yielding parsed chunk dicts) on success. The
        caller consumes the generator off the UI thread.
        """
        provider = self._relay.provider_manager.get(provider_name)

        if provider is None:
            return {
                "success": False,
                "stream_gen": None,
                "error": f"Unknown provider '{provider_name}'.",
                "attempts": [],
            }

        payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": message}],
            "model": model,
            "stream": True,
        }
        for key in ("temperature", "top_p", "max_tokens"):
            if key in generation_kwargs and generation_kwargs[key] is not None:
                payload[key] = generation_kwargs[key]

        return self._relay.chat_service.chat_across_stream_messages(
            [(provider, model)],
            payload,
            max_retries=settings.max_retries,
        )

    def probe_model(self, provider_name: str, model: str):
        """
        Single-model live availability probe (inline ✓/⚠/✗ test) using
        ScanEngine. Returns a ScanResult or None for unknown providers.
        """
        provider = self._relay.provider_manager.get(provider_name)

        if provider is None:
            return None

        client = self._relay.chat_service.registry.get(provider_name)
        engine = ScanEngine(concurrency=1)
        results = engine.scan(client, provider, [model])

        return results[0]

    # ----------------------------------------------------------- dashboard

    def dashboard_summary(self) -> DashboardSummary:
        stats = self.ops_stats()
        models = self.models()

        return DashboardSummary(
            relay_name=self.relay_name(),
            server=ServerStatus(
                running=self.server_running(),
                host=settings.relay_host,
                port=settings.relay_port,
                url=self.server_url(),
            ),
            setup_state=self.setup_state(),
            provider_count=len(self._relay.provider_manager.all()),
            enabled_providers=len(self._relay.provider_manager.enabled()),
            default_provider=self.default_provider(),
            model_count=len(models),
            healthy_models=sum(
                1 for model in models if model.status == "healthy"
            ),
            requests=stats.get("requests", 0),
            successes=stats.get("successes", 0),
            failures=stats.get("failures", 0),
            success_rate=stats.get("success_rate"),
            average_latency_ms=stats.get("average_latency_ms"),
            chats=stats.get("chats", 0),
            chat_attempts=stats.get("chat_attempts", 0),
            persistence_enabled=self.persistence_enabled(),
            persistence_error=self._relay.persistence_init_error or "",
            env_file=self.env_file_path(),
            state_dir=self.state_dir_path(),
        )
