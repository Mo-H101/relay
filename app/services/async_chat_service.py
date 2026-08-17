"""
Async mirror of the sync ``ChatService``.

Implements the same candidate failover, retry, and request-timeout-budget
algorithm over the async provider clients (``achat`` / ``achat_stream``),
sharing the pure decision helpers in ``chat_policy`` so the two stacks
cannot drift.

Waits use ``asyncio.sleep`` so they are cancellable; ``CancelledError``
is never swallowed (it is a ``BaseException``, so the ``except Exception``
handlers around a single attempt let it propagate) and travels out of the
service, letting the API layer react to client disconnects.
"""
import asyncio
import time
from typing import Any, AsyncIterator, List, Tuple

from app.providers.base import Provider
from app.providers.exceptions import ProviderError
from app.services.chat_policy import (
    Attempt,
    budget_exhausted,
    empty_content,
    fallback_reason,
    retry_wait_seconds,
)
from app.services.client_registry import ClientRegistry
from app.services.capabilities import is_chat_testable
from app.services.failure_classifier import (
    PROVIDER_LEVEL,
    RETRYABLE,
    FailureKind,
    classify,
)
from app.services.metrics import relay_metrics


def _loop_elapsed(start_wall: float) -> float:
    """
    Wall-clock seconds since ``start_wall`` using the running loop's
    monotonic clock (the async counterpart of ``time.perf_counter``).
    """
    return asyncio.get_running_loop().time() - start_wall


class AsyncChatService:
    """
    Sends prompts using the async provider clients, failing over
    intelligently across models and providers.
    """

    def __init__(self) -> None:
        self.registry = ClientRegistry()

    async def _atry_once(
        self,
        provider: Provider,
        model: str,
        message: str,
        attempt_no: int,
        **generation_kwargs: Any,
    ) -> Tuple[Attempt, str | None, FailureKind | None]:
        """
        Perform a single async chat attempt.

        Returns (attempt, response, kind): response is None on failure,
        and kind is the classified FailureKind (None on success).
        """

        start = time.perf_counter()

        try:
            client = self.registry.get(provider.identity())
            response = await client.achat(
                provider=provider,
                model=model,
                message=message,
                **generation_kwargs,
            )
        except Exception as exc:
            latency = int((time.perf_counter() - start) * 1000)
            kind = classify(exc)

            return (
                Attempt(
                    provider=provider.name,
                    model=model,
                    attempt=attempt_no,
                    latency_ms=latency,
                    success=False,
                    failure_type=kind.value,
                    reason=str(exc),
                    retry_after=getattr(exc, "retry_after", None),
                    _exc=exc,
                ),
                None,
                kind,
            )

        latency = int((time.perf_counter() - start) * 1000)

        if empty_content(response):
            return (
                Attempt(
                    provider=provider.name,
                    model=model,
                    attempt=attempt_no,
                    latency_ms=latency,
                    success=False,
                    failure_type=FailureKind.EMPTY_RESPONSE.value,
                    reason="Provider returned empty content.",
                ),
                None,
                FailureKind.EMPTY_RESPONSE,
            )

        return (
            Attempt(
                provider=provider.name,
                model=model,
                attempt=attempt_no,
                latency_ms=latency,
                success=True,
            ),
            response,
            None,
        )

    async def achat_across(
        self,
        candidates: List[Tuple[Provider, str]],
        message: str,
        max_retries: int = 1,
        *,
        turn=None,
        **generation_kwargs: Any,
    ) -> dict:
        """
        Try candidates in order: current model, retry if appropriate,
        next model, next provider.

        Returns a result dict with success, provider, model, response,
        latency_ms, fallback_reason, and per-attempt records. On failure,
        the result includes success=False, error, fallback_reason, and
        attempts (no response key). Mirrors ``ChatService.chat_across``.

        When a ``TurnContext`` is supplied (P9c continuity), the envelope
        is injected once up front, each failover to a new candidate passes
        through the switch caps, and a successful turn is committed.
        """

        attempts: List[Attempt] = []
        errors: List[str] = []
        skip_providers = set()
        max_retries = max(0, int(max_retries))
        start_wall = asyncio.get_running_loop().time()

        original_message = message

        if turn is not None:
            message = turn.inject_message(message)

        last_key = None
        stop_failover = False

        for provider, model in candidates:

            if stop_failover:
                break

            if provider.name in skip_providers:
                continue

            retry_no = 0
            overflow_retried = False

            while retry_no <= max_retries:

                if budget_exhausted(_loop_elapsed(start_wall)):
                    break

                if (
                    turn is not None
                    and last_key is not None
                    and last_key != (provider.name, model)
                ):
                    decision = turn.switch(
                        from_provider=last_key[0],
                        from_model=last_key[1],
                        to_provider=provider.name,
                        to_model=model,
                        reason="failover",
                    )
                    if not decision.get("allowed", True):
                        stop_failover = True
                        break

                attempt, response, kind = await self._atry_once(
                    provider,
                    model,
                    message,
                    retry_no,
                    **generation_kwargs,
                )

                last_key = (provider.name, model)
                attempts.append(attempt)

                if attempt.success:
                    result = {
                        "success": True,
                        "provider": provider.name,
                        "model": model,
                        "response": response,
                        "latency_ms": attempt.latency_ms,
                        "fallback_reason": fallback_reason(
                            provider.name,
                            model,
                            candidates,
                            attempts,
                        ),
                        "attempts": [
                            record.to_dict() for record in attempts
                        ],
                    }
                    if turn is not None:
                        turn.finish(
                            provider=provider.name,
                            model=model,
                            latency_ms=attempt.latency_ms,
                        )
                        turn.attach(result)
                    return result

                errors.append(f"{model} ({provider.name}): {attempt.reason}")

                # Phase 10B: overflow-retry with a more aggressively
                # compacted envelope, independent of the normal retry
                # budget.  Exactly one overflow retry per candidate.
                if (
                    not overflow_retried
                    and turn is not None
                    and attempt._exc is not None
                    and turn.context_manager is not None
                    and turn.context_manager.should_retry_compacted(
                        attempt._exc
                    )
                ):
                    overflow_retried = True
                    turn.rebuild_for_overflow()
                    message = turn.inject_message(original_message)
                    relay_metrics.continuity_overflow_retries.inc()
                    continue

                if kind in PROVIDER_LEVEL:
                    skip_providers.add(provider.name)
                    break

                if kind not in RETRYABLE or retry_no >= max_retries:
                    break

                wait = retry_wait_seconds(
                    attempt,
                    retry_no,
                    _loop_elapsed(start_wall),
                )

                if wait > 0:
                    await asyncio.sleep(wait)

                retry_no += 1

        first_provider = candidates[0][0].name if candidates else ""
        first_model = candidates[0][1] if candidates else ""

        result = {
            "success": False,
            "provider": first_provider,
            "model": first_model,
            "error": "; ".join(errors) if errors else "No candidates to try.",
            "fallback_reason": None,
            "attempts": [record.to_dict() for record in attempts],
        }

        if turn is not None:
            turn.attach(result)

        return result

    async def _atry_stream_once(
        self,
        provider: Provider,
        model: str,
        message: str,
        **generation_kwargs: Any,
    ) -> AsyncIterator[str]:
        """
        Attempt to start an async streaming chat with a single
        provider/model.

        Yields content delta strings.
        Raises an exception if the stream fails to start or fails
        mid-stream.
        """
        client = self.registry.get(provider.identity())
        async for chunk in client.achat_stream(
            provider=provider,
            model=model,
            message=message,
            **generation_kwargs,
        ):
            yield chunk

    async def achat_across_stream(
        self,
        candidates: List[Tuple[Provider, str]],
        message: str,
        max_retries: int = 1,
        *,
        turn=None,
        **generation_kwargs: Any,
    ) -> dict:
        """
        Try candidates in order to start a streaming chat.

        Returns a result dict with success, provider, model, stream_gen
        (None when no candidate started), error (when success is False),
        and attempts: per-attempt failure records for candidates that never
        produced a first chunk. Once a stream starts, its final outcome is
        reported by the caller while consuming the async generator.

        Streaming start failures are not retried: the candidate list is the
        failover path. No exception is raised; the caller decides the HTTP
        mapping.
        """
        attempts: List[dict] = []
        errors: List[str] = []
        skip_providers = set()

        if turn is not None:
            message = turn.inject_message(message)

        last_key = None

        for provider, model in candidates:

            if provider.name in skip_providers:
                continue

            if (
                turn is not None
                and last_key is not None
                and last_key != (provider.name, model)
            ):
                decision = turn.switch(
                    from_provider=last_key[0],
                    from_model=last_key[1],
                    to_provider=provider.name,
                    to_model=model,
                    reason="failover",
                )
                if not decision.get("allowed", True):
                    break

            start = time.perf_counter()

            try:
                stream_gen = self._atry_stream_once(
                    provider,
                    model,
                    message,
                    **generation_kwargs,
                )
                # Pull the first chunk to verify the stream actually started.
                first_chunk = await stream_gen.__anext__()
            except StopAsyncIteration:
                attempts.append({
                    "provider": provider.name,
                    "model": model,
                    "latency_ms": int((time.perf_counter() - start) * 1000),
                    "failure_type": "empty_stream",
                    "reason": "stream ended before producing content",
                })
                errors.append(f"{model} ({provider.name}): empty stream")
                last_key = (provider.name, model)
                continue
            except Exception as exc:
                kind = classify(exc)
                attempts.append({
                    "provider": provider.name,
                    "model": model,
                    "latency_ms": int((time.perf_counter() - start) * 1000),
                    "failure_type": kind.value,
                    "reason": str(exc),
                })
                errors.append(f"{model} ({provider.name}): {exc}")

                if kind in PROVIDER_LEVEL:
                    skip_providers.add(provider.name)
                last_key = (provider.name, model)
                continue

            async def gen() -> AsyncIterator[str]:
                yield first_chunk
                async for chunk in stream_gen:
                    yield chunk

            result = {
                "success": True,
                "provider": provider.name,
                "model": model,
                "stream_gen": gen(),
                "error": None,
                "attempts": attempts,
            }

            if turn is not None:
                turn.finish(
                    provider=provider.name,
                    model=model,
                    outcome="ok",
                )
                turn.attach(result)

            return result

        first_provider = candidates[0][0].name if candidates else ""
        first_model = candidates[0][1] if candidates else ""

        result = {
            "success": False,
            "provider": first_provider,
            "model": first_model,
            "stream_gen": None,
            "error": (
                "; ".join(errors)
                if errors
                else "No candidate could start a stream."
            ),
            "attempts": attempts,
        }

        if turn is not None:
            turn.attach(result)

        return result

    async def _atry_once_messages(
        self,
        provider: Provider,
        model: str,
        payload: dict,
        attempt_no: int,
    ) -> Tuple[Attempt, dict | None, FailureKind | None]:
        """
        Perform a single async chat attempt with a full message payload.

        Returns (attempt, response, kind): response is the provider's
        parsed response dict on success, None on failure, and kind is the
        classified FailureKind (None on success).
        """

        start = time.perf_counter()

        try:
            client = self.registry.get(provider.identity())
            # The wire payload must always name the candidate being
            # attempted: virtual/task-routed models resolve to a concrete
            # upstream model per candidate, and failover can move across
            # different models on the same provider.
            attempt_payload = dict(payload)
            attempt_payload["model"] = model
            response = await client.achat_messages(
                provider=provider,
                payload=attempt_payload,
            )
        except Exception as exc:
            latency = int((time.perf_counter() - start) * 1000)
            kind = classify(exc)

            return (
                Attempt(
                    provider=provider.name,
                    model=model,
                    attempt=attempt_no,
                    latency_ms=latency,
                    success=False,
                    failure_type=kind.value,
                    reason=str(exc),
                    retry_after=getattr(exc, "retry_after", None),
                    _exc=exc,
                ),
                None,
                kind,
            )

        latency = int((time.perf_counter() - start) * 1000)

        return (
            Attempt(
                provider=provider.name,
                model=model,
                attempt=attempt_no,
                latency_ms=latency,
                success=True,
            ),
            response,
            None,
        )

    async def achat_across_messages(
        self,
        candidates: List[Tuple[Provider, str]],
        payload: dict,
        max_retries: int = 1,
        *,
        turn=None,
    ) -> dict:
        """
        Try candidates in order with a full message payload: current
        model, retry if appropriate, next model, next provider.

        Returns the same result shape as achat_across, with response set
        to the provider's parsed response dict on success.
        """

        attempts: List[Attempt] = []
        errors: List[str] = []
        skip_providers = set()
        max_retries = max(0, int(max_retries))
        start_wall = asyncio.get_running_loop().time()

        original_payload = payload

        if turn is not None:
            payload = turn.inject_payload(payload)

        last_key = None
        stop_failover = False

        for provider, model in candidates:

            if stop_failover:
                break

            if provider.name in skip_providers:
                continue

            retry_no = 0
            overflow_retried = False

            while retry_no <= max_retries:

                if budget_exhausted(_loop_elapsed(start_wall)):
                    break

                if (
                    turn is not None
                    and last_key is not None
                    and last_key != (provider.name, model)
                ):
                    decision = turn.switch(
                        from_provider=last_key[0],
                        from_model=last_key[1],
                        to_provider=provider.name,
                        to_model=model,
                        reason="failover",
                    )
                    if not decision.get("allowed", True):
                        stop_failover = True
                        break

                attempt, response, kind = await self._atry_once_messages(
                    provider,
                    model,
                    payload,
                    retry_no,
                )

                last_key = (provider.name, model)
                attempts.append(attempt)

                if attempt.success:
                    result = {
                        "success": True,
                        "provider": provider.name,
                        "model": model,
                        "response": response,
                        "latency_ms": attempt.latency_ms,
                        "fallback_reason": fallback_reason(
                            provider.name,
                            model,
                            candidates,
                            attempts,
                        ),
                        "attempts": [
                            record.to_dict() for record in attempts
                        ],
                    }
                    if turn is not None:
                        turn.finish(
                            provider=provider.name,
                            model=model,
                            latency_ms=attempt.latency_ms,
                        )
                        turn.attach(result)
                    return result

                errors.append(f"{model} ({provider.name}): {attempt.reason}")

                # Phase 10B: overflow-retry with a more aggressively
                # compacted envelope.
                if (
                    not overflow_retried
                    and turn is not None
                    and attempt._exc is not None
                    and turn.context_manager is not None
                    and turn.context_manager.should_retry_compacted(
                        attempt._exc
                    )
                ):
                    overflow_retried = True
                    turn.rebuild_for_overflow()
                    payload = turn.inject_payload(original_payload)
                    relay_metrics.continuity_overflow_retries.inc()
                    continue

                if kind in PROVIDER_LEVEL:
                    skip_providers.add(provider.name)
                    break

                if kind not in RETRYABLE or retry_no >= max_retries:
                    break

                wait = retry_wait_seconds(
                    attempt,
                    retry_no,
                    _loop_elapsed(start_wall),
                )

                if wait > 0:
                    await asyncio.sleep(wait)

                retry_no += 1

        first_provider = candidates[0][0].name if candidates else ""
        first_model = candidates[0][1] if candidates else ""

        result = {
            "success": False,
            "provider": first_provider,
            "model": first_model,
            "error": "; ".join(errors) if errors else "No candidates to try.",
            "fallback_reason": None,
            "attempts": [record.to_dict() for record in attempts],
        }

        if turn is not None:
            turn.attach(result)

        return result

    async def _atry_stream_once_messages(
        self,
        provider: Provider,
        model: str,
        payload: dict,
    ) -> AsyncIterator[dict]:
        """
        Attempt to start an async streaming chat with a single
        provider/model using a full message payload.

        Yields parsed chunk dicts.
        Raises an exception if the stream fails to start or fails
        mid-stream.
        """
        client = self.registry.get(provider.identity())
        # Same contract as the non-stream path: the payload model must be
        # the concrete candidate model, never a virtual/task name.
        attempt_payload = dict(payload)
        attempt_payload["model"] = model
        async for chunk in client.achat_stream_messages(
            provider=provider,
            payload=attempt_payload,
        ):
            yield chunk

    async def achat_across_stream_messages(
        self,
        candidates: List[Tuple[Provider, str]],
        payload: dict,
        max_retries: int = 1,
        *,
        turn=None,
        on_progress=None,
    ) -> dict:
        """
        Try candidates in order to start a streaming chat with a full
        message payload.

        Returns a result dict with success, provider, model, stream_gen
        (yielding parsed chunk dicts, None when no candidate started),
        error (when success is False), and attempts: per-attempt failure
        records for candidates that never produced a first chunk. Once a
        stream starts, its final outcome is reported by the caller while
        consuming the async generator.

        Streaming start failures are not retried: the candidate list is
        the failover path. No exception is raised; the caller decides the
        HTTP mapping.

        ``on_progress`` is an optional callback invoked as candidates are
        attempted so long-running failovers can be surfaced to the user.
        It receives a dict with stage ("attempt" | "failed" | "started"),
        the 1-based candidate index, total candidate count, provider and
        model names, and (for "failed") the reason. The default path is
        unchanged when it is omitted.
        """
        attempts: List[dict] = []
        errors: List[str] = []
        skip_providers = set()
        total = len(candidates)

        if turn is not None:
            payload = turn.inject_payload(payload)

        last_key = None

        for index, (provider, model) in enumerate(candidates, start=1):

            if provider.name in skip_providers:
                continue

            if on_progress is not None:
                on_progress({
                    "stage": "attempt",
                    "index": index,
                    "total": total,
                    "provider": provider.name,
                    "model": model,
                })

            if (
                turn is not None
                and last_key is not None
                and last_key != (provider.name, model)
            ):
                decision = turn.switch(
                    from_provider=last_key[0],
                    from_model=last_key[1],
                    to_provider=provider.name,
                    to_model=model,
                    reason="failover",
                )
                if not decision.get("allowed", True):
                    break

            start = time.perf_counter()

            try:
                # The shared payload is rebound to the candidate model so
                # each attempt targets the correct endpoint.
                payload["model"] = model
                stream_gen = self._atry_stream_once_messages(
                    provider,
                    model,
                    payload,
                )
                # Pull the first chunk to verify the stream actually started.
                first_chunk = await stream_gen.__anext__()
            except StopAsyncIteration:
                attempts.append({
                    "provider": provider.name,
                    "model": model,
                    "latency_ms": int((time.perf_counter() - start) * 1000),
                    "failure_type": "empty_stream",
                    "reason": "stream ended before producing content",
                })
                errors.append(f"{model} ({provider.name}): empty stream")
                if on_progress is not None:
                    on_progress({
                        "stage": "failed",
                        "index": index,
                        "total": total,
                        "provider": provider.name,
                        "model": model,
                        "reason": "stream ended before producing content",
                    })
                last_key = (provider.name, model)
                continue
            except Exception as exc:
                kind = classify(exc)
                attempts.append({
                    "provider": provider.name,
                    "model": model,
                    "latency_ms": int((time.perf_counter() - start) * 1000),
                    "failure_type": kind.value,
                    "reason": str(exc),
                })
                errors.append(f"{model} ({provider.name}): {exc}")
                if on_progress is not None:
                    on_progress({
                        "stage": "failed",
                        "index": index,
                        "total": total,
                        "provider": provider.name,
                        "model": model,
                        "reason": str(exc),
                    })

                if kind in PROVIDER_LEVEL:
                    skip_providers.add(provider.name)
                last_key = (provider.name, model)
                continue

            if on_progress is not None:
                on_progress({
                    "stage": "started",
                    "index": index,
                    "total": total,
                    "provider": provider.name,
                    "model": model,
                })

            async def gen() -> AsyncIterator[dict]:
                yield first_chunk
                async for chunk in stream_gen:
                    yield chunk

            result = {
                "success": True,
                "provider": provider.name,
                "model": model,
                "stream_gen": gen(),
                "error": None,
                "attempts": attempts,
            }

            if turn is not None:
                turn.finish(
                    provider=provider.name,
                    model=model,
                    outcome="ok",
                )
                turn.attach(result)

            return result

        first_provider = candidates[0][0].name if candidates else ""
        first_model = candidates[0][1] if candidates else ""

        result = {
            "success": False,
            "provider": first_provider,
            "model": first_model,
            "stream_gen": None,
            "error": (
                "; ".join(errors)
                if errors
                else "No candidate could start a stream."
            ),
            "attempts": attempts,
        }

        if turn is not None:
            turn.attach(result)

        return result

    async def achat(
        self,
        provider: Provider,
        message: str,
    ) -> Tuple[str, str]:
        """
        Send a message through a single provider, failing over to the
        next model when a model is unavailable.

        Mirrors ``ChatService.chat``.
        """

        candidates = [
            (provider, model)
            for model in provider.models
            if is_chat_testable(model)
        ]

        result = await self.achat_across(candidates, message)

        if not result["success"]:
            raise ProviderError(
                result.get("error", "Provider request failed.")
            )

        return result["model"], result["response"]
