from app.services.scoring import CandidateScorer, Rankable
from app.services.signals import (
    SIGNALS,
    SIGNAL_KEYS,
    enabled_attr_for,
    label_for,
    neutral_for,
    signal_for,
    weight_attr_for,
)


class TestSignalRegistry:
    def test_signal_keys_are_unique(self):
        keys = [signal.key for signal in SIGNALS]

        assert len(keys) == len(set(keys))

    def test_legacy_signal_order_preserved(self):
        assert SIGNAL_KEYS[:5] == (
            "priority",
            "success",
            "latency",
            "failure",
            "preference",
        )

    def test_task_compatibility_adaptive_and_quality_signals_appended_last(self):
        assert SIGNAL_KEYS[-5] == "task_compatibility"
        assert SIGNAL_KEYS[-4:-2] == (
            "adaptive_reliability",
            "adaptive_latency",
        )
        assert SIGNAL_KEYS[-2] == "quality"
        assert SIGNAL_KEYS[-1] == "cost"

    def test_neutral_values_in_unit_range(self):
        for signal in SIGNALS:
            assert 0.0 <= signal.neutral <= 1.0

    def test_weight_attributes_exist_on_scorer(self):
        scorer = CandidateScorer()

        for signal in SIGNALS:
            assert hasattr(scorer, signal.weight_attr)

    def test_every_signal_has_a_label(self):
        for signal in SIGNALS:
            assert label_for(signal.key) == signal.label

    def test_helpers_round_trip(self):
        signal = signal_for("latency")

        assert signal.key == "latency"
        assert weight_attr_for("latency") == "latency_weight"
        assert neutral_for("latency") == 0.5
        assert enabled_attr_for("priority") is None
        assert enabled_attr_for("task_compatibility") == "task_catalog_enabled"
        assert (
            enabled_attr_for("adaptive_reliability")
            == "adaptive_routing_enabled"
        )
        assert enabled_attr_for("adaptive_latency") == "adaptive_routing_enabled"


class TestBreakdownEmitsEverySignal:
    def test_breakdown_contains_every_signal_key(self):
        scorer = CandidateScorer()
        breakdown = scorer.breakdown(Rankable("A", "a-1", priority=5))

        assert set(breakdown) == set(SIGNAL_KEYS) | {"health_band", "total"}

    def test_disabled_signal_emits_zero_contribution(self):
        scorer = CandidateScorer()
        breakdown = scorer.breakdown(Rankable("A", "a-1", priority=5))

        assert breakdown["task_compatibility"] == 0.0
        assert breakdown["adaptive_reliability"] == 0.0
        assert breakdown["adaptive_latency"] == 0.0
        assert breakdown["quality"] == 0.0
        assert breakdown["cost"] == 0.0

    def test_cost_signal_is_neutral_by_default(self):
        scorer = CandidateScorer()

        assert scorer.cost_weight == 0.0
        assert scorer.cost_score(None) == 0.5
        assert scorer.cost_score(99) == 0.5

    def test_breakdown_total_matches_legacy_components_when_disabled(self):
        scorer = CandidateScorer()
        rankable = Rankable(
            "A",
            "a-1",
            priority=10,
            health_band=0,
            telemetry=None,
            preference=0,
        )

        breakdown = scorer.breakdown(rankable)

        legacy_components = sum(
            breakdown[key]
            for key in ("priority", "success", "latency", "failure", "preference")
        )

        assert breakdown["total"] == round(legacy_components, 4)
