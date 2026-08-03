import pytest

from app.core.config import Settings, _valid_float, _valid_int
from app.services.candidate_builder import CandidateBuilder
from app.services.failure_classifier import FailureKind
from app.services.feedback import (
    DEFAULT_DEGRADED_TTL_SECONDS,
    DEFAULT_UNAVAILABLE_TTL_SECONDS,
    MODEL_INVALID_REQUEST_UNAVAILABLE_THRESHOLD,
    MODEL_SERVER_ERROR_THRESHOLD,
    MODEL_TIMEOUT_DEGRADED_THRESHOLD,
    MODEL_TIMEOUT_UNAVAILABLE_THRESHOLD,
    MODEL_UNKNOWN_DEGRADED_THRESHOLD,
    PROVIDER_SERVER_ERROR_THRESHOLD,
)
from app.services.health_checker import HEALTHY, ProviderHealth
from app.services.health_store import HealthStore
from app.services.scoring import CandidateScorer
from app.services.telemetry import TelemetryStore, TelemetryStats

TUNING_ENV_VARS = [
    "SCORING_PRIORITY_WEIGHT",
    "SCORING_SUCCESS_WEIGHT",
    "SCORING_LATENCY_WEIGHT",
    "SCORING_FAILURE_WEIGHT",
    "SCORING_PREFERENCE_WEIGHT",
    "SCORING_PRIORITY_DENOM",
    "SCORING_LATENCY_REF_MS",
    "SCORING_FAILURE_REF_COUNT",
    "SCORING_COST_WEIGHT",
    "DECISION_ENGINE_ENABLED",
    "HEALTH_FEEDBACK_MODEL_SERVER_ERROR_THRESHOLD",
    "HEALTH_FEEDBACK_PROVIDER_SERVER_ERROR_THRESHOLD",
    "HEALTH_FEEDBACK_MODEL_TIMEOUT_DEGRADED_THRESHOLD",
    "HEALTH_FEEDBACK_MODEL_TIMEOUT_UNAVAILABLE_THRESHOLD",
    "HEALTH_FEEDBACK_MODEL_INVALID_REQUEST_UNAVAILABLE_THRESHOLD",
    "HEALTH_FEEDBACK_MODEL_UNKNOWN_DEGRADED_THRESHOLD",
    "HEALTH_FRESHNESS_EXPONENT",
    "TELEMETRY_MAX_FAILURE_HISTORY",
    "ADAPTIVE_MIN_SAMPLES",
    "ADAPTIVE_LEARNING_RATE",
    "ADAPTIVE_LATENCY_WEIGHT",
    "ADAPTIVE_RELIABILITY_WEIGHT",
    "QUALITY_FEEDBACK_MIN_SAMPLES",
    "QUALITY_FEEDBACK_LEARNING_RATE",
    "QUALITY_FEEDBACK_RETENTION_LIMIT",
    "QUALITY_FEEDBACK_WEIGHT",
]


def _clear_tuning_env(monkeypatch):
    for name in TUNING_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class _ScoringConfig:
    def __init__(self, **kwargs):
        self.scoring_priority_weight = 1.0
        self.scoring_success_weight = 1.0
        self.scoring_latency_weight = 1.0
        self.scoring_failure_weight = 1.0
        self.scoring_preference_weight = 1.0
        self.scoring_priority_denom = 10.0
        self.scoring_latency_ref_ms = 250.0
        self.scoring_failure_ref_count = 5
        self.adaptive_routing_enabled = False
        self.adaptive_min_samples = 10
        self.adaptive_learning_rate = 0.1
        self.adaptive_latency_weight = 1.0
        self.adaptive_reliability_weight = 1.0
        self.quality_feedback_enabled = False
        self.quality_feedback_min_samples = 10
        self.quality_feedback_learning_rate = 0.1
        self.quality_feedback_retention_limit = 10000
        self.quality_feedback_weight = 1.0

        for key, value in kwargs.items():
            setattr(self, key, value)


def _stats(average_latency_ms):
    return TelemetryStats(
        provider="p",
        model="m",
        request_count=10,
        success_count=10,
        failure_count=0,
        average_latency_ms=average_latency_ms,
        recent_failures=[],
    )


def make_report(name, status=HEALTHY):
    return ProviderHealth(
        name=name,
        status=status,
        latency_ms=5,
        last_checked="now",
        details="ok",
        connectivity=True,
        rate_limit_status="ok",
        last_successful_request=None,
    )


class TestSettingsDefaults:
    def test_scoring_defaults_match_legacy_constants(self, monkeypatch):
        _clear_tuning_env(monkeypatch)
        cfg = Settings()

        assert cfg.scoring_priority_weight == 1.0
        assert cfg.scoring_success_weight == 1.0
        assert cfg.scoring_latency_weight == 1.0
        assert cfg.scoring_failure_weight == 1.0
        assert cfg.scoring_preference_weight == 1.0
        assert cfg.scoring_priority_denom == 10.0
        assert cfg.scoring_latency_ref_ms == 250.0
        assert cfg.scoring_failure_ref_count == 5

    def test_health_defaults_match_legacy_constants(self, monkeypatch):
        _clear_tuning_env(monkeypatch)
        cfg = Settings()

        assert (
            cfg.health_feedback_model_server_error_threshold
            == MODEL_SERVER_ERROR_THRESHOLD
        )
        assert (
            cfg.health_feedback_provider_server_error_threshold
            == PROVIDER_SERVER_ERROR_THRESHOLD
        )
        assert (
            cfg.health_feedback_model_timeout_degraded_threshold
            == MODEL_TIMEOUT_DEGRADED_THRESHOLD
        )
        assert (
            cfg.health_feedback_model_timeout_unavailable_threshold
            == MODEL_TIMEOUT_UNAVAILABLE_THRESHOLD
        )
        assert (
            cfg.health_feedback_model_invalid_request_unavailable_threshold
            == MODEL_INVALID_REQUEST_UNAVAILABLE_THRESHOLD
        )
        assert (
            cfg.health_feedback_model_unknown_degraded_threshold
            == MODEL_UNKNOWN_DEGRADED_THRESHOLD
        )
        assert cfg.health_degraded_ttl_seconds == DEFAULT_DEGRADED_TTL_SECONDS
        assert cfg.health_unavailable_ttl_seconds == DEFAULT_UNAVAILABLE_TTL_SECONDS

    def test_freshness_and_telemetry_defaults(self, monkeypatch):
        _clear_tuning_env(monkeypatch)
        cfg = Settings()

        assert cfg.health_freshness_exponent == 1.0
        assert cfg.telemetry_max_failure_history == 50

    def test_custom_valid_values_are_accepted(self, monkeypatch):
        monkeypatch.setenv("SCORING_LATENCY_WEIGHT", "2.5")
        monkeypatch.setenv("SCORING_LATENCY_REF_MS", "100")
        monkeypatch.setenv(
            "HEALTH_FEEDBACK_PROVIDER_SERVER_ERROR_THRESHOLD",
            "2",
        )
        monkeypatch.setenv("TELEMETRY_MAX_FAILURE_HISTORY", "10")
        cfg = Settings()

        assert cfg.scoring_latency_weight == 2.5
        assert cfg.scoring_latency_ref_ms == 100.0
        assert cfg.health_feedback_provider_server_error_threshold == 2
        assert cfg.telemetry_max_failure_history == 10


class TestSettingsValidation:
    def test_settings_rejects_non_numeric_weight(self, monkeypatch):
        monkeypatch.setenv("SCORING_LATENCY_WEIGHT", "abc")

        with pytest.raises(ValueError):
            Settings()

    def test_settings_rejects_negative_weight(self, monkeypatch):
        monkeypatch.setenv("SCORING_LATENCY_WEIGHT", "-0.5")

        with pytest.raises(ValueError):
            Settings()

    def test_settings_rejects_zero_denominator(self, monkeypatch):
        monkeypatch.setenv("SCORING_PRIORITY_DENOM", "0")

        with pytest.raises(ValueError):
            Settings()

    def test_settings_rejects_nan_freshness(self, monkeypatch):
        monkeypatch.setenv("HEALTH_FRESHNESS_EXPONENT", "nan")

        with pytest.raises(ValueError):
            Settings()

    def test_valid_float_accepts_numbers(self):
        assert _valid_float("X", "1.0") == 1.0
        assert _valid_float("X", "0") == 0.0

    def test_valid_float_rejects_non_numeric(self):
        with pytest.raises(ValueError):
            _valid_float("X", "abc")

    def test_valid_float_rejects_non_finite(self):
        for value in ("nan", "inf", "-inf"):
            with pytest.raises(ValueError):
                _valid_float("X", value)

    def test_valid_float_rejects_below_minimum(self):
        with pytest.raises(ValueError):
            _valid_float("X", "-1", minimum=0.0)

    def test_valid_float_rejects_zero_when_exclusive(self):
        with pytest.raises(ValueError):
            _valid_float("X", "0", minimum=0.0, exclusive_minimum=True)

    def test_valid_float_rejects_above_maximum(self):
        with pytest.raises(ValueError):
            _valid_float("X", "1.5", maximum=1.0)

    def test_valid_float_accepts_at_maximum(self):
        assert _valid_float("X", "1.0", maximum=1.0) == 1.0

    def test_quality_learning_rate_rejects_above_one(self, monkeypatch):
        monkeypatch.setenv("QUALITY_FEEDBACK_LEARNING_RATE", "1.5")

        with pytest.raises(ValueError):
            Settings()

    def test_quality_learning_rate_accepts_boundary(self, monkeypatch):
        monkeypatch.setenv("QUALITY_FEEDBACK_LEARNING_RATE", "1.0")

        assert Settings().quality_feedback_learning_rate == 1.0

    def test_cost_weight_default_and_custom(self, monkeypatch):
        _clear_tuning_env(monkeypatch)

        assert Settings().scoring_cost_weight == 0.0

        monkeypatch.setenv("SCORING_COST_WEIGHT", "2.5")
        assert Settings().scoring_cost_weight == 2.5

    def test_decision_engine_enabled_default_and_custom(self, monkeypatch):
        _clear_tuning_env(monkeypatch)

        assert Settings().decision_engine_enabled is False

        monkeypatch.setenv("DECISION_ENGINE_ENABLED", "true")
        assert Settings().decision_engine_enabled is True

    def test_valid_int_accepts_integer(self):
        assert _valid_int("X", "5") == 5

    def test_valid_int_rejects_float_string(self):
        with pytest.raises(ValueError):
            _valid_int("X", "5.5")

    def test_valid_int_rejects_below_minimum(self):
        with pytest.raises(ValueError):
            _valid_int("X", "0", minimum=1)


class TestScorerDefaults:
    def test_default_scorer_matches_legacy_formulas(self):
        scorer = CandidateScorer(config=_ScoringConfig())

        assert scorer.priority_denom == 10.0
        assert scorer.latency_ref_ms == 250.0
        assert scorer.failure_ref_count == 5
        assert scorer.priority_score(10) == pytest.approx(10 / (10 + 10))
        assert scorer.latency_score(250) == pytest.approx(0.5)
        assert scorer.failure_score(5) == 0.0

    def test_default_scorer_matches_global_settings(self):
        default = CandidateScorer()
        from_settings = CandidateScorer(config=Settings())

        assert default.priority_weight == from_settings.priority_weight
        assert default.latency_ref_ms == from_settings.latency_ref_ms


class TestScorerWeightChanges:
    def test_latency_weight_zero_makes_latency_irrelevant(self):
        fast = _stats(50)
        slow = _stats(1000)
        scorer = CandidateScorer(
            config=_ScoringConfig(scoring_latency_weight=0.0)
        )

        assert scorer.fitness(1, fast) == scorer.fitness(1, slow)

    def test_default_latency_weight_prefers_faster(self):
        fast = _stats(50)
        slow = _stats(1000)
        scorer = CandidateScorer()

        assert scorer.fitness(1, fast) > scorer.fitness(1, slow)

    def test_latency_weight_amplifies_gap(self):
        fast = _stats(50)
        slow = _stats(1000)
        default = CandidateScorer()
        boosted = CandidateScorer(
            config=_ScoringConfig(scoring_latency_weight=2.0)
        )

        gap_default = default.fitness(1, fast) - default.fitness(1, slow)
        gap_boosted = boosted.fitness(1, fast) - boosted.fitness(1, slow)

        assert gap_boosted == pytest.approx(2 * gap_default)

    def test_preference_weight_zero_ignores_reference_order(self):
        scorer = CandidateScorer(
            config=_ScoringConfig(scoring_preference_weight=0.0)
        )

        assert scorer.fitness(1, preference=0) == scorer.fitness(
            1,
            preference=5,
        )

    def test_default_preference_prefers_earlier_reference(self):
        scorer = CandidateScorer()

        assert scorer.fitness(1, preference=0) > scorer.fitness(
            1,
            preference=5,
        )

    def test_candidate_builder_forwards_scoring_config(self):
        builder = CandidateBuilder(
            config=_ScoringConfig(scoring_latency_weight=0.5)
        )

        assert builder._scorer.latency_weight == 0.5


class TestHealthThresholdConfig:
    def test_provider_threshold_lowered_via_config(self):
        store = HealthStore(provider_server_error_threshold=1)
        store.record_failure("A", "m1", FailureKind.SERVER_ERROR.value)

        state = store.learned("A")

        assert state is not None
        assert state.provider_status == "degraded"

    def test_provider_threshold_default_keeps_provider_healthy(self):
        store = HealthStore()
        store.record_failure("A", "m1", FailureKind.SERVER_ERROR.value)

        state = store.learned("A")

        assert state is not None
        assert state.provider_status is None
        assert state.degraded_models == frozenset({"m1"})

    def test_timeout_unavailable_threshold_lowered_via_config(self):
        store = HealthStore(model_timeout_unavailable_threshold=1)
        store.record_failure("A", "m1", FailureKind.TIMEOUT.value)

        state = store.learned("A")

        assert state is not None
        assert state.unavailable_models == frozenset({"m1"})


class TestFreshnessExponent:
    def test_exponent_shapes_decay_curve(self):
        clock = {"t": 0.0}
        store = HealthStore(
            ttl_seconds=100,
            freshness_exponent=2.0,
            now=lambda: clock["t"],
        )
        store.save(make_report("A"))

        clock["t"] = 50.0
        assert store.freshness("A") == pytest.approx(0.25)

        clock["t"] = 100.0
        assert store.freshness("A") == 0.0

    def test_exponent_zero_keeps_full_until_expiry(self):
        clock = {"t": 0.0}
        store = HealthStore(
            ttl_seconds=100,
            freshness_exponent=0.0,
            now=lambda: clock["t"],
        )
        store.save(make_report("A"))

        clock["t"] = 50.0
        assert store.freshness("A") == 1.0

        clock["t"] = 101.0
        assert store.freshness("A") == 0.0


class TestTelemetryHistoryConfig:
    def test_failure_history_bounded_by_config(self):
        store = TelemetryStore(max_failure_history=2)

        for _ in range(5):
            store.record_attempt(
                "p",
                "m",
                success=False,
                failure_type="timeout",
            )

        assert len(store.recent_failures("p", "m")) == 2

    def test_default_failure_history_is_large(self):
        store = TelemetryStore()

        for _ in range(10):
            store.record_attempt(
                "p",
                "m",
                success=False,
                failure_type="timeout",
            )

        assert len(store.recent_failures("p", "m")) == 10


class TestAdaptiveConfig:
    def test_adaptive_defaults(self, monkeypatch):
        _clear_tuning_env(monkeypatch)
        cfg = Settings()

        assert cfg.adaptive_routing_enabled is False
        assert cfg.adaptive_min_samples == 10
        assert cfg.adaptive_learning_rate == 0.1
        assert cfg.adaptive_latency_weight == 1.0
        assert cfg.adaptive_reliability_weight == 1.0

    def test_adaptive_custom_values_are_accepted(self, monkeypatch):
        monkeypatch.setenv("ADAPTIVE_LEARNING_RATE", "0.3")
        monkeypatch.setenv("ADAPTIVE_MIN_SAMPLES", "5")
        monkeypatch.setenv("ADAPTIVE_LATENCY_WEIGHT", "2.0")
        cfg = Settings()

        assert cfg.adaptive_learning_rate == 0.3
        assert cfg.adaptive_min_samples == 5
        assert cfg.adaptive_latency_weight == 2.0

    def test_adaptive_rejects_negative_learning_rate(self, monkeypatch):
        monkeypatch.setenv("ADAPTIVE_LEARNING_RATE", "-0.1")

        with pytest.raises(ValueError):
            Settings()

    def test_adaptive_rejects_zero_min_samples(self, monkeypatch):
        monkeypatch.setenv("ADAPTIVE_MIN_SAMPLES", "0")

        with pytest.raises(ValueError):
            Settings()

    def test_scorer_reads_adaptive_config(self):
        scorer = CandidateScorer(
            config=_ScoringConfig(
                adaptive_routing_enabled=True,
                adaptive_min_samples=3,
                adaptive_reliability_weight=2.0,
                adaptive_latency_weight=0.5,
            )
        )

        assert scorer.adaptive_routing_enabled is True
        assert scorer.adaptive_min_samples == 3
        assert scorer.adaptive_reliability_weight == 2.0
        assert scorer.adaptive_latency_weight == 0.5


class TestQualityConfig:
    def test_quality_defaults(self, monkeypatch):
        _clear_tuning_env(monkeypatch)
        cfg = Settings()

        assert cfg.quality_feedback_enabled is False
        assert cfg.quality_feedback_min_samples == 10
        assert cfg.quality_feedback_learning_rate == 0.1
        assert cfg.quality_feedback_retention_limit == 10000
        assert cfg.quality_feedback_weight == 1.0

    def test_quality_custom_values_are_accepted(self, monkeypatch):
        monkeypatch.setenv("QUALITY_FEEDBACK_LEARNING_RATE", "0.25")
        monkeypatch.setenv("QUALITY_FEEDBACK_MIN_SAMPLES", "5")
        monkeypatch.setenv("QUALITY_FEEDBACK_RETENTION_LIMIT", "500")
        monkeypatch.setenv("QUALITY_FEEDBACK_WEIGHT", "2.0")
        cfg = Settings()

        assert cfg.quality_feedback_learning_rate == 0.25
        assert cfg.quality_feedback_min_samples == 5
        assert cfg.quality_feedback_retention_limit == 500
        assert cfg.quality_feedback_weight == 2.0

    def test_quality_rejects_negative_learning_rate(self, monkeypatch):
        monkeypatch.setenv("QUALITY_FEEDBACK_LEARNING_RATE", "-0.1")

        with pytest.raises(ValueError):
            Settings()

    def test_quality_rejects_zero_min_samples(self, monkeypatch):
        monkeypatch.setenv("QUALITY_FEEDBACK_MIN_SAMPLES", "0")

        with pytest.raises(ValueError):
            Settings()

    def test_quality_rejects_zero_retention_limit(self, monkeypatch):
        monkeypatch.setenv("QUALITY_FEEDBACK_RETENTION_LIMIT", "0")

        with pytest.raises(ValueError):
            Settings()

    def test_quality_rejects_negative_weight(self, monkeypatch):
        monkeypatch.setenv("QUALITY_FEEDBACK_WEIGHT", "-1.0")

        with pytest.raises(ValueError):
            Settings()

    def test_scorer_reads_quality_config(self):
        scorer = CandidateScorer(
            config=_ScoringConfig(
                quality_feedback_enabled=True,
                quality_feedback_weight=2.0,
            )
        )

        assert scorer.quality_feedback_enabled is True
        assert scorer.quality_weight == 2.0
