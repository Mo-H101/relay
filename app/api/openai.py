"""
OpenAI-compatible API endpoints for Relay.
"""
from fastapi import APIRouter, Response, Body
from fastapi.responses import JSONResponse, StreamingResponse
from app.core.config import settings
from app.core.relay import relay
from app.services.async_chat_service import AsyncChatService
from app.services.correlation import new_correlation_id
from app.services.failure_classifier import classify
from app.services.metrics import relay_metrics
from app.services.ops_store import ops_store
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


def _correlation_headers(correlation_id: str) -> dict:
    """
    Header payload for error responses carrying the correlation id.
    """
    return {_CORRELATION_HEADER: correlation_id}


def _openai_error_response(
    status_code: int,
    message: str,
    error_type: str = "invalid_request_error",
    code: str | None = None,
    correlation_id: str = "",
) -> JSONResponse:
    """
    Build an OpenAI-shaped error body ({"error": {...}}) rather than the
    FastAPI {"detail": ...} shape, so SDK clients parse errors directly.
    """
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "code": code,
            }
        },
        headers=_correlation_headers(correlation_id),
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
):
    correlation_id = new_correlation_id()
    response.headers[_CORRELATION_HEADER] = correlation_id

    # 1. tool_choice without tools is invalid on the OpenAI surface.
    if req.tool_choice is not None and not req.tools:
        return _openai_error_response(
            400,
            "When using tool_choice, a non-empty tools list is required.",
            code="invalid_request",
            correlation_id=correlation_id,
        )

    # 2. Filter providers that declare the model
    candidates = [
        (p, req.model) for p in relay.provider_manager.all()
        if req.model in p.models
    ]
    if not candidates:
        return _openai_error_response(
            400,
            f"Model '{req.model}' not available from any provider.",
            code="model_not_found",
            correlation_id=correlation_id,
        )

    # 3. Build the verbatim wire payload from the request.
    payload = req.to_provider_payload()
    gen_kwargs = _generation_kwargs(req)

    # 4. Handle streaming vs non-streaming
    if req.stream:
        payload["stream"] = True

        # Streaming response
        result = await async_chat_svc.achat_across_stream_messages(
            candidates,
            payload,
            max_retries=settings.max_retries,
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
            )

        provider_name = result["provider"]
        stream_gen = result["stream_gen"]

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
            try:
                async for chunk in stream_gen:
                    out = {
                        "id": stream_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": req.model,
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
                    req.model,
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

        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={_CORRELATION_HEADER: correlation_id},
        )

    # Non-streaming path: full message pipeline with verbatim passthrough.
    start_time = time.perf_counter()

    try:
        result = await async_chat_svc.achat_across_messages(
            candidates,
            payload,
            max_retries=settings.max_retries,
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
    for p in relay.provider_manager.all():
        for m in p.models:
            models.append(ModelObject(id=m, owned_by=p.name))
    return ModelList(data=models)
