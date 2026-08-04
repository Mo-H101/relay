import logging
import os
from typing import Any, Optional

from app.core.config import settings
from app.services.provider_manager import ProviderManager
from app.services.correlation import new_correlation_id
from app.services.health_checker import HealthChecker
from app.services.health_refresher import HealthRefresher
from app.services.health_store import HealthStore
from app.services.candidate_builder import CandidateBuilder
from app.services.chat_service import ChatService
from app.services.async_chat_service import AsyncChatService
from app.services.decision_engine import DecisionEngine
from app.services.routing import RoutingEngine
from app.services.log_service import RequestLogger
from app.services.telemetry import TelemetryStore
from app.services.quality import QualityStore
from app.services.state_store import StateStore, StateStoreError
from app.services.state_flusher import StateFlusher
from app.providers.factory import build_runtime_provider
from app.providers.registry import PROVIDER_REGISTRY, RUNTIME_READY

_logger = logging.getLogger("relay")


class Relay:
    """
    Main Relay application.
    """

    def __init__(self) -> None:
        self.provider_manager = ProviderManager()
        self.health_store = HealthStore(
            ttl_seconds=settings.health_ttl_seconds,
            degraded_ttl_seconds=settings.health_degraded_ttl_seconds,
            unavailable_ttl_seconds=settings.health_unavailable_ttl_seconds,
            freshness_exponent=settings.health_freshness_exponent,
            model_server_error_threshold=(
                settings.health_feedback_model_server_error_threshold
            ),
            provider_server_error_threshold=(
                settings.health_feedback_provider_server_error_threshold
            ),
            model_timeout_degraded_threshold=(
                settings.health_feedback_model_timeout_degraded_threshold
            ),
            model_timeout_unavailable_threshold=(
                settings.health_feedback_model_timeout_unavailable_threshold
            ),
            model_invalid_request_unavailable_threshold=(
                settings.health_feedback_model_invalid_request_unavailable_threshold
            ),
            model_unknown_degraded_threshold=(
                settings.health_feedback_model_unknown_degraded_threshold
            ),
        )
        self.health_checker = HealthChecker(health_store=self.health_store)
        self.health_refresher = HealthRefresher(
            provider_manager=self.provider_manager,
            health_checker=self.health_checker,
            interval_seconds=settings.health_refresh_interval_seconds,
            deep=settings.health_deep_refresh_enabled,
        )
        self.routing = RoutingEngine()
        self.telemetry = TelemetryStore(
            max_failure_history=settings.telemetry_max_failure_history,
            ewma_alpha=settings.adaptive_learning_rate,
        )
        self.quality_store = QualityStore(
            min_samples=settings.quality_feedback_min_samples,
            learning_rate=settings.quality_feedback_learning_rate,
            retention_limit=settings.quality_feedback_retention_limit,
        )
        self.candidate_builder = CandidateBuilder(
            routing=self.routing,
            health_store=self.health_store,
            telemetry=self.telemetry,
            quality_store=self.quality_store,
        )
        self.decision_engine = DecisionEngine(
            builder=self.candidate_builder,
        )
        self.chat_service = ChatService()
        self.async_chat_service = AsyncChatService()
        self.request_logger = RequestLogger()

        self.state_store: Optional[StateStore] = None
        self.state_flusher: Optional[StateFlusher] = None
        self.persistence_init_error: Optional[str] = None

        if settings.persistence_enabled:
            self._init_persistence()

        self._load_providers()

    def _init_persistence(self) -> None:
        """
        Open the StateStore, load persisted state, and start the write-
        behind flusher. Failure to open or load disables persistence for
        this process rather than failing startup.
        """
        path = settings.persistence_path
        parent = os.path.dirname(path)

        if parent:
            os.makedirs(parent, exist_ok=True)

        try:
            self.state_store = StateStore(path)
        except StateStoreError as exc:
            _logger.warning(
                "persistence unavailable; continuing without it: %s", path
            )
            self.persistence_init_error = str(exc)
            self.state_store = None
            return

        self._load_state()

        if self.state_store is None:
            return

        self.state_flusher = StateFlusher(
            health_store=self.health_store,
            telemetry=self.telemetry,
            state_store=self.state_store,
            quality_store=self.quality_store,
            decision_engine=self.decision_engine,
            interval_seconds=settings.persistence_flush_interval_seconds,
            retention_days=settings.persistence_retention_days,
        )

    def _load_state(self) -> None:
        """
        Restore persisted learned health, telemetry, quality, and
        decision statistics into the in-memory stores.
        """
        try:
            learned = self.state_store.load_learned_state()
            self.health_store.import_learned_state(learned)

            telemetry = self.state_store.load_telemetry()
            self.telemetry.import_state(telemetry)

            quality = self.state_store.load_quality()
            self.quality_store.import_state(quality)

            decision = self.state_store.load_decision_stats()
            if decision is not None:
                self.decision_engine.import_state(decision)
        except Exception as exc:
            _logger.exception(
                "persisted state load failed; disabling persistence"
            )
            self.persistence_init_error = str(exc)
            self.state_store.close()
            self.state_store = None

    def _load_providers(self):
        for defn in PROVIDER_REGISTRY.values():
            if defn.id not in RUNTIME_READY:
                continue

            if not getattr(settings, defn.enabled_attr, False):
                continue

            try:
                self.provider_manager.register(build_runtime_provider(defn))
            except Exception:
                continue

    def choose_provider(self):
        """
        Return the provider chat would select first, using the same
        candidate intelligence path as /chat (routing, health awareness,
        telemetry scoring). Falls back to priority selection when there is
        no health/telemetry data.

        When the decision engine is enabled, selection flows through it
        and produces an explicit DecisionScore; the selected candidate is
        identical to the first candidate of the hot path, so this method
        never diverges from /chat.
        """

        providers = self.provider_manager.ranked()

        if not providers:
            return None

        if self.decision_engine.enabled:
            decision = self.decision_engine.decide(providers)

            if decision.selected is None:
                return self.provider_manager.best()

            for provider in providers:
                if provider.name == decision.selected.provider:
                    return provider

            return self.provider_manager.best()

        candidates = self.candidate_builder.build(providers)

        if not candidates:
            return self.provider_manager.best()

        return candidates[0][0]

    def chat(self, message: str, task: str | None = None, correlation_id: str | None = None, **generation_kwargs: Any) -> dict:
        """
        Send a message through the highest-priority available provider,
        failing over intelligently across models and providers.

        A correlation id is generated per request (or echoed from the
        caller) and carried on the result dict so the API layer can emit
        it as a response header and the request logger can tag log
        records. It is never persisted.
        """

        cid = correlation_id or new_correlation_id()

        providers = self.provider_manager.ranked()

        if not providers:
            return {
                "success": False,
                "error": "No provider available.",
                "correlation_id": cid,
            }

        candidates = self.candidate_builder.build(providers, task=task)

        if self.decision_engine.enabled:
            self.decision_engine.decide(providers, task=task)

        result = self.chat_service.chat_across(
            candidates,
            message,
            max_retries=settings.max_retries,
            **generation_kwargs,
        )

        result["correlation_id"] = cid

        self.request_logger.chat(result)

        if settings.telemetry_enabled:
            self._record_telemetry(result)

        if settings.health_feedback_enabled:
            self._record_feedback(result)

        return result

    async def achat(
        self,
        message: str,
        task: str | None = None,
        correlation_id: str | None = None,
        **generation_kwargs: Any,
    ) -> dict:
        """
        Async version of chat(). Sends a message through the highest-priority
        available provider, failing over intelligently across models and
        providers.

        Uses the async chat service to avoid blocking the event loop.
        """

        cid = correlation_id or new_correlation_id()

        providers = self.provider_manager.ranked()

        if not providers:
            return {
                "success": False,
                "error": "No provider available.",
                "correlation_id": cid,
            }

        candidates = self.candidate_builder.build(providers, task=task)

        if self.decision_engine.enabled:
            self.decision_engine.decide(providers, task=task)

        result = await self.async_chat_service.achat_across(
            candidates,
            message,
            max_retries=settings.max_retries,
            **generation_kwargs,
        )

        result["correlation_id"] = cid

        self.request_logger.chat(result)

        if settings.telemetry_enabled:
            self._record_telemetry(result)

        if settings.health_feedback_enabled:
            self._record_feedback(result)

        return result

    def _record_telemetry(self, result: dict) -> None:
        """
        Record per-attempt operational telemetry from the completed chat.
        """

        for attempt in result.get("attempts", []):
            provider = attempt.get("provider")
            model = attempt.get("model")

            if not provider or not model:
                continue

            self.telemetry.record_attempt(
                provider,
                model,
                success=bool(attempt.get("success")),
                latency_ms=attempt.get("latency_ms") or 0,
                failure_type=attempt.get("failure_type"),
            )

    def _record_feedback(self, result: dict) -> None:
        """
        Feed chat attempt outcomes into the health store so future
        candidate selection can learn from real failures.
        """

        for attempt in result.get("attempts", []):
            provider = attempt.get("provider")
            model = attempt.get("model")

            if not provider or not model:
                continue

            if attempt.get("success"):
                self.health_store.record_success(provider, model)
            else:
                self.health_store.record_failure(
                    provider,
                    model,
                    attempt.get("failure_type") or "unknown",
                )

    def health(self, deep: bool = False):
        results = []

        for provider in self.provider_manager.all():
            report = self.health_checker.check(provider, deep=deep)

            results.append(
                {
                    "name": report.name,
                    "status": report.status,
                    "latency_ms": report.latency_ms,
                    "last_checked": report.last_checked,
                    "details": report.details,
                    "connectivity": report.connectivity,
                    "rate_limit_status": report.rate_limit_status,
                    "healthy_models": report.healthy_models,
                    "degraded_models": report.degraded_models,
                    "unavailable_models": report.unavailable_models,
                    "unsupported_models": report.unsupported_models,
                    "last_successful_request": (
                        report.last_successful_request
                    ),
                    "models": [
                        {
                            "name": model.name,
                            "status": model.status,
                            "capability": model.capability,
                            "latency_ms": model.latency_ms,
                            "status_code": model.status_code,
                            "error": model.error,
                        }
                        for model in report.models
                    ],
                }
            )

        return {"deep": deep, "providers": results}


relay = Relay()
