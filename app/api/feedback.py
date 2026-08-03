"""
User quality feedback endpoint (Phase 7D).

POST /feedback accepts metadata-only quality ratings for a
(provider, model) pair. The schema is strict (extra="forbid"), so any
payload carrying prompt/message/response/content fields is rejected with
HTTP 422 and never reaches the store; a defensive guard rejects those
field names with HTTP 400 even if the schema ever changes.

Authentication is enforced by the global require_api_key dependency
declared on the FastAPI app, so this route is never reachable without a
valid key when RELAY_API_KEY is set.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException

from app.core.relay import relay
from app.models.feedback import FeedbackRequest

router = APIRouter()

# Field names that must never be accepted, regardless of schema evolution.
_FORBIDDEN_SCHEMA_FIELDS = ("prompt", "message", "response", "content")


def _validate_pair(relay, provider: str, model: str) -> Optional[str]:
    """
    Best-effort provider/model validation (Phase 7D audit hardening).

    Rejects a pair only when it can be positively confirmed as unknown:
    the provider is registered and carries a non-empty model list that
    does not contain the model. Unknown providers and unverifiable states
    (no provider manager, no registered provider, empty model list) are
    accepted, so the store's metadata-only recording never rejects
    legitimate feedback.
    """
    manager = getattr(relay, "provider_manager", None)

    if manager is None:
        return None

    registered = manager.get(provider)

    if registered is None:
        return None

    if not registered.models:
        return None

    if model in registered.models:
        return None

    return f"Unknown model {model!r} for provider {provider!r}."


@router.post("/feedback", status_code=202)
def submit_feedback(payload: FeedbackRequest) -> dict:
    data = payload.model_dump()

    for field_name in _FORBIDDEN_SCHEMA_FIELDS:
        if field_name in data:
            raise HTTPException(
                status_code=400,
                detail="Unsupported field.",
            )

    invalid = _validate_pair(relay, payload.provider, payload.model)

    if invalid is not None:
        raise HTTPException(status_code=400, detail=invalid)

    stored = relay.quality_store.record(
        provider=payload.provider,
        model=payload.model,
        rating=payload.rating,
        category=(
            payload.category.value
            if payload.category is not None
            else None
        ),
        correlation_id=payload.correlation_id,
    )

    return {
        "stored": stored,
        "provider": payload.provider,
        "model": payload.model,
        "rating": payload.rating,
    }
