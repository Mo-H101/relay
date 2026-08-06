from fastapi import APIRouter, HTTPException, Response, Request
from typing import Any
import time

from app.core.config import settings
from app.core.relay import relay
from app.models.chat import ChatRequest, ChatResponse
from app.services.continuity_headers import (
    ContinuityHeaderError,
    resolve_scope,
)
from app.services.routing import TASK_CATEGORIES
from app.services.task_classifier import classify_task
from app.services.metrics import relay_metrics
from app.services.ops_store import ops_store

router = APIRouter()

_CORRELATION_HEADER = "X-Relay-Correlation-Id"
_CONVERSATION_HEADER = "X-Relay-Conversation-Id"
_RESUME_TOKEN_HEADER = "X-Relay-Resume-Token"


def _correlation_headers(correlation_id: str) -> dict:
    """
    Header payload for error responses carrying the correlation id.
    """
    return {_CORRELATION_HEADER: correlation_id}


def _continuity_headers(result: dict) -> dict:
    """
    Header payload echoing the conversation id when continuity is active.
    P9d also hands back the one-time resume token for the turn via
    ``X-Relay-Resume-Token`` when one was issued. Values are already
    server-issued opaque ids/tokens; they are never echoed in error
    bodies.
    """
    continuity = result.get("continuity")
    if not isinstance(continuity, dict):
        return {}
    headers = {}
    conversation_id = continuity.get("conversation_id")
    if conversation_id:
        headers[_CONVERSATION_HEADER] = conversation_id
    resume_token = continuity.get("resume_token")
    if resume_token:
        headers[_RESUME_TOKEN_HEADER] = resume_token
    return headers


def _resolve_continuity_scope(http_request: Request) -> dict | None:
    """
    Resolve the continuity scope from the request headers. A malformed
    header value becomes a generic 400 (the offending value is never
    surfaced). P9d: when continuity is active the presented resume token
    is validated and the decision is attached to the scope.
    """
    try:
        scope = resolve_scope(http_request)
    except ContinuityHeaderError:
        raise HTTPException(
            status_code=400,
            detail="Invalid relay continuity header.",
        )
    if scope:
        relay.validate_resume(scope)
    return scope


def _fallback_flag(result: dict) -> bool:
    """
    True when the completed chat did not stay on the first candidate.
    """
    if result.get("fallback_reason"):
        return True
    return len(result.get("attempts") or []) > 1


def _record_chat(
    endpoint: str,
    stream: bool,
    result: dict,
    latency_ms: float,
    gen_kwargs: dict[str, Any] | None,
) -> None:
    relay_metrics.record_chat(
        endpoint,
        stream,
        result,
        latency_ms,
        gen_kwargs=gen_kwargs,
    )
    ops_store.record_chat(
        endpoint=endpoint,
        stream=stream,
        provider=result.get("provider") or "",
        model=result.get("model") or "",
        success=bool(result.get("success")),
        fallback=_fallback_flag(result),
        latency_ms=latency_ms,
        attempts=len(result.get("attempts") or []),
    )


def _resolve_task(request: ChatRequest) -> str | None:
    """
    Resolve the routing task for a chat request.

    With task classification disabled (default), the explicit task field
    is used verbatim (invalid values stay invalid). With it enabled, a
    valid explicit task overrides classification, and a missing or
    invalid explicit task is classified from the message with a
    deterministic fallback to "general".
    """
    if request.task is not None:
        explicit = request.task.strip().lower()

        if explicit in TASK_CATEGORIES:
            return explicit

    return classify_task(
        request.message,
        settings.task_classification_threshold,
    )


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, http_request: Request, response: Response):

    if settings.task_classification_enabled:
        task = _resolve_task(request)
    elif request.task is not None:
        task = request.task.strip().lower()

        if task not in TASK_CATEGORIES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown task '{request.task}'. "
                    f"Valid tasks: {', '.join(TASK_CATEGORIES)}"
                ),
            )
    else:
        task = None

    # Extract generation parameters
    generation_kwargs: dict[str, Any] = {}
    if request.temperature is not None:
        generation_kwargs["temperature"] = request.temperature
    if request.top_p is not None:
        generation_kwargs["top_p"] = request.top_p
    if request.max_tokens is not None:
        generation_kwargs["max_tokens"] = request.max_tokens
    if request.stop is not None:
        generation_kwargs["stop"] = request.stop
    if request.frequency_penalty is not None:
        generation_kwargs["frequency_penalty"] = request.frequency_penalty
    if request.presence_penalty is not None:
        generation_kwargs["presence_penalty"] = request.presence_penalty
    if request.seed is not None:
        generation_kwargs["seed"] = request.seed

    start = time.perf_counter()

    continuity_scope = _resolve_continuity_scope(http_request)

    result = await relay.achat(
        request.message,
        task=task,
        continuity_scope=continuity_scope,
        **generation_kwargs,
    )

    latency_ms = (time.perf_counter() - start) * 1000

    _record_chat("/chat", False, result, latency_ms, generation_kwargs)

    correlation_id = result.get("correlation_id", "")
    response.headers[_CORRELATION_HEADER] = correlation_id

    for header, value in _continuity_headers(result).items():
        response.headers[header] = value

    if not result.get("success"):
        error_headers = _correlation_headers(correlation_id)
        error_headers.update(_continuity_headers(result))
        if "provider" in result:
            raise HTTPException(
                status_code=502,
                detail=result.get("error", "Provider request failed."),
                headers=error_headers,
            )
        raise HTTPException(
            status_code=503,
            detail=result.get("error", "No provider available."),
            headers=error_headers,
        )

    if not result.get("response"):
        raise HTTPException(
            status_code=502,
            detail="Provider returned empty content.",
            headers=_correlation_headers(correlation_id),
        )

    return ChatResponse(
        provider=result["provider"],
        model=result["model"],
        response=result["response"],
    )
