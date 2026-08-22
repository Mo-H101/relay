"""
Pure, deterministic decision helpers shared by the sync ``ChatService`` and
the async ``AsyncChatService``.

Everything in this module is loop-independent: no I/O, no sleeping, and no
wall-clock reads. Both services import the same helpers so candidate
failover, retry, and request-timeout-budget decisions cannot drift between
the sync and async stacks.
"""
from dataclasses import dataclass, field
from typing import Any, List, Tuple

from app.core.config import settings


# Bound total upstream work for a request even when the candidate catalog or
# configured retry count is unexpectedly large.
MAX_ATTEMPTS_PER_REQUEST = 32


@dataclass
class Attempt:
    """
    Record of a single provider/model attempt.
    """

    provider: str
    model: str
    attempt: int
    latency_ms: int
    success: bool
    failure_type: str = ""
    reason: str = ""
    retry_after: float | None = None
    _exc: Any = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "attempt": self.attempt,
            "latency_ms": self.latency_ms,
            "success": self.success,
            "failure_type": self.failure_type or None,
            "reason": self.reason,
        }


def retry_wait_seconds(
    attempt: Attempt,
    retry_no: int,
    elapsed_seconds: float,
) -> float:
    """
    Seconds to wait before the next retry of the same candidate.

    Prefers a provider-supplied Retry-After when honoring is enabled,
    otherwise exponential backoff (base * 2^retry_no). Both are capped
    by their configured maximum, and the result never exceeds the
    remaining request-timeout budget (0 = no budget).

    All knobs default to preserving current behavior: honoring and
    backoff are off, so retries are immediate unless configured.
    """
    budget = settings.request_timeout_budget_seconds
    remaining = (
        float("inf")
        if budget <= 0
        else max(0.0, budget - elapsed_seconds)
    )

    wait = 0.0

    if settings.retry_honor_retry_after and attempt.retry_after is not None:
        wait = min(
            attempt.retry_after,
            settings.retry_after_max_seconds,
        )
    elif settings.retry_backoff_base_seconds > 0:
        wait = min(
            settings.retry_backoff_base_seconds * (2 ** retry_no),
            settings.retry_backoff_max_seconds,
        )

    return min(wait, remaining)


def empty_content(content: str | None) -> bool:
    """
    True when a chat response carries no visible assistant content.

    A provider may return HTTP 200 with a null or blank ``content`` field
    (for example a reasoning model that exhausts its token budget on
    ``reasoning_content``). Such a response is useless to the caller, so
    the sync and async services treat it as a failed attempt and fail
    over to the next candidate.
    """
    return content is None or not content.strip()


def budget_exhausted(elapsed_seconds: float) -> bool:
    """
    True when a configured request-timeout budget has been consumed.

    A budget of 0 (default) means no deadline. The caller supplies the
    elapsed wall-clock time so each stack can use its own monotonic clock
    (``time.perf_counter`` for sync, the event loop's clock for async).
    """
    budget = settings.request_timeout_budget_seconds

    if budget <= 0:
        return False

    return elapsed_seconds >= budget


def attempt_budget_exhausted(attempts) -> bool:
    """Return whether another upstream attempt may start."""
    return len(attempts) >= MAX_ATTEMPTS_PER_REQUEST


def fallback_reason(
    provider_name: str,
    model: str,
    candidates: List[Tuple[Any, str]],
    attempts: List[Attempt],
) -> str | None:
    """
    Report why the winning candidate differs from the first candidate.

    Returns the reason of the most recent failed attempt, or None when
    the first candidate won.
    """
    first_provider, first_model = (
        candidates[0][0].name,
        candidates[0][1],
    )

    if provider_name == first_provider and model == first_model:
        return None

    return next(
        (
            other.reason
            for other in reversed(attempts[:-1])
            if not other.success
        ),
        None,
    )
