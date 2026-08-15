"""
Phase 7: actual decision record (orchestration truth layer) unit tests.

Covers the DecisionRecordStore bounded/thread-safe behavior, the
DecisionRecord serialization surface, the candidate/attempt builders, and
the privacy contract over every serialized record.
"""

import threading

from app.providers.base import Provider
from app.services.decision_record import (
    DecisionAttempt,
    DecisionCandidate,
    DecisionRecord,
    DecisionRecordStore,
    build_attempts,
    build_candidates,
    decision_score_for,
    selected_rank,
)
from app.services.memory_contract import contains_never_captured


def make_provider(name, models, priority=1):
    return Provider(
        name=name,
        base_url=f"https://{name.lower()}.invalid",
        api_key="test-key",
        enabled=True,
        priority=priority,
        models=list(models),
    )


def make_record(correlation_id="req-1", **overrides):
    fields = {
        "correlation_id": correlation_id,
        "timestamp": 1000.0,
        "requested_model": "auto",
        "classified_task": "coding",
        "routed": True,
        "selected_provider": "A",
        "selected_model": "a-1",
        "candidates": (
            DecisionCandidate("A", "a-1", 1),
            DecisionCandidate("B", "b-1", 2),
        ),
        "attempts": (
            DecisionAttempt("A", "a-1", True, 12),
        ),
        "outcome": "succeeded",
        "selected_rank": 1,
        "decision_reason": "routed to top-ranked candidate",
        "confidence": None,
        "signals": None,
    }
    fields.update(overrides)
    return DecisionRecord(**fields)


class TestDecisionRecord:
    def test_serialization_shape(self):
        record = make_record()
        data = record.to_dict()

        assert data["correlation_id"] == "req-1"
        assert data["requested_model"] == "auto"
        assert data["classified_task"] == "coding"
        assert data["routed"] is True
        assert data["selected_provider"] == "A"
        assert data["selected_model"] == "a-1"
        assert data["selected_rank"] == 1
        assert data["outcome"] == "succeeded"
        assert data["candidates"] == [
            {"provider": "A", "model": "a-1", "rank": 1},
            {"provider": "B", "model": "b-1", "rank": 2},
        ]
        assert data["attempts"] == [
            {
                "provider": "A",
                "model": "a-1",
                "success": True,
                "latency_ms": 12,
                "failure_type": None,
            }
        ]
        # Timestamp is rendered as a human-readable ISO string.
        assert "T" in data["timestamp"]
        assert data["timestamp"].endswith("+00:00")

    def test_serialization_is_metadata_only(self):
        assert not contains_never_captured(make_record().to_dict())

    def test_serialization_metadata_only_with_signals(self):
        record = make_record(
            signals={
                "priority": 1.0,
                "success": 0.5,
                "latency": 0.4,
                "failure": 0.0,
                "preference": 0.5,
                "task_compatibility": 0.5,
                "adaptive_reliability": 0.5,
                "adaptive_latency": 0.5,
                "quality": 0.5,
                "cost": 0.0,
            }
        )
        assert not contains_never_captured(record.to_dict())
        assert record.to_dict()["signals"]["priority"] == 1.0

    def test_no_content_fields_allowed(self):
        record = make_record()
        assert "response" not in record.to_dict()
        assert "message" not in record.to_dict()
        assert "prompt" not in record.to_dict()
        assert "content" not in record.to_dict()


class TestDecisionRecordStore:
    def test_record_and_most_recent(self):
        store = DecisionRecordStore()
        store.record(make_record("r1", timestamp=1.0))
        store.record(make_record("r2", timestamp=2.0))

        assert store.most_recent().correlation_id == "r2"

    def test_get_by_correlation_id(self):
        store = DecisionRecordStore()
        store.record(make_record("r1"))
        store.record(make_record("r2"))

        assert store.get("r1").correlation_id == "r1"
        assert store.get("missing") is None

    def test_empty_store(self):
        store = DecisionRecordStore()
        assert store.most_recent() is None
        assert store.get("anything") is None
        assert store.snapshot() == []

    def test_bounded_eviction(self):
        store = DecisionRecordStore(max_records=3)

        for i in range(5):
            store.record(make_record(f"r{i}", timestamp=float(i)))

        assert [r["correlation_id"] for r in store.snapshot()] == [
            "r2",
            "r3",
            "r4",
        ]
        assert store.get("r0") is None
        assert store.get("r4") is not None

    def test_update_attaches_late_outcome(self):
        store = DecisionRecordStore()
        store.record(make_record("s1", outcome="stream_started"))

        assert store.update("s1", outcome="succeeded") is True
        assert store.get("s1").outcome == "succeeded"

    def test_update_unknown_id_returns_false(self):
        store = DecisionRecordStore()
        assert store.update("missing", outcome="failed") is False

    def test_snapshot_limited(self):
        store = DecisionRecordStore()
        for i in range(4):
            store.record(make_record(f"r{i}", timestamp=float(i)))

        assert [r["correlation_id"] for r in store.snapshot(limit=2)] == [
            "r2",
            "r3",
        ]

    def test_thread_safe_records(self):
        store = DecisionRecordStore(max_records=100)
        errors = []

        def worker(start):
            try:
                for i in range(50):
                    store.record(
                        make_record(f"w{start}-{i}", timestamp=float(i))
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(n,)) for n in range(4)
        ]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert len(store.snapshot()) == 100
        assert all(r["correlation_id"] for r in store.snapshot())

    def test_snapshot_metadata_only(self):
        store = DecisionRecordStore()
        store.record(make_record())
        assert not contains_never_captured(store.snapshot())


class TestBuilders:
    def test_build_candidates_ranks_in_order(self):
        providers = [make_provider("A", ["a-1", "a-2"])]

        ranked = build_candidates([(providers[0], "a-1"), (providers[0], "a-2")])

        assert ranked == (
            DecisionCandidate("A", "a-1", 1),
            DecisionCandidate("A", "a-2", 2),
        )

    def test_selected_rank(self):
        providers = [make_provider("A", ["a-1"]), make_provider("B", ["b-1"])]
        ranked = build_candidates(
            [(providers[0], "a-1"), (providers[1], "b-1")]
        )

        assert selected_rank("A", "a-1", ranked) == 1
        assert selected_rank("B", "b-1", ranked) == 2
        assert selected_rank("C", "c-1", ranked) is None

    def test_build_attempts_drops_reason(self):
        attempts = build_attempts(
            [
                {
                    "provider": "A",
                    "model": "a-1",
                    "success": False,
                    "latency_ms": 5,
                    "failure_type": "timeout",
                    "reason": "socket read timeout after 5s",
                },
                {
                    "provider": "A",
                    "model": "a-2",
                    "success": True,
                    "latency_ms": 9,
                    "failure_type": None,
                },
            ]
        )

        assert attempts == (
            DecisionAttempt("A", "a-1", False, 5, "timeout"),
            DecisionAttempt("A", "a-2", True, 9, None),
        )

        # The reason string never survives into the metadata surface.
        assert not contains_never_captured(
            [{"provider": a.provider, "model": a.model} for a in attempts]
        )

    def test_build_attempts_empty_and_garbage(self):
        assert build_attempts(None) == ()
        assert build_attempts([]) == ()
        assert build_attempts([None, "junk"]) == ()


class TestDecisionScoreLookup:
    def test_decision_score_for_matches_executed_candidate(self):
        class FakeScore:
            def __init__(self, provider, model):
                self.provider = provider
                self.model = model

        class FakeResult:
            ranked = [
                FakeScore("A", "a-1"),
                FakeScore("B", "b-1"),
            ]

        assert decision_score_for(FakeResult(), "A", "a-1").model == "a-1"
        assert decision_score_for(FakeResult(), "B", "b-1").model == "b-1"
        assert decision_score_for(FakeResult(), "C", "c-1") is None
        assert decision_score_for(None, "A", "a-1") is None
