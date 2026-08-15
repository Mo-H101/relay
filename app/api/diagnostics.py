from fastapi import APIRouter

from app.core.relay import relay
from app.services.diagnostics import DiagnosticsService

router = APIRouter()


@router.get("/diagnostics")
def diagnostics(task: str | None = None):
    """
    Read-only operational snapshot: provider states, learned health,
    telemetry summaries, scoring/ranking information, recent actual
    routing decisions, and persistence status. Never exposes prompts,
    responses, API keys, or user data and never triggers provider probes.
    """
    return DiagnosticsService().build_snapshot(relay, task=task)
