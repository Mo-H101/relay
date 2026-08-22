"""
Read-only diagnostics snapshot of Relay's operational state.

DiagnosticsService composes data from the existing intelligence stores
without duplicating their logic and without performing any network I/O
or mutating state. It is safe to call from monitoring/debugging paths:
GET /diagnostics never probes providers, never changes routing, and
never persists anything.

Privacy: the snapshot exposes provider/model names, health/telemetry
aggregates, scoring parameters, and persistence status only. It never
includes prompts, responses, user data, or API keys (only booleans like
"has_api_key").
"""

from datetime import datetime, timezone
from typing import Optional

from app.core.config import settings
from app.services.adaptive import AdaptiveWeights
from app.services.explanation import ExplanationService
from app.services.feedback import DEGRADED, UNAVAILABLE
from app.services.metrics import relay_metrics
from app.services.ops_store import ops_store
from app.services.scoring import CandidateScorer

# Bound on actual-decision records surfaced in one diagnostics snapshot.
# The store itself is already bounded (DecisionRecordStore.max_records);
# this keeps the snapshot payload small even at that bound.
_ACTUAL_DECISIONS_LIMIT = 50


class DiagnosticsService:
    """
    Builds a structured, privacy-safe view of Relay state.
    """

    def build_snapshot(self, relay, task: str | None = None) -> dict:
        """
        Compose the full diagnostics snapshot from an active Relay.
        """
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "providers": self._providers(relay),
            "provider_registration": self._provider_registration(relay),
            "learned_health": self._learned_health(relay),
            "telemetry": self._telemetry(relay),
            "operations": self._operations(),
            "scoring": self._scoring(relay, task),
            "adaptive": self._adaptive(relay),
            "quality": self._quality(relay),
            "actual_decisions": self._actual_decisions(relay),
            "persistence": self._persistence(relay),
            "continuity": self._continuity(relay),
        }

    def _provider_registration(self, relay) -> list[dict]:
        """Return safe startup/reload status without exception text."""
        manager = getattr(relay, "provider_manager", None)
        getter = getattr(manager, "registration_status", None)
        if getter is None:
            return []
        return getter()

    def _operations(self) -> dict:
        """
        Rolling operational summary (last OPS_WINDOW_SECONDS) plus
        cumulative auth counters. Metadata only; no payloads.
        """
        stats = ops_store.stats()

        return {
            "window_seconds": stats["window_seconds"],
            "max_events": stats["max_events"],
            "requests": stats["requests"],
            "successes": stats["successes"],
            "failures": stats["failures"],
            "success_rate": stats["success_rate"],
            "failure_rate": stats["failure_rate"],
            "average_latency_ms": stats["average_latency_ms"],
            "p50_latency_ms": stats["p50_latency_ms"],
            "p95_latency_ms": stats["p95_latency_ms"],
            "streaming": stats["streaming"],
            "providers": stats["providers"],
            "endpoints": stats["endpoints"],
            "auth": {
                "failures": relay_metrics.auth_failures.total(),
                "authenticated": relay_metrics.auth_success.total(),
            },
        }

    def _providers(self, relay) -> dict:
        """
        Passive per-provider view from stored snapshots only.
        """
        snapshots = {
            provider.name: relay.health_store.get(provider.name)
            for provider in relay.provider_manager.all()
        }

        entries = []

        for provider in relay.provider_manager.all():
            report = snapshots[provider.name]

            if report is None:
                entries.append(
                    {
                        "name": provider.name,
                        "enabled": provider.enabled,
                        "priority": provider.priority,
                        "requires_api_key": provider.requires_api_key,
                        "has_api_key": provider.has_api_key(),
                        "models": list(provider.models),
                        "status": "not_checked",
                        "connectivity": None,
                        "last_checked": None,
                        "rate_limit_status": None,
                        "healthy_models": [],
                        "degraded_models": [],
                        "unavailable_models": [],
                        "unsupported_models": [],
                    }
                )
                continue

            entries.append(
                {
                    "name": provider.name,
                    "enabled": provider.enabled,
                    "priority": provider.priority,
                    "requires_api_key": provider.requires_api_key,
                    "has_api_key": provider.has_api_key(),
                    "models": list(provider.models),
                    "status": report.status,
                    "connectivity": report.connectivity,
                    "last_checked": report.last_checked,
                    "rate_limit_status": report.rate_limit_status,
                    "healthy_models": list(report.healthy_models),
                    "degraded_models": list(report.degraded_models),
                    "unavailable_models": list(report.unavailable_models),
                    "unsupported_models": list(report.unsupported_models),
                }
            )

        return {"providers": entries}

    def _learned_health(self, relay) -> dict:
        """
        Active learned degradation from chat feedback, via the store's
        own export (single passive read of all providers).
        """
        exported = relay.health_store.export_learned_state()

        entries = []
        degraded_models = 0
        unavailable_models = 0

        for provider, data in sorted(exported.items()):
            marks = data.get("model_marks") or {}
            d_models = [
                model
                for model, statuses in marks.items()
                if DEGRADED in statuses
            ]
            u_models = [
                model
                for model, statuses in marks.items()
                if UNAVAILABLE in statuses
            ]
            degraded_models += len(d_models)
            unavailable_models += len(u_models)

            entries.append(
                {
                    "provider": provider,
                    "status": data.get("provider_status"),
                    "degraded_models": d_models,
                    "unavailable_models": u_models,
                }
            )

        return {
            "providers": entries,
            "summary": {
                "degraded_providers": sum(
                    1 for entry in entries if entry["status"] == DEGRADED
                ),
                "unavailable_providers": sum(
                    1 for entry in entries if entry["status"] == UNAVAILABLE
                ),
                "degraded_models": degraded_models,
                "unavailable_models": unavailable_models,
            },
        }

    def _telemetry(self, relay) -> dict:
        """
        Per-(provider, model) aggregates plus session totals.
        """
        stats_list = relay.telemetry.all()

        entries = []
        total_requests = 0
        total_successes = 0
        total_failures = 0

        for stats in stats_list:
            total_requests += stats.request_count
            total_successes += stats.success_count
            total_failures += stats.failure_count

            entries.append(
                {
                    "provider": stats.provider,
                    "model": stats.model,
                    "request_count": stats.request_count,
                    "success_count": stats.success_count,
                    "failure_count": stats.failure_count,
                    "success_rate": (
                        round(
                            stats.success_count / stats.request_count,
                            4,
                        )
                        if stats.request_count
                        else None
                    ),
                    "average_latency_ms": stats.average_latency_ms,
                    "recent_failure_count": len(stats.recent_failures),
                }
            )

        return {
            "entries": entries,
            "summary": {
                "total_requests": total_requests,
                "total_successes": total_successes,
                "total_failures": total_failures,
                "success_rate": (
                    round(total_successes / total_requests, 4)
                    if total_requests
                    else None
                ),
            },
        }

    def _scoring(self, relay, task: str | None) -> dict:
        """
        Configured scoring parameters and the current decision ranking
        with full explanations, reusing CandidateScorer and
        ExplanationService.
        """
        scorer = CandidateScorer()
        task_normalized = task.strip().lower() if task else None

        providers = relay.provider_manager.ranked()

        if not providers:
            ranked = []
            decision_scores = []
        else:
            ranked = relay.candidate_builder.ranked_candidates(
                providers,
                task=task_normalized,
            )

            engine = getattr(relay, "decision_engine", None)

            if engine is not None and engine.enabled:
                decision_scores = engine.score_pool(
                    providers,
                    task=task_normalized,
                ).ranked
            else:
                decision_scores = []

        return {
            "weights": {
                "priority": scorer.priority_weight,
                "success": scorer.success_weight,
                "latency": scorer.latency_weight,
                "failure": scorer.failure_weight,
                "preference": scorer.preference_weight,
                "task_compatibility": scorer.task_compatibility_weight,
                "adaptive_reliability": scorer.adaptive_reliability_weight,
                "adaptive_latency": scorer.adaptive_latency_weight,
                "quality": scorer.quality_weight,
                "cost": scorer.cost_weight,
            },
            "references": {
                "priority_denominator": scorer.priority_denom,
                "latency_ref_ms": scorer.latency_ref_ms,
                "failure_ref_count": scorer.failure_ref_count,
            },
            "ranking": [
                {
                    "provider": candidate.provider,
                    "model": candidate.model,
                    "rank": candidate.rank,
                    "health_band": candidate.health_band,
                    "health_status": candidate.health_status,
                    "score_breakdown": candidate.breakdown,
                }
                for candidate in ranked
            ],
            "decision": ExplanationService().explain(
                ranked,
                task=task_normalized,
                health_aware=bool(settings.health_aware_routing),
            ),
            "decision_engine": {
                "enabled": bool(
                    getattr(
                        getattr(relay, "decision_engine", None),
                        "enabled",
                        False,
                    )
                ),
                "stats": (
                    relay.decision_engine.stats()
                    if getattr(relay, "decision_engine", None) is not None
                    else {}
                ),
                "scores": [
                    {
                        "provider": score.provider,
                        "model": score.model,
                        "rank": score.rank,
                        "health_band": score.health_band,
                        "health_status": score.health_status,
                        "fitness": score.fitness,
                        "total": score.total,
                        "confidence": score.confidence,
                        "reason": score.reason,
                        "signals": [
                            {
                                "key": signal.key,
                                "normalized": signal.normalized,
                                "weight": signal.weight,
                                "contribution": signal.contribution,
                                "enabled": signal.enabled,
                                "confidence": signal.confidence,
                            }
                            for signal in score.signals
                        ],
                    }
                    for score in decision_scores
                ],
            },
        }

    def _adaptive(self, relay) -> dict:
        """
        Adaptive routing state (Phase 7C): tuning parameters plus the
        per-candidate learned EWMA state. Metadata only; bounded so a
        large telemetry store cannot blow up the snapshot.
        """
        adaptive = AdaptiveWeights(telemetry=relay.telemetry)

        states = adaptive.states()

        return {
            "config": adaptive.config(),
            "state": [
                {
                    "provider": state.provider,
                    "model": state.model,
                    "request_count": state.request_count,
                    "confidence": state.confidence,
                    "ewma_success": state.ewma_success,
                    "ewma_latency_ms": state.ewma_latency_ms,
                    "latency_trend_ms": state.latency_trend_ms,
                    "reliability_delta": state.reliability_delta,
                    "latency_delta": state.latency_delta,
                }
                for state in states[:50]
            ],
        }

    def _actual_decisions(self, relay) -> dict:
        """
        Recent actual routing decision records (Phase 7/8 orchestration
        truth layer): the executed provider/model per request, the
        ordered candidate pool, ranks, and per-attempt metadata. Read-only
        and bounded (only the most recent records are surfaced). Metadata
        only: no prompts, responses, content, or credentials — the same
        surface the /decision/explain/actual endpoint serves.
        """
        store = getattr(relay, "decision_record_store", None)

        if store is None:
            return {
                "available": False,
                "limit": _ACTUAL_DECISIONS_LIMIT,
                "records": [],
            }

        return {
            "available": True,
            "limit": _ACTUAL_DECISIONS_LIMIT,
            "max_records": store.max_records,
            "records": store.snapshot(limit=_ACTUAL_DECISIONS_LIMIT),
        }

    def _quality(self, relay) -> dict:
        """
        Quality feedback aggregates (Phase 7D): per-(provider, model)
        sample counts, positive/negative tallies, EWMA score, and
        confidence. Metadata only; raw feedback content is never stored
        or exposed. Bounded by the store's retention limit.
        """
        store = getattr(relay, "quality_store", None)

        if store is None:
            return {
                "enabled": False,
                "config": {
                    "min_samples": 0,
                    "learning_rate": 0.0,
                    "retention_limit": 0,
                },
                "pairs": [],
                "summary": {
                    "pairs": 0,
                    "total_ratings": 0,
                    "confident_pairs": 0,
                },
            }

        stats = store.stats()

        return {
            "enabled": bool(settings.quality_feedback_enabled),
            "config": {
                "min_samples": stats["min_samples"],
                "learning_rate": stats["learning_rate"],
                "retention_limit": stats["retention_limit"],
            },
            "pairs": store.aggregates(),
            "summary": {
                "pairs": stats["pairs"],
                "total_ratings": stats["total_ratings"],
                "confident_pairs": stats["confident_pairs"],
            },
        }

    def _persistence(self, relay) -> dict:
        """
        Persistence status from the StateStore/StateFlusher.
        """
        enabled = bool(settings.persistence_enabled)
        state_store = relay.state_store
        state_flusher = relay.state_flusher

        if state_store is None:
            return {
                "enabled": enabled,
                "available": False,
                "path": str(settings.persistence_path) if enabled else None,
                "schema_version": None,
                "storage_status": (
                    "disabled" if not enabled else "unavailable"
                ),
                "learned_memory": {
                    "learned_providers": 0,
                    "telemetry_pairs": 0,
                    "quality_pairs": 0,
                    "decision_stats_rows": 0,
                },
                "retention_days": settings.persistence_retention_days,
                "last_load_at": None,
                "last_flush_at": None,
                "load_count": 0,
                "flush_count": 0,
                "load_errors": [],
                "flush_errors": [],
                "initialization_error": getattr(
                    relay, "persistence_init_error", None
                ),
            }

        store_stats = state_store.stats()
        flush_stats = (
            state_flusher.flush_stats() if state_flusher is not None else {}
        )

        try:
            memory_counts = state_store.memory_counts()
        except Exception:
            memory_counts = {
                "learned_providers": 0,
                "telemetry_pairs": 0,
                "quality_pairs": 0,
                "decision_stats_rows": 0,
            }

        load_errors = store_stats["load_errors"]
        flush_errors = flush_stats.get("flush_errors", [])

        return {
            "enabled": enabled,
            "available": True,
            "path": store_stats["path"],
            "schema_version": store_stats.get("schema_version"),
            "storage_status": (
                "ok"
                if not load_errors and not flush_errors
                else "degraded"
            ),
            "learned_memory": memory_counts,
            "retention_days": settings.persistence_retention_days,
            "last_load_at": store_stats["last_load_at"],
            "last_flush_at": flush_stats.get("last_flush_at"),
            "load_count": store_stats["load_count"],
            "flush_count": flush_stats.get("flush_count", 0),
            "load_errors": load_errors,
            "flush_errors": flush_errors,
            "initialization_error": None,
        }

    def _continuity(self, relay) -> dict:
        """
        Phase 10A: bounded continuity counts for the /diagnostics
        surface. Counts only — no prompts, responses, tokens, summaries,
        or row content.  When continuity is disabled returns
        ``{"enabled": false}``; when enabled but the store is unavailable
        the existing best-effort zero counts apply.
        """
        if not settings.continuity_enabled:
            return {"enabled": False}

        store = relay.conversation_store
        if store is None:
            return {"enabled": True, "available": False}

        counts = store.counts()
        return {
            "enabled": True,
            "conversations": counts.get("conversations", 0),
            "active": counts.get("active", 0),
            "archived": counts.get("archived", 0),
            "turns": counts.get("turns", 0),
            "summaries": counts.get("summaries", 0),
            "compactions": counts.get("compactions", 0),
            "projects": counts.get("projects", 0),
            "replays": counts.get("replays", 0),
        }
