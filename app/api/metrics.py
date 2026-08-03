"""
Prometheus metrics endpoint.
"""

from fastapi import APIRouter, Response

from app.core.relay import relay
from app.services.metrics import relay_metrics

router = APIRouter()

_MEDIA_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("/metrics")
def metrics():
    """
    Prometheus text exposition of Relay's operational metrics.

    Exposes counters, gauges, and histograms only. Never includes
    prompts, responses, API keys, proxy credentials, or user input.
    Protected by the global API-key dependency when RELAY_API_KEY is set.
    """
    relay_metrics.persistence_enabled.set(
        1 if relay.state_store is not None else 0
    )

    return Response(
        content=relay_metrics.render(),
        media_type=_MEDIA_TYPE,
    )
