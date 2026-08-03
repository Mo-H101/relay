from datetime import UTC, datetime

from fastapi import APIRouter

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
            "generated_at": datetime.now(UTC).isoformat(),
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