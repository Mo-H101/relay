from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import List
import time

import httpx

from app.providers.availability import classify_probe
from app.providers.base import Provider
from app.providers.openai_compat_client import proxy_request_kwargs
from app.services.capabilities import (
    detect_capability,
    is_chat_testable,
)
from app.services.client_registry import ClientRegistry
from app.services.health_store import HealthStore
from app.services.metrics import relay_metrics

HEALTHY = "healthy"
DEGRADED = "degraded"
UNAVAILABLE = "unavailable"
UNSUPPORTED = "unsupported"
NOT_CHECKED = "not_checked"

_DEFAULT_PROBE_COUNT = 5
_MAX_PROBE_COUNT = 30


@dataclass
class ModelHealth:
    """
    Result of a model health check.
    """

    name: str
    status: str
    capability: str = "chat"
    latency_ms: int = 0
    status_code: int | None = None
    error: str = ""


@dataclass
class ProviderHealth:
    """
    Result of a provider health check.
    """

    name: str
    status: str
    latency_ms: int
    last_checked: str
    details: str
    connectivity: bool
    rate_limit_status: str
    last_successful_request: str | None
    healthy_models: List[str] = field(default_factory=list)
    degraded_models: List[str] = field(default_factory=list)
    unavailable_models: List[str] = field(default_factory=list)
    unsupported_models: List[str] = field(default_factory=list)
    models: List[ModelHealth] = field(default_factory=list)


class HealthChecker:
    """
    Responsible for checking provider availability.

    A normal check probes only provider connectivity and the configured
    priority models. A deep check probes every chat-capable model and
    reports unsupported models without probing them.
    """

    def __init__(self, health_store: HealthStore | None = None) -> None:
        self.registry = ClientRegistry()
        self._last_success: dict = {}
        self._health_store = health_store

    def check(
        self,
        provider: Provider,
        deep: bool = False,
    ) -> ProviderHealth:
        connectivity, details, latency = self._check_connectivity(provider)

        models: List[ModelHealth] = []

        if connectivity:
            models = self._check_models(provider, deep=deep)

        healthy = [model for model in models if model.status == HEALTHY]
        degraded = [model for model in models if model.status == DEGRADED]
        unavailable = [
            model for model in models if model.status == UNAVAILABLE
        ]
        unsupported = [
            model for model in models if model.status == UNSUPPORTED
        ]

        if healthy:
            self._last_success[provider.name] = (
                datetime.now(UTC).isoformat()
            )

        chat_models = [
            model for model in provider.models if is_chat_testable(model)
        ]

        if not connectivity:
            provider_status = UNAVAILABLE
        elif not chat_models:
            provider_status = UNSUPPORTED
        elif healthy:
            provider_status = HEALTHY
        elif degraded:
            provider_status = DEGRADED
        elif models:
            provider_status = UNAVAILABLE
        else:
            provider_status = NOT_CHECKED

        rate_limited = any(
            model.status_code == 429 for model in models
        )

        rate_limit_status = (
            "rate_limited"
            if rate_limited
            else ("ok" if models else "unknown")
        )

        report = ProviderHealth(
            name=provider.name,
            status=provider_status,
            latency_ms=latency,
            last_checked=datetime.now(UTC).isoformat(),
            details=details,
            connectivity=connectivity,
            rate_limit_status=rate_limit_status,
            last_successful_request=self._last_success.get(provider.name),
            healthy_models=[model.name for model in healthy],
            degraded_models=[model.name for model in degraded],
            unavailable_models=[model.name for model in unavailable],
            unsupported_models=[model.name for model in unsupported],
            models=models,
        )

        if self._health_store is not None:
            self._health_store.save(report)

        relay_metrics.update_provider_health(report)

        return report

    def _client_for(self, provider: Provider):
        """
        Resolve the provider's client, or None when unknown so the
        generic connectivity probe falls back for legacy providers.
        """
        try:
            return self.registry.get(provider.identity())
        except Exception:
            return None

    def _check_connectivity(self, provider: Provider):
        """
        Probe the provider endpoint. Returns (ok, details, latency_ms).

        Clients that implement ``connectivity_probe`` supply their own
        auth convention (Anthropic sends ``x-api-key``, Gemini sends a
        query key). Otherwise the generic Bearer GET is used, which is
        correct for OpenAI-compatible providers and keyless ones.
        """

        probe = getattr(self._client_for(provider), "connectivity_probe", None)

        if probe is not None:
            return probe(provider)

        start = time.perf_counter()

        headers = {}

        if provider.has_api_key():
            headers["Authorization"] = f"Bearer {provider.api_key}"

        url = provider.base_url.rstrip("/") + provider.health_endpoint

        try:
            response = httpx.get(
                url,
                headers=headers,
                timeout=10,
                **proxy_request_kwargs(provider, url),
            )

            ok = response.status_code == 200
            details = f"HTTP {response.status_code}"

        except Exception as exc:
            ok = False
            details = str(exc)

        latency = int((time.perf_counter() - start) * 1000)

        return ok, details, latency

    def _check_models(
        self,
        provider: Provider,
        deep: bool = False,
    ) -> List[ModelHealth]:
        """
        Probe chat-capable models. In deep mode every chat-capable model is
        probed.

        In normal mode only configured priority models are probed. If none
        are configured, probing escalates through chat models until a healthy
        one is found (bounded by _MAX_PROBE_COUNT).

        Unsupported models are reported without probing in both modes.
        """

        chat_models = [
            model for model in provider.models if is_chat_testable(model)
        ]

        if deep:
            to_probe = chat_models
        else:
            to_probe = [
                model
                for model in provider.priority_models
                if model in chat_models
            ]

            if not to_probe:
                results: List[ModelHealth] = []
                for count in range(
                    _DEFAULT_PROBE_COUNT,
                    _MAX_PROBE_COUNT + 1,
                    _DEFAULT_PROBE_COUNT,
                ):
                    next_batch = chat_models[len(results):count]
                    results.extend(
                        self._probe_models(provider, next_batch)
                    )
                    if any(r.status == HEALTHY for r in results):
                        return results + self._unsupported_models(provider)
                return results + self._unsupported_models(provider)

        results = self._probe_models(provider, to_probe)

        return results + self._unsupported_models(provider)

    def _unsupported_models(self, provider: Provider) -> List[ModelHealth]:
        """
        Models that cannot be health-checked via chat probes.
        """

        return [
            ModelHealth(
                name=model,
                status=UNSUPPORTED,
                capability=detect_capability(model).value,
            )
            for model in provider.models
            if not is_chat_testable(model)
        ]

    def _probe_models(
        self,
        provider: Provider,
        model_names: List[str],
    ) -> List[ModelHealth]:
        if not model_names:
            return []

        try:
            client = self.registry.get(provider.identity())
        except RuntimeError:
            return [
                ModelHealth(
                    name=model,
                    status=UNAVAILABLE,
                    capability=detect_capability(model).value,
                    error="no client registered",
                )
                for model in model_names
            ]

        results: List[ModelHealth] = []

        with ThreadPoolExecutor(max_workers=12) as executor:
            probes = executor.map(
                lambda model: client.probe_model(provider, model),
                model_names,
            )

            for model, probe in zip(model_names, probes):

                status = {
                    "available": HEALTHY,
                    "overloaded": DEGRADED,
                    "unavailable": UNAVAILABLE,
                }[classify_probe(probe)]

                results.append(
                    ModelHealth(
                        name=model,
                        status=status,
                        capability=detect_capability(model).value,
                        latency_ms=probe.latency_ms,
                        status_code=probe.status_code,
                        error=probe.error,
                    )
                )

        return results
