from dataclasses import dataclass

from app.services.failure_classifier import FailureKind

PROVIDER = "provider"
MODEL = "model"

DEGRADED = "degraded"
UNAVAILABLE = "unavailable"
CLEAR = "clear"
NONE = "none"

MODEL_SERVER_ERROR_THRESHOLD = 1
PROVIDER_SERVER_ERROR_THRESHOLD = 3
MODEL_TIMEOUT_DEGRADED_THRESHOLD = 2
MODEL_TIMEOUT_UNAVAILABLE_THRESHOLD = 5
MODEL_INVALID_REQUEST_UNAVAILABLE_THRESHOLD = 3
MODEL_UNKNOWN_DEGRADED_THRESHOLD = 3

DEFAULT_DEGRADED_TTL_SECONDS = 60
DEFAULT_UNAVAILABLE_TTL_SECONDS = 900


@dataclass(frozen=True)
class FeedbackAction:
    """
    Effect of a recorded chat failure.

    scope is "provider", "model", or "none"; effect is "degraded",
    "unavailable", "clear", or "none".
    """

    scope: str = NONE
    effect: str = NONE
    ttl_seconds: int = 0

    @classmethod
    def clear(cls) -> "FeedbackAction":
        return cls(scope=MODEL, effect=CLEAR)


def action_for(
    failure_type: str,
    model_failures: int = 0,
    provider_failures: int = 0,
    degraded_ttl: int = DEFAULT_DEGRADED_TTL_SECONDS,
    unavailable_ttl: int = DEFAULT_UNAVAILABLE_TTL_SECONDS,
    model_server_error_threshold: int = MODEL_SERVER_ERROR_THRESHOLD,
    provider_server_error_threshold: int = PROVIDER_SERVER_ERROR_THRESHOLD,
    model_timeout_degraded_threshold: int = MODEL_TIMEOUT_DEGRADED_THRESHOLD,
    model_timeout_unavailable_threshold: int = MODEL_TIMEOUT_UNAVAILABLE_THRESHOLD,
    model_invalid_request_unavailable_threshold: int = (
        MODEL_INVALID_REQUEST_UNAVAILABLE_THRESHOLD
    ),
    model_unknown_degraded_threshold: int = MODEL_UNKNOWN_DEGRADED_THRESHOLD,
) -> FeedbackAction:
    """
    Map a FailureKind (or its value string) to a feedback action.

    Thresholds guard against marking providers/models unhealthy from a
    single failed request. Every threshold and TTL is overridable so the
    policy can be tuned from configuration.
    """

    if isinstance(failure_type, FailureKind):
        failure_type = failure_type.value

    if failure_type == FailureKind.AUTH_ERROR.value:
        return FeedbackAction(PROVIDER, UNAVAILABLE, unavailable_ttl)

    if failure_type == FailureKind.QUOTA_EXHAUSTED.value:
        return FeedbackAction(PROVIDER, UNAVAILABLE, unavailable_ttl)

    if failure_type == FailureKind.RATE_LIMIT.value:
        return FeedbackAction(PROVIDER, DEGRADED, degraded_ttl)

    if failure_type == FailureKind.SERVER_ERROR.value:
        if provider_failures >= provider_server_error_threshold:
            return FeedbackAction(PROVIDER, DEGRADED, degraded_ttl)
        if model_failures >= model_server_error_threshold:
            return FeedbackAction(MODEL, DEGRADED, degraded_ttl)
        return FeedbackAction()

    if failure_type == FailureKind.TIMEOUT.value:
        if model_failures >= model_timeout_unavailable_threshold:
            return FeedbackAction(MODEL, UNAVAILABLE, unavailable_ttl)
        if model_failures >= model_timeout_degraded_threshold:
            return FeedbackAction(MODEL, DEGRADED, degraded_ttl)
        return FeedbackAction()

    if failure_type == FailureKind.INVALID_REQUEST.value:
        if model_failures >= model_invalid_request_unavailable_threshold:
            return FeedbackAction(MODEL, UNAVAILABLE, unavailable_ttl)
        return FeedbackAction(MODEL, DEGRADED, degraded_ttl)

    if failure_type == FailureKind.UNKNOWN.value:
        if model_failures >= model_unknown_degraded_threshold:
            return FeedbackAction(MODEL, DEGRADED, degraded_ttl)
        return FeedbackAction()

    return FeedbackAction()
