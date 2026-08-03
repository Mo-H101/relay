from app.services.scoring import (
    BAND_DEGRADED,
    BAND_HEALTHY,
    BAND_NOT_CHECKED,
    BAND_UNAVAILABLE,
    CandidateScorer,
    Rankable,
)
from app.services.telemetry import FailureEvent, TelemetryStats


def make_stats(
    request_count=1,
    success_count=1,
    failure_count=0,
    average_latency_ms=100.0,
    recent_failures=None,
):
    return TelemetryStats(
        provider="P",
        model="m",
        request_count=request_count,
        success_count=success_count,
        failure_count=failure_count,
        average_latency_ms=average_latency_ms,
        recent_failures=recent_failures or [],
    )


def make_failures(kinds):
    return [FailureEvent(failure_type=kind, ts=float(i)) for i, kind in enumerate(kinds)]


class TestPriorityFallback:
    def test_no_telemetry_orders_by_priority(self):
        scorer = CandidateScorer()
        candidates = [
            Rankable("A", "a-1", priority=1),
            Rankable("B", "b-1", priority=10),
            Rankable("C", "c-1", priority=5),
        ]

        ranked = scorer.rank(candidates)

        assert [c.provider for c in ranked] == ["B", "C", "A"]

    def test_no_telemetry_fitness_monotonic_in_priority(self):
        scorer = CandidateScorer()

        assert scorer.fitness(10) > scorer.fitness(5) > scorer.fitness(1)

    def test_no_band_defaults_to_not_checked(self):
        scorer = CandidateScorer()
        candidates = [
            Rankable("A", "a-1", priority=5),
            Rankable("B", "b-1", priority=10),
        ]

        ranked = scorer.rank(candidates)

        assert [c.provider for c in ranked] == ["B", "A"]

    def test_no_telemetry_candidates_keep_input_order_on_ties(self):
        scorer = CandidateScorer()
        candidates = [
            Rankable("A", "a-1", priority=10),
            Rankable("B", "b-1", priority=10),
        ]

        ranked = scorer.rank(candidates)

        assert [c.provider for c in ranked] == ["A", "B"]


class TestLatencyPreference:
    def test_faster_candidate_wins_within_same_band(self):
        scorer = CandidateScorer()
        slow = Rankable(
            "A",
            "a-1",
            priority=10,
            health_band=BAND_NOT_CHECKED,
            telemetry=make_stats(average_latency_ms=1000),
        )
        fast = Rankable(
            "B",
            "b-1",
            priority=5,
            health_band=BAND_NOT_CHECKED,
            telemetry=make_stats(average_latency_ms=50),
        )

        ranked = scorer.rank([slow, fast])

        assert [c.provider for c in ranked] == ["B", "A"]

    def test_latency_score_prefers_lower(self):
        scorer = CandidateScorer()

        assert scorer.latency_score(50) > scorer.latency_score(1000)

    def test_latency_neutral_when_unknown(self):
        scorer = CandidateScorer()

        assert scorer.latency_score(None) == 0.5


class TestSuccessRatePreference:
    def test_higher_success_rate_wins_within_same_band(self):
        scorer = CandidateScorer()
        weak = Rankable(
            "A",
            "a-1",
            priority=10,
            health_band=BAND_HEALTHY,
            telemetry=make_stats(
                request_count=10,
                success_count=2,
                failure_count=8,
                average_latency_ms=100,
            ),
        )
        strong = Rankable(
            "B",
            "b-1",
            priority=5,
            health_band=BAND_HEALTHY,
            telemetry=make_stats(
                request_count=10,
                success_count=9,
                failure_count=1,
                average_latency_ms=100,
            ),
        )

        ranked = scorer.rank([weak, strong])

        assert [c.provider for c in ranked] == ["B", "A"]

    def test_success_score_clamped_and_neutral(self):
        scorer = CandidateScorer()

        assert scorer.success_score(None) == 0.5
        assert scorer.success_score(2.0) == 1.0
        assert scorer.success_score(-1.0) == 0.0
        assert scorer.success_score(0.75) == 0.75


class TestFailurePenalty:
    def test_recent_failures_lose_within_same_band(self):
        scorer = CandidateScorer()
        clean = Rankable(
            "A",
            "a-1",
            priority=10,
            health_band=BAND_HEALTHY,
            telemetry=make_stats(average_latency_ms=100),
        )
        flaky = Rankable(
            "B",
            "b-1",
            priority=5,
            health_band=BAND_HEALTHY,
            telemetry=make_stats(
                average_latency_ms=100,
                recent_failures=make_failures(["timeout"] * 5),
            ),
        )

        ranked = scorer.rank([flaky, clean])

        assert [c.provider for c in ranked] == ["A", "B"]

    def test_failure_score_degrades_with_count(self):
        scorer = CandidateScorer()

        assert scorer.failure_score(0) == 1.0
        assert scorer.failure_score(3) < scorer.failure_score(1)
        assert scorer.failure_score(5) == 0.0
        assert scorer.failure_score(20) == 0.0


class TestHealthBandInvariant:
    def test_band_outranks_all_fitness(self):
        scorer = CandidateScorer()
        healthy_terrible = Rankable(
            "A",
            "a-1",
            priority=1,
            health_band=BAND_HEALTHY,
            telemetry=make_stats(
                request_count=10,
                success_count=0,
                failure_count=10,
                average_latency_ms=5000,
                recent_failures=make_failures(["timeout"] * 10),
            ),
        )
        degraded_excellent = Rankable(
            "B",
            "b-1",
            priority=100,
            health_band=BAND_DEGRADED,
            telemetry=make_stats(
                request_count=10,
                success_count=10,
                failure_count=0,
                average_latency_ms=1,
            ),
        )

        ranked = scorer.rank([degraded_excellent, healthy_terrible])

        assert [c.provider for c in ranked] == ["A", "B"]

    def test_band_ordering_ascending(self):
        scorer = CandidateScorer()
        candidates = [
            Rankable("unavail", "u", priority=100, health_band=BAND_UNAVAILABLE),
            Rankable("degraded", "d", priority=100, health_band=BAND_DEGRADED),
            Rankable("healthy", "h", priority=1, health_band=BAND_HEALTHY),
            Rankable("unknown", "n", priority=5, health_band=BAND_NOT_CHECKED),
        ]

        ranked = scorer.rank(candidates)

        assert [c.provider for c in ranked] == [
            "healthy",
            "degraded",
            "unknown",
            "unavail",
        ]


class TestTaskPreferenceScoring:
    def test_earlier_preference_wins_within_same_band(self):
        scorer = CandidateScorer()
        earlier = Rankable("A", "m1", priority=5, preference=0)
        later = Rankable("B", "m2", priority=5, preference=1)

        ranked = scorer.rank([later, earlier])

        assert [c.model for c in ranked] == ["m1", "m2"]

    def test_preference_score_rewards_earlier_refs(self):
        scorer = CandidateScorer()

        assert scorer.preference_score(0) == 1.0
        assert scorer.preference_score(0) > scorer.preference_score(1)
        assert scorer.preference_score(2) < scorer.preference_score(1)

    def test_preference_neutral_when_unknown(self):
        scorer = CandidateScorer()

        assert scorer.preference_score(None) == 0.5

    def test_no_preference_candidates_keep_priority_order(self):
        scorer = CandidateScorer()
        candidates = [
            Rankable("A", "a-1", priority=10),
            Rankable("B", "b-1", priority=1),
        ]

        ranked = scorer.rank(candidates)

        assert [c.provider for c in ranked] == ["A", "B"]

    def test_telemetry_still_breaks_ties_within_same_preference(self):
        scorer = CandidateScorer()
        slow = Rankable(
            "A",
            "m1",
            priority=10,
            health_band=BAND_NOT_CHECKED,
            telemetry=make_stats(average_latency_ms=1000),
            preference=0,
        )
        fast = Rankable(
            "B",
            "m1",
            priority=1,
            health_band=BAND_NOT_CHECKED,
            telemetry=make_stats(average_latency_ms=50),
            preference=0,
        )

        ranked = scorer.rank([slow, fast])

        assert [c.provider for c in ranked] == ["B", "A"]


class TestDeterminism:
    def test_identical_inputs_produce_identical_order(self):
        scorer = CandidateScorer()
        candidates = [
            Rankable("A", "a-1", priority=5, telemetry=make_stats()),
            Rankable("B", "b-1", priority=5, telemetry=make_stats()),
        ]

        first = scorer.rank(candidates)
        second = scorer.rank(candidates)

        assert [c.provider for c in first] == [c.provider for c in second]

    def test_equal_keys_preserve_input_order(self):
        scorer = CandidateScorer()
        candidates = [
            Rankable("A", "a-1", priority=5),
            Rankable("B", "b-1", priority=5),
            Rankable("C", "c-1", priority=5),
        ]

        ranked = scorer.rank(candidates)

        assert [c.provider for c in ranked] == ["A", "B", "C"]
