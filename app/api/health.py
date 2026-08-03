from fastapi import APIRouter

from app.core.relay import relay
from app.services.health_checker import HEALTHY, UNAVAILABLE

router = APIRouter()


def _aggregate_status(providers):
    """
    Collapse a list of per-provider reports into a single aggregate
    status: "unavailable", "degraded", or "ok".
    """
    if not providers:
        return "unavailable"

    statuses = {provider["status"] for provider in providers}

    if UNAVAILABLE in statuses:
        return "unavailable"

    if statuses == {HEALTHY}:
        return "ok"

    return "degraded"


@router.get("/health")
def health():
    """
    Public liveness probe. Returns only an aggregate status and never
    exposes provider names, models, credentials, or diagnostics.
    """
    report = relay.health(deep=False)
    return {"status": _aggregate_status(report.get("providers", []))}


@router.get("/health/deep")
def health_deep():
    return relay.health(deep=True)
