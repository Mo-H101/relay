"""
OpenAI-compatible API endpoints for Relay.
"""
from fastapi import APIRouter, Response, Body, Request
from fastapi.responses import JSONResponse, StreamingResponse
from app.core.config import settings
from app.core.relay import relay
from app.services.async_chat_service import AsyncChatService
from app.services.continuity_headers import (
    ContinuityHeaderError,
    resolve_scope,
)
from app.services.correlation import new_correlation_id
from app.services.failure_classifier import classify
from app.services.metrics import relay_metrics
from app.services.ops_store import ops_store
from app.services.routing import TASK_CATEGORIES
from app.services.task_classifier import classify_task
from app.schemas.openai import (
    OpenAIChatCompletionRequest,
    ModelObject,
    ModelList,
)
import uuid
import time
import json

router = APIRouter()
async_chat_svc = AsyncChatService()

_CORRELATION_HEADER = "X-Relay-Correlation-Id"
_CONVERSATION_HEADER = "X-Relay-Conversation-Id"
_RESUME_TOKEN_HEADER = "X-Relay-Resume-Token"

# Relay-facing virtual model names: they always route through Relay's
# own candidate machinery instead of naming an upstream model.
_VIRTUAL_MODELS = frozenset({"auto", "default", "relay"})


def _message_text(message) -> str:
    """
    Extract the plain-text form of a chat message for task
    classification: a string content verbatim, a multimodal part list as
    the joined text parts, and nothing else.
    """
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.text for part in content if getattr(part, "text", None)
        )
    return ""


def _last_user_message_text(messages) -> str:
    """
    Return the text of the newest user message (the natural analogue of
    the legacy /chat free-text message), or "" when none exists.
    """
    for message in reversed(messages):
        if message.role == "user":
            return _message_text(message)
    return ""


def _resolve_candidates(relay, providers, requested_model, messages):
    """
    Resolve the request's model field into an ordered candidate list.

    Returns ``(candidates, task, routed)``: ``candidates`` is the ordered
    (provider, model) list, ``task`` the routing task that was applied
    (None when none), and ``routed`` True when the request went through
    Relay's candidate machinery rather than an explicit upstream model.

    An omitted model or a virtual name ("auto", "default", "relay")
    routes through Relay's candidate machinery, optionally narrowed by
    free-text task classification when enabled. A task name routes with
    that task. Any other value is an explicit upstream model id matched
    literally against every provider, preserving the original verbatim
    passthrough behavior.
    """
    requested = (requested_model or "").strip()
    lowered = requested.lower()

    if not requested or lowered in _VIRTUAL_MODELS:
        task = None
        if settings.task_classification_enabled:
            task = classify_task(
                _last_user_message_text(messages),
                settings.task_classification_threshold,
            )
        return (
            relay.candidate_builder.build(providers, task=task),
            task,
            True,
        )

    if lowered in TASK_CATEGORIES:
        return (
            relay.candidate_builder.build(providers, task=lowered),
            lowered,
            True,
        )

    return [
        (provider, requested)
        for provider in providers
        if requested in provider.models
    ], None, False


def _correlation_headers(correlation_id: str) -> dict:
    """
    Header payload for error responses carrying the correlation id.
    """
    return {_CORRELATION_HEADER: correlation_id}


def _resolve_continuity_scope(
    http_request: Request, correlation_id: str = ""
) -> dict | None:
    """
    Resolve the continuity scope from the request headers. A malformed
    header value becomes a generic 400 (the offending value is never
    surfaced). P9d: when continuity is active the presented resume token
    is validated and the decision is attached to the scope.
    """
    try:
        scope = resolve_scope(http_request)
    except ContinuityHeaderError:
        return _openai_error_response(
            400,
            "Invalid relay continuity header.",
            code="invalid_request",
            correlation_id=correlation_id,
        )
    if scope is not None:
        relay.validate_resume(scope)
    return scope


def _continuity_headers(result: dict) -> dict:
    """
    Header payload echoing the conversation id when continuity is active.
    P9d also hands back the one-time resume token for the turn via
    ``X-Relay-Resume-Token`` when one was issued.
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


def _continuity_events(result: dict) -> list[dict]:
    """
    The additive ``relay:*`` continuity events carried on a stream result.
    """
    continuity = result.get("continuity")
    if not isinstance(continuity, dict):
        return []
    return [dict(ev) for ev in continuity.get("events") or []]


def _sse_continuity_events(result: dict) -> str:
    """
    Render the continuity events as additive SSE lines (``event:`` +
    ``data:``) emitted before the provider stream, so clients can observe
    conversation creation and model handoffs without the payload being
    affected.
    """
    lines: list[str] = []
    for ev in _continuity_events(result):
        ev_type = ev.get("type", "relay:event")
        lines.append(f"event: {ev_type}")
        lines.append(f"data: {json.dumps(ev)}")
        lines.append("")
    return "\n".join(lines)


def _openai_error_response(
    status_code: int,
    message: str,
    error_type: str = "invalid_request_error",
    code: str | None = None,
    correlation_id: str = "",
    extra_headers: dict | None = None,
) -> JSONResponse:
    """
    Build an OpenAI-shaped error body ({"error": {...}}) rather than the
    FastAPI {"detail": ...} shape, so SDK clients parse errors directly.
    """
    headers = _correlation_headers(correlation_id)
    headers.update(extra_headers or {})
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "code": code,
            }
        },
        headers=headers,
    )


def _record_telemetry_and_health(provider: str, model: str, success: bool, latency_ms: int, failure_type: str = None):
    """Record telemetry and health feedback for a completed request."""
    if settings.telemetry_enabled:
        relay.telemetry.record_attempt(
            provider,
            model,
            success=success,
            latency_ms=latency_ms,
            failure_type=failure_type,
        )
    if settings.health_feedback_enabled:
        if success:
            relay.health_store.record_success(provider, model)
        else:
            relay.health_store.record_failure(provider, model, failure_type or "unknown")


def _record_attempts_telemetry_and_health(result: dict) -> None:
    """
    Record telemetry and health feedback for every recorded attempt,
    mirroring the /chat pipeline (Relay._record_telemetry and
    Relay._record_feedback). Failed attempts feed real failure signals
    and the winning attempt records its success, so a request recovered
    by failover does not punish the provider that served it.
    """
    for attempt in result.get("attempts") or []:
        provider = attempt.get("provider")
        model = attempt.get("model")

        if not provider or not model:
            continue

        _record_telemetry_and_health(
            provider,
            model,
            bool(attempt.get("success")),
            attempt.get("latency_ms") or 0,
            attempt.get("failure_type"),
        )


def _used_kwargs(gen_kwargs: dict) -> dict:
    """Drop explicitly-unset generation parameters before recording."""
    return {key: value for key, value in gen_kwargs.items() if value is not None}


def _fallback_flag(result: dict) -> bool:
    """True when the chat did not stay on the first candidate."""
    if result.get("fallback_reason"):
        return True
    return len(result.get("attempts") or []) > 1


def _record_chat(
    endpoint: str,
    stream: bool,
    result: dict,
    latency_ms: float,
    gen_kwargs: dict | None,
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


def _record_stream_chat(
    result: dict,
    latency_ms: float,
    gen_kwargs: dict,
    success: bool | None = None,
) -> None:
    """Record a stream chat where attempts hold only failed starts."""
    metrics_result = dict(result)
    if success is not None:
        metrics_result["success"] = success
    if metrics_result.get("fallback_reason") is None and result.get("attempts"):
        metrics_result["fallback_reason"] = "stream_failover"
    relay_metrics.record_chat(
        "/v1/chat/completions",
        True,
        metrics_result,
        latency_ms,
        gen_kwargs=gen_kwargs,
    )
    ops_store.record_chat(
        endpoint="/v1/chat/completions",
        stream=True,
        provider=result.get("provider") or "",
        model=result.get("model") or "",
        success=bool(metrics_result.get("success")),
        fallback=bool(result.get("attempts")),
        latency_ms=latency_ms,
        attempts=len(result.get("attempts") or []),
    )


def _generation_kwargs(req: OpenAIChatCompletionRequest) -> dict:
    """Generation parameters recorded as chat metrics for /v1 requests."""
    kwargs: dict = {
        "temperature": req.temperature,
        "top_p": req.top_p,
        "max_tokens": req.max_tokens,
        "stop": req.stop,
        "frequency_penalty": req.frequency_penalty,
        "presence_penalty": req.presence_penalty,
        "seed": req.seed,
        "user": req.user,
        "tool_choice": req.tool_choice,
        "stream_options": req.stream_options,
    }
    if req.tools is not None:
        kwargs["tools"] = req.tools
    return kwargs


@router.post("/v1/chat/completions")
async def openai_chat_completion(
    req: OpenAIChatCompletionRequest = Body(...),
    response: Response = None,
    http_request: Request = None,
):
    correlation_id = new_correlation_id()
    response.headers[_CORRELATION_HEADER] = correlation_id

    # Continuity scope is resolved once for the whole request; a malformed
    # header value becomes a generic 400 (never echoing the value).
    continuity_scope = None
    if http_request is not None:
        continuity_scope = _resolve_continuity_scope(
            http_request, correlation_id=correlation_id
        )
        if isinstance(continuity_scope, JSONResponse):
            return continuity_scope

    # 1. tool_choice without tools is invalid on the OpenAI surface.
    if req.tool_choice is not None and not req.tools:
        return _openai_error_response(
            400,
            "When using tool_choice, a non-empty tools list is required.",
            code="invalid_request",
            correlation_id=correlation_id,
        )

    # 2. Resolve the Relay-facing model interface: virtual names, task
    #    names, and omitted models route through task/candidate
    #    machinery; literal upstream model ids keep the passthrough
    #    behavior.
    providers = relay.provider_manager.all()
    candidates, routed_task, routed = _resolve_candidates(
        relay, providers, req.model, req.messages
    )
    if not candidates:
        if req.model:
            detail = f"Model '{req.model}' not available from any provider."
        else:
            detail = "No provider available for automatic routing."
        return _openai_error_response(
            400,
            detail,
            code="model_not_found",
            correlation_id=correlation_id,
        )

    # Decision observability (parity with /chat): when the request was
    # routed through Relay's candidate machinery and the decision engine
    # is enabled, record a decision pass over the same pool. Ordering is
    # unchanged; the engine is a scoring/observability layer.
    if routed and relay.decision_engine.enabled:
        relay.decision_engine.decide(providers, task=routed_task)

    # 3. Build the verbatim wire payload from the request.
    payload = req.to_provider_payload()
    gen_kwargs = _generation_kwargs(req)

    turn = relay.begin_continuity_turn(continuity_scope)

    # 4. Handle streaming vs non-streaming
    if req.stream:
        payload["stream"] = True

        # Streaming response
        result = await async_chat_svc.achat_across_stream_messages(
            candidates,
            payload,
            max_retries=settings.max_retries,
            turn=turn,
        )

        # Record telemetry/health for candidates that failed to start.
        _record_attempts_telemetry_and_health(result)

        if not result["success"]:
            _record_stream_chat(
                result,
                sum(
                    attempt.get("latency_ms") or 0
                    for attempt in result.get("attempts") or []
                ),
                _used_kwargs(gen_kwargs),
            )
            return _openai_error_response(
                502,
                result["error"],
                error_type="server_error",
                code="provider_error",
                correlation_id=correlation_id,
                extra_headers=_continuity_headers(result),
            )

        provider_name = result["provider"]
        stream_model = result.get("model") or req.model
        stream_gen = result["stream_gen"]
        continuity_sse = _sse_continuity_events(result)

        # Stable identifiers across the whole stream, plus passthrough of
        # provider deltas, finish_reason, tool_call deltas, and usage.
        stream_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        full_response = ""
        start_time = time.perf_counter()
        success = False
        failure_type = None

        async def stream_generator():
            nonlocal full_response, success, failure_type
            if continuity_sse:
                yield continuity_sse + "\n\n"
            try:
                async for chunk in stream_gen:
                    out = {
                        "id": stream_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": stream_model,
                    }
                    if "choices" in chunk:
                        out["choices"] = chunk["choices"]
                    if "usage" in chunk:
                        out["usage"] = chunk["usage"]
                    yield f"data: {json.dumps(out)}\n\n"
                success = True
                yield "data: [DONE]\n\n"
            except Exception as exc:
                # Stream failed mid-stream
                failure_type = classify(exc).value
                error_chunk = {
                    "error": {
                        "message": str(exc),
                        "type": "stream_error",
                        "code": "stream_error"
                    }
                }
                yield f"data: {json.dumps(error_chunk)}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                # Record telemetry and health
                latency_ms = int((time.perf_counter() - start_time) * 1000)
                _record_telemetry_and_health(
                    provider_name,
                    stream_model,
                    success,
                    latency_ms,
                    failure_type,
                )
                _record_stream_chat(
                    result,
                    latency_ms,
                    _used_kwargs(gen_kwargs),
                    success=success,
                )

        headers = {_CORRELATION_HEADER: correlation_id}
        headers.update(_continuity_headers(result))

        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers=headers,
        )

    # Non-streaming path: full message pipeline with verbatim passthrough.
    start_time = time.perf_counter()

    try:
        result = await async_chat_svc.achat_across_messages(
            candidates,
            payload,
            max_retries=settings.max_retries,
            turn=turn,
        )
    except Exception as exc:
        return _openai_error_response(
            500,
            str(exc),
            error_type="server_error",
            code="relay_error",
            correlation_id=correlation_id,
        )

    latency_ms = (time.perf_counter() - start_time) * 1000

    # Record telemetry and health per attempt: failed candidates feed
    # real failure signals and the winning attempt records its success.
    _record_attempts_telemetry_and_health(result)

    for header, value in _continuity_headers(result).items():
        response.headers[header] = value

    if not result["success"]:
        _record_chat(
            "/v1/chat/completions",
            False,
            result,
            latency_ms,
            _used_kwargs(gen_kwargs),
        )
        return _openai_error_response(
            502,
            result.get("error", "Provider error"),
            error_type="server_error",
            code="provider_error",
            correlation_id=correlation_id,
            extra_headers=_continuity_headers(result),
        )

    _record_chat(
        "/v1/chat/completions",
        False,
        result,
        latency_ms,
        _used_kwargs(gen_kwargs),
    )

    resp = result["response"]
    resp.setdefault("id", f"chatcmpl-{uuid.uuid4().hex}")
    resp.setdefault("object", "chat.completion")
    resp.setdefault("created", int(time.time()))
    return resp


@router.get("/v1/models")
def openai_models():
    models: list[ModelObject] = []
    providers = relay.provider_manager.all()
    # Relay-facing names first: virtual routing names and the task
    # categories, owned by Relay so clients can discover automatic
    # routing without knowing any upstream model id. Only advertised when
    # at least one provider is registered so the empty-provider contract
    # is preserved.
    if providers:
        for name in sorted(_VIRTUAL_MODELS):
            models.append(ModelObject(id=name, owned_by="relay"))
        for name in TASK_CATEGORIES:
            models.append(ModelObject(id=name, owned_by="relay"))
    # Then the raw upstream catalog, unchanged.
    for p in providers:
        for m in p.models:
            models.append(ModelObject(id=m, owned_by=p.name))
    return ModelList(data=models)
