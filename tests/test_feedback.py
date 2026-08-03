from app.services.failure_classifier import FailureKind
from app.services.feedback import (
    DEGRADED,
    DEFAULT_DEGRADED_TTL_SECONDS,
    DEFAULT_UNAVAILABLE_TTL_SECONDS,
    MODEL,
    MODEL_TIMEOUT_UNAVAILABLE_THRESHOLD,
    MODEL_UNKNOWN_DEGRADED_THRESHOLD,
    NONE,
    PROVIDER,
    PROVIDER_SERVER_ERROR_THRESHOLD,
    UNAVAILABLE,
    FeedbackAction,
    action_for,
)


class TestFeedbackPolicy:
    def test_auth_error_provider_unavailable_long_ttl(self):
        action = action_for(FailureKind.AUTH_ERROR.value)

        assert (action.scope, action.effect) == (PROVIDER, UNAVAILABLE)
        assert action.ttl_seconds == DEFAULT_UNAVAILABLE_TTL_SECONDS

    def test_quota_exhausted_provider_unavailable_long_ttl(self):
        action = action_for(FailureKind.QUOTA_EXHAUSTED.value)

        assert (action.scope, action.effect) == (PROVIDER, UNAVAILABLE)
        assert action.ttl_seconds == DEFAULT_UNAVAILABLE_TTL_SECONDS

    def test_rate_limit_provider_degraded_short_ttl(self):
        action = action_for(FailureKind.RATE_LIMIT.value)

        assert (action.scope, action.effect) == (PROVIDER, DEGRADED)
        assert action.ttl_seconds == DEFAULT_DEGRADED_TTL_SECONDS

    def test_server_error_model_degraded_initially(self):
        action = action_for(
            FailureKind.SERVER_ERROR.value,
            model_failures=1,
            provider_failures=1,
        )

        assert (action.scope, action.effect) == (MODEL, DEGRADED)
        assert action.ttl_seconds == DEFAULT_DEGRADED_TTL_SECONDS

    def test_server_error_provider_degraded_after_threshold(self):
        action = action_for(
            FailureKind.SERVER_ERROR.value,
            model_failures=1,
            provider_failures=PROVIDER_SERVER_ERROR_THRESHOLD,
        )

        assert (action.scope, action.effect) == (PROVIDER, DEGRADED)

    def test_timeout_ignored_below_threshold(self):
        action = action_for(FailureKind.TIMEOUT.value, model_failures=1)

        assert action.scope == NONE

    def test_timeout_model_degraded_after_threshold(self):
        action = action_for(FailureKind.TIMEOUT.value, model_failures=2)

        assert (action.scope, action.effect) == (MODEL, DEGRADED)
        assert action.ttl_seconds == DEFAULT_DEGRADED_TTL_SECONDS

    def test_timeout_model_unavailable_at_higher_threshold(self):
        action = action_for(
            FailureKind.TIMEOUT.value,
            model_failures=MODEL_TIMEOUT_UNAVAILABLE_THRESHOLD,
        )

        assert (action.scope, action.effect) == (MODEL, UNAVAILABLE)
        assert action.ttl_seconds == DEFAULT_UNAVAILABLE_TTL_SECONDS

    def test_invalid_request_model_degraded(self):
        action = action_for(
            FailureKind.INVALID_REQUEST.value,
            model_failures=1,
        )

        assert (action.scope, action.effect) == (MODEL, DEGRADED)

    def test_invalid_request_model_unavailable_at_threshold(self):
        action = action_for(
            FailureKind.INVALID_REQUEST.value,
            model_failures=3,
        )

        assert (action.scope, action.effect) == (MODEL, UNAVAILABLE)

    def test_unknown_ignored_below_threshold(self):
        action = action_for(
            FailureKind.UNKNOWN.value,
            model_failures=MODEL_UNKNOWN_DEGRADED_THRESHOLD - 1,
        )

        assert action.scope == NONE

    def test_unknown_model_degraded_after_threshold(self):
        action = action_for(
            FailureKind.UNKNOWN.value,
            model_failures=MODEL_UNKNOWN_DEGRADED_THRESHOLD,
        )

        assert (action.scope, action.effect) == (MODEL, DEGRADED)

    def test_unrecognized_kind_is_noop(self):
        action = action_for("bogus", model_failures=10)

        assert action.scope == NONE

    def test_accepts_enum_directly(self):
        action = action_for(FailureKind.AUTH_ERROR)

        assert action.effect == UNAVAILABLE

    def test_clear_action(self):
        action = FeedbackAction.clear()

        assert action.scope == MODEL
        assert action.effect == "clear"


class TestFeedbackTtlSelection:
    def test_unavailable_uses_longer_ttl_than_degraded(self):
        auth = action_for(FailureKind.AUTH_ERROR.value)
        rate = action_for(FailureKind.RATE_LIMIT.value)

        assert auth.ttl_seconds == DEFAULT_UNAVAILABLE_TTL_SECONDS
        assert rate.ttl_seconds == DEFAULT_DEGRADED_TTL_SECONDS
        assert auth.ttl_seconds > rate.ttl_seconds

    def test_ttl_override_respected(self):
        action = action_for(
            FailureKind.RATE_LIMIT.value,
            degraded_ttl=30,
            unavailable_ttl=120,
        )

        assert action.ttl_seconds == 30
