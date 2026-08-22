from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app.core.config import settings
from app.core.relay import relay
from app.services.explanation import ExplanationService

router = APIRouter()


@router.get("/provider")
def get_provider():
    """
    Returns the provider currently selected by Relay.
    """

    provider = relay.choose_provider()

    if provider is None:
        return {
            "provider": None,
            "message": "No provider available."
        }

    return {
        "name": provider.name,
        "priority": provider.priority,
        "enabled": provider.enabled,
        "models": provider.models,
    }


@router.get("/decision/explain")
def explain_decision(task: str | None = None):
    """
    Explains the most recent routing decision: which provider/model was
    selected, why each candidate ranked where it did, and which signals
    (health, telemetry, task preference) affected the ranking.

    This is the predictive/explanatory surface: it recomputes what Relay
    *would* select for the current pool. For what an actual completed
    request *did* select, use /decision/explain/actual.
    """

    if not settings.decision_explanations_enabled:
        return {
            "enabled": False,
            "message": "Decision explanations are disabled.",
        }

    providers = relay.provider_manager.ranked()

    if not providers:
        return {
            "selected": None,
            "candidates": [],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    task_normalized = task.strip().lower() if task else None

    ranked = relay.candidate_builder.ranked_candidates(
        providers,
        task=task_normalized,
    )

    return ExplanationService().explain(
        ranked,
        task=task_normalized,
        health_aware=bool(settings.health_aware_routing),
    )


@router.get("/decision/explain/actual")
def explain_actual_decision(correlation_id: str | None = None):
    """
    Returns the actual decision record for a completed request: the
    provider/model that really executed, the ordered candidate pool,
    per-attempt metadata, and (when the decision engine was enabled) the
    score-based reason/confidence/signals for the executed candidate.

    Without ``correlation_id`` the most recent actual decision is
    returned. With one, the matching record is returned (404 when none
    exists). This is the "what actually happened" surface, distinct from
    the predictive /decision/explain above. Metadata only.
    """

    if not settings.decision_explanations_enabled:
        return {
            "enabled": False,
            "message": "Decision explanations are disabled.",
        }

    store = getattr(relay, "decision_record_store", None)

    if store is None:
        raise HTTPException(
            status_code=404,
            detail="No actual decisions recorded.",
        )

    if correlation_id:
        record = store.get(correlation_id)

        if record is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "No actual decision recorded for that correlation id."
                ),
            )

        return record.to_dict()

    record = store.most_recent()

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No actual decisions recorded.",
        )

    return record.to_dict()
