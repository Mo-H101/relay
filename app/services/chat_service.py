from typing import List, Tuple, Any, Generator
import time

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


class ChatService:
    """
    Sends prompts using the appropriate provider client, failing over
    intelligently across models and providers.
    """

    def __init__(self) -> None:
        self.registry = ClientRegistry()

    def _try_once(
        self,
        provider: Provider,
        model: str,
        message: str,
        attempt_no: int,
        **generation_kwargs: Any,
    ) -> Tuple[Attempt, str | None, FailureKind | None]:
        """
        Perform a single chat attempt.

        Returns (attempt, response, kind): response is None on failure,
        and kind is the classified FailureKind (None on success).
        """

        start = time.perf_counter()

        try:
            client = self.registry.get(provider.identity())
            response = client.chat(
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

    def chat_across(
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
        attempts (no response key).

        When a ``TurnContext`` is supplied (P9c continuity), the envelope
        is injected once up front, each failover to a new candidate passes
        through the switch caps, and a successful turn is committed.
        """

        attempts: List[Attempt] = []
        errors: List[str] = []
        skip_providers = set()
        max_retries = max(0, int(max_retries))
        start_wall = time.perf_counter()

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

            while retry_no <= max_retries:

                if budget_exhausted(time.perf_counter() - start_wall):
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

                attempt, response, kind = self._try_once(
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

                if kind in PROVIDER_LEVEL:
                    skip_providers.add(provider.name)
                    break

                if kind not in RETRYABLE or retry_no >= max_retries:
                    break

                wait = retry_wait_seconds(
                    attempt,
                    retry_no,
                    time.perf_counter() - start_wall,
                )

                if wait > 0:
                    time.sleep(wait)

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

    def _try_once_messages(
        self,
        provider: Provider,
        model: str,
        payload: dict,
        attempt_no: int,
    ) -> Tuple[Attempt, dict | None, FailureKind | None]:
        """
        Perform a single chat attempt with a full message payload.

        Returns (attempt, response, kind): response is the provider's
        parsed response dict on success, None on failure, and kind is the
        classified FailureKind (None on success).
        """

        start = time.perf_counter()

        try:
            client = self.registry.get(provider.identity())
            response = client.chat_messages(
                provider=provider,
                payload=payload,
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

    def chat_across_messages(
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

        Returns the same result shape as chat_across, with response set
        to the provider's parsed response dict on success.
        """

        attempts: List[Attempt] = []
        errors: List[str] = []
        skip_providers = set()
        max_retries = max(0, int(max_retries))
        start_wall = time.perf_counter()

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

            while retry_no <= max_retries:

                if budget_exhausted(time.perf_counter() - start_wall):
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

                attempt, response, kind = self._try_once_messages(
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

                if kind in PROVIDER_LEVEL:
                    skip_providers.add(provider.name)
                    break

                if kind not in RETRYABLE or retry_no >= max_retries:
                    break

                wait = retry_wait_seconds(
                    attempt,
                    retry_no,
                    time.perf_counter() - start_wall,
                )

                if wait > 0:
                    time.sleep(wait)

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

    def _try_stream_once(
        self,
        provider: Provider,
        model: str,
        message: str,
        **generation_kwargs: Any,
    ) -> Generator[str, None, None]:
        """
        Attempt to start a streaming chat with a single provider/model.

        Yields content delta strings.
        Raises an exception if the stream fails to start or fails mid-stream.
        """
        client = self.registry.get(provider.identity())
        # The provider's chat_stream method returns a generator
        yield from client.chat_stream(
            provider=provider,
            model=model,
            message=message,
            **generation_kwargs,
        )

    def chat_across_stream(
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
        reported by the caller while consuming the generator.

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
                stream_gen = self._try_stream_once(
                    provider,
                    model,
                    message,
                    **generation_kwargs,
                )
                # Pull the first chunk to verify the stream actually started.
                first_chunk = next(stream_gen)
            except StopIteration:
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

            def gen() -> Generator[str, None, None]:
                yield first_chunk
                yield from stream_gen

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

    def _try_stream_once_messages(
        self,
        provider: Provider,
        model: str,
        payload: dict,
    ) -> Generator[dict, None, None]:
        """
        Attempt to start a streaming chat with a single provider/model
        using a full message payload.

        Yields parsed chunk dicts.
        Raises an exception if the stream fails to start or fails mid-stream.
        """
        client = self.registry.get(provider.identity())
        yield from client.chat_stream_messages(
            provider=provider,
            payload=payload,
        )

    def chat_across_stream_messages(
        self,
        candidates: List[Tuple[Provider, str]],
        payload: dict,
        max_retries: int = 1,
        *,
        turn=None,
    ) -> dict:
        """
        Try candidates in order to start a streaming chat with a full
        message payload.

        Returns a result dict with success, provider, model, stream_gen
        (yielding parsed chunk dicts, None when no candidate started),
        error (when success is False), and attempts: per-attempt failure
        records for candidates that never produced a first chunk. Once a
        stream starts, its final outcome is reported by the caller while
        consuming the generator.

        Streaming start failures are not retried: the candidate list is
        the failover path. No exception is raised; the caller decides the
        HTTP mapping.
        """
        attempts: List[dict] = []
        errors: List[str] = []
        skip_providers = set()

        if turn is not None:
            payload = turn.inject_payload(payload)

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
                stream_gen = self._try_stream_once_messages(
                    provider,
                    model,
                    payload,
                )
                first_chunk = next(stream_gen)
            except StopIteration:
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

            def gen() -> Generator[dict, None, None]:
                yield first_chunk
                yield from stream_gen

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

    def chat(
        self,
        provider: Provider,
        message: str,
    ) -> Tuple[str, str]:
        """
        Send a message through a single provider, failing over to the
        next model when a model is unavailable.
        """

        candidates = [
            (provider, model)
            for model in provider.models
            if is_chat_testable(model)
        ]

        result = self.chat_across(candidates, message)

        if not result["success"]:
            raise ProviderError(
                result.get("error", "Provider request failed.")
            )

        return result["model"], result["response"]
