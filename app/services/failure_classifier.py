from enum import Enum

from app.providers.exceptions import (
    ProviderHTTPError,
    ProviderTimeout,
)


class FailureKind(str, Enum):
    """
    Classification of a failed provider/model attempt.
    """

    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    QUOTA_EXHAUSTED = "quota_exhausted"
    INVALID_REQUEST = "invalid_request"
    SERVER_ERROR = "server_error"
    AUTH_ERROR = "auth_error"
    UNKNOWN = "unknown"


RETRYABLE = {
    FailureKind.TIMEOUT,
    FailureKind.RATE_LIMIT,
    FailureKind.SERVER_ERROR,
    FailureKind.UNKNOWN,
}

PROVIDER_LEVEL = {
    FailureKind.AUTH_ERROR,
    FailureKind.QUOTA_EXHAUSTED,
}

_QUOTA_MARKERS = (
    "quota",
    "insufficient_quota",
    "billing",
    "exceeded your current quota",
)


def classify(exc) -> FailureKind:
    """
    Classify an exception into a FailureKind.
    """

    if isinstance(exc, ProviderTimeout):
        return FailureKind.TIMEOUT

    if isinstance(exc, ProviderHTTPError):
        code = exc.status_code
        message = (exc.message or "").lower()

        if code == 429:
            return FailureKind.RATE_LIMIT

        if code == 402 or any(marker in message for marker in _QUOTA_MARKERS):
            return FailureKind.QUOTA_EXHAUSTED

        if code in (401, 403):
            return FailureKind.AUTH_ERROR

        if code in (400, 404, 405, 415, 422):
            return FailureKind.INVALID_REQUEST

        if code >= 500:
            return FailureKind.SERVER_ERROR

        return FailureKind.UNKNOWN

    return FailureKind.UNKNOWN
