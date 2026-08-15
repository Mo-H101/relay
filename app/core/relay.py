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
from app.services.decision_record import DecisionRecordStore, record_actual_decision
from app.services.routing import RoutingEngine
from app.services.log_service import RequestLogger
from app.services.telemetry import TelemetryStore
from app.services.quality import QualityStore
from app.services.state_store import StateStore, StateStoreError
from app.services.state_flusher import StateFlusher
from app.services.conversation_store import ConversationStore
from app.services.continuity_flusher import ContinuityFlusher
from app.services.context_manager import ContextManager
from app.services.handoff import HandoffCoordinator
from app.services.metrics import relay_metrics
from app.services import platform_store
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
        # Bounded in-memory store of the actual decisions requests made
        # (Phase 7 orchestration truth layer). Metadata only, never
        # persisted; old records are evicted past the bound.
        self.decision_record_store = DecisionRecordStore()
        self.chat_service = ChatService()
        self.async_chat_service = AsyncChatService()
        self.request_logger = RequestLogger()

        self.state_store: Optional[StateStore] = None
        self.state_flusher: Optional[StateFlusher] = None
        self.persistence_init_error: Optional[str] = None

        if settings.persistence_enabled:
            self._init_persistence()

        # P9 project continuity. Disabled by default: the store and
        # flusher are None and every continuity path is inert.
        self.conversation_store: Optional[ConversationStore] = None
        self.continuity_flusher: Optional[ContinuityFlusher] = None
        self.continuity_handoff: Optional[HandoffCoordinator] = None
        self.continuity_recovery = None

        if settings.continuity_enabled:
            self._init_continuity()

        relay_metrics.continuity_enabled.set(
            1 if settings.continuity_enabled else 0
        )

        self._load_providers()

    def _init_continuity(self) -> None:
        """
        Open the ConversationStore and start the write-behind
        ContinuityFlusher on the shared platform.db. Continuity is
        additive on top of the existing routing; a store failure never
        affects startup or the chat path.
        """
        path = settings.persistence_path or str(platform_store.default_path())

        self.conversation_store = ConversationStore(path)
        self.continuity_flusher = ContinuityFlusher(
            conversation_store=self.conversation_store,
            interval_seconds=settings.continuity_flush_interval_seconds,
            retention_days=settings.continuity_retention_days,
        )
        from app.services.continuity_recovery import ContinuityRecovery

        self.continuity_recovery = ContinuityRecovery(
            self.conversation_store,
            max_resume_replays=settings.max_resume_replays,
        )
        self.continuity_handoff = HandoffCoordinator(
            flusher=self.continuity_flusher,
            context_manager=ContextManager(),
            recovery=self.continuity_recovery,
        )

    def validate_resume(self, continuity_scope) -> None:
        """
        P9d: validate a presented resume token for a resolved continuity
        scope and attach the decision as ``scope["resume"]``. Never
        raises; a failure degrades to a denial. Mutates the scope in
        place so ``begin_continuity_turn`` sees the decision.
        """
        if not isinstance(continuity_scope, dict):
            return
        recovery = self.continuity_recovery
        if recovery is None:
            continuity_scope["resume"] = {
                "attempted": False,
                "valid": False,
                "reason": "unavailable",
                "state": "active",
                "last_seq": None,
            }
            return
        try:
            continuity_scope["resume"] = recovery.validate_resume(
                continuity_scope.get("conversation_id"),
                continuity_scope.get("key_id"),
                continuity_scope.get("resume_token"),
            )
        except Exception:  # noqa: BLE001 - continuity never breaks chat
            continuity_scope["resume"] = {
                "attempted": False,
                "valid": False,
                "reason": "error",
                "state": "active",
                "last_seq": None,
            }

    def begin_continuity_turn(self, continuity_scope):
        """
        Start a P9c turn from a resolved continuity scope, or return None
        when continuity is off or the coordinator is unavailable. Never
        raises; continuity must never break chat.

        P9d: when the scope carries a valid resume decision the durable
        resume envelope is hydrated so the resumed turn continues from the
        last safe point and never repeats acknowledged work.
        """
        if not continuity_scope or self.continuity_handoff is None:
            return None
        try:
            resume = continuity_scope.get("resume") or {}
            resume_envelope = None
            if resume.get("valid"):
                resume_envelope = self.continuity_recovery.resume_envelope(
                    continuity_scope.get("conversation_id"),
                    continuity_scope.get("key_id"),
                )
            return self.continuity_handoff.start(
                key_id=continuity_scope["key_id"],
                client_bucket=continuity_scope["client_bucket"],
                project_key=continuity_scope["project_key"],
                conversation_id=continuity_scope.get("conversation_id"),
                token_budget=continuity_scope.get("token_budget"),
                resume=resume_envelope,
                resume_last_seq=resume.get("last_seq"),
            )
        except Exception:  # noqa: BLE001 - continuity never breaks chat
            return None

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

        self._audit_store("store.open")
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
            self._audit_store("store.close", outcome="failed")
            self.state_store = None

    def _audit_store(self, action: str, outcome: str = "ok") -> None:
        """
        Best-effort ``store.open``/``store.close`` audit event. Never
        raises and never changes the init path.
        """
        from app.services import event_log as event_log_module

        try:
            event_log_module.event_log().emit(
                action,
                actor="system",
                target="state_store",
                outcome=outcome,
            )
        except Exception:  # noqa: BLE001 - audit must never break startup
            pass

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

    def chat(self, message: str, task: str | None = None, correlation_id: str | None = None, continuity_scope: dict | None = None, **generation_kwargs: Any) -> dict:
        """
        Send a message through the highest-priority available provider,
        failing over intelligently across models and providers.

        A correlation id is generated per request (or echoed from the
        caller) and carried on the result dict so the API layer can emit
        it as a response header and the request logger can tag log
        records. It is never persisted.

        ``continuity_scope`` (resolved from the request headers by the
        API layer) opts this request into P9c continuity: the envelope is
        injected, failover passes the switch caps, and the successful
        turn is committed.
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

        decision_result = None
        if self.decision_engine.enabled:
            decision_result = self.decision_engine.decide(providers, task=task)

        turn = self.begin_continuity_turn(continuity_scope)

        result = self.chat_service.chat_across(
            candidates,
            message,
            max_retries=settings.max_retries,
            turn=turn,
            **generation_kwargs,
        )

        result["correlation_id"] = cid

        self.request_logger.chat(result)

        if settings.telemetry_enabled:
            self._record_telemetry(result)

        if settings.health_feedback_enabled:
            self._record_feedback(result)

        self._record_actual_decision(
            correlation_id=cid,
            requested_model=None,
            routed_task=task,
            routed=True,
            candidates=candidates,
            provider=result.get("provider") or "",
            model=result.get("model") or "",
            attempts=result.get("attempts"),
            outcome="succeeded" if result.get("success") else "failed",
            decision_result=decision_result,
        )

        return result

    async def achat(
        self,
        message: str,
        task: str | None = None,
        correlation_id: str | None = None,
        continuity_scope: dict | None = None,
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

        decision_result = None
        if self.decision_engine.enabled:
            decision_result = self.decision_engine.decide(providers, task=task)

        turn = self.begin_continuity_turn(continuity_scope)

        result = await self.async_chat_service.achat_across(
            candidates,
            message,
            max_retries=settings.max_retries,
            turn=turn,
            **generation_kwargs,
        )

        result["correlation_id"] = cid

        self.request_logger.chat(result)

        if settings.telemetry_enabled:
            self._record_telemetry(result)

        if settings.health_feedback_enabled:
            self._record_feedback(result)

        self._record_actual_decision(
            correlation_id=cid,
            requested_model=None,
            routed_task=task,
            routed=True,
            candidates=candidates,
            provider=result.get("provider") or "",
            model=result.get("model") or "",
            attempts=result.get("attempts"),
            outcome="succeeded" if result.get("success") else "failed",
            decision_result=decision_result,
        )

        return result

    def _record_actual_decision(
        self,
        *,
        correlation_id: str,
        requested_model: str | None,
        routed_task: str | None,
        routed: bool,
        candidates,
        provider: str,
        model: str,
        attempts,
        outcome: str,
        decision_result=None,
    ) -> None:
        """
        Record the decision the legacy /chat path actually made via the
        shared decision_record implementation, so /chat produces the same
        actual-decision truth surface as /v1. Never raises and never
        changes routing: this is observability only.
        """
        try:
            record_actual_decision(
                self.decision_record_store,
                correlation_id=correlation_id,
                requested_model=requested_model,
                routed_task=routed_task,
                routed=routed,
                candidates=candidates,
                provider=provider,
                model=model,
                attempts=attempts,
                outcome=outcome,
                decision_result=decision_result,
            )
        except Exception:  # noqa: BLE001 - decision truth must never break chat
            _logger.warning(
                "actual-decision recording failed for %s; continuing",
                correlation_id,
            )

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
