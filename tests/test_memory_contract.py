import json

import pytest

from app.services.decision_engine import DecisionStats
from app.services.failure_classifier import FailureKind
from app.services.health_store import HealthStore
from app.services.log_service import RequestLogger
from app.services.memory_contract import (
    FORBIDDEN_KEYS,
    MEMORY_SURFACES,
    MemoryClass,
    contains_never_captured,
)
from app.services.metrics import RelayMetrics
from app.services.ops_store import RequestStatsStore
from app.services.quality import QualityStore
from app.services.telemetry import TelemetryStore


class TestMemoryClasses:
    def test_durable_surfaces_are_persistence_backed(self):
        durable = [
            "state_store",
            "state_flusher",
            "learned_health_feedback",
            "telemetry_aggregates",
            "telemetry_failure_history",
            "adaptive_routing_learning",
            "quality_feedback_aggregates",
            "decision_stats",
        ]

        for surface in durable:
            assert MEMORY_SURFACES[surface] == MemoryClass.DURABLE

    def test_ephemeral_surfaces_are_in_memory(self):
        ephemeral = [
            "health_snapshots",
            "ops_store",
            "metrics",
            "logs",
            "correlation_ids",
            "task_classifications",
            "decision_scores",
            "decision_explanations",
            "decision_records",
        ]

        for surface in ephemeral:
            assert MEMORY_SURFACES[surface] == MemoryClass.EPHEMERAL

    def test_never_surfaces_are_forbidden(self):
        never = [
            "prompts",
            "responses",
            "generated_content",
            "api_keys",
            "proxy_credentials",
            "user_identity",
        ]

        for surface in never:
            assert MEMORY_SURFACES[surface] == MemoryClass.NEVER


class TestContainsNeverCaptured:
    def test_clean_dict_is_safe(self):
        assert not contains_never_captured(
            {"provider": "A", "model": "a-1", "latency_ms": 10}
        )

    def test_forbidden_key_at_top_level(self):
        assert contains_never_captured({"message": "hello"})
        assert contains_never_captured({"api_key": "sk-test"})

    def test_forbidden_key_nested(self):
        assert contains_never_captured(
            {"attempts": [{"provider": "A", "response": "hi"}]}
        )

    @pytest.mark.parametrize(
        "key",
        [
            "prompt",
            "prompts",
            "prompt_text",
            "message",
            "messages",
            "user_message",
            "response",
            "responses",
            "model_response",
            "content",
            "api_key",
            "api-key",
            "apikey",
            "authorization",
            "proxy",
            "proxy_url",
            "password",
            "secret",
            "secret_value",
            "user_identity",
            "identity",
        ],
    )
    def test_documented_variants_are_never_captured(self, key):
        assert contains_never_captured({key: "x"})
        assert contains_never_captured({"outer": {"inner": {key: "x"}}})

    def test_variant_keys_are_all_registered(self):
        for variant in (
            "prompt_text",
            "user_message",
            "secret_value",
            "model_response",
        ):
            assert variant in FORBIDDEN_KEYS

    def test_forbidden_key_is_case_insensitive(self):
        assert contains_never_captured({"Message": "hello"})
        assert contains_never_captured({"API_KEY": "sk-test"})

    def test_list_of_dicts_scanned(self):
        assert contains_never_captured(
            [{"provider": "A"}, {"prompt": "hi"}]
        )

    def test_scalar_values_are_safe(self):
        assert not contains_never_captured("hello world")
        assert not contains_never_captured(42)

    def test_forbidden_keys_are_never_empty(self):
        assert FORBIDDEN_KEYS


class TestTelemetryExportNeverCaptures:
    def test_export_has_no_forbidden_keys_or_content(self):
        store = TelemetryStore()
        store.record_attempt("A", "a-1", True, 10)
        store.record_attempt("A", "a-1", False, 10, "timeout")

        export = store.export_state()

        assert not contains_never_captured(export)

    def test_import_round_trip_never_captures(self):
        store = TelemetryStore()
        store.record_attempt("A", "a-1", True, 10)

        export = store.export_state()
        fresh = TelemetryStore()
        fresh.import_state(export)

        assert not contains_never_captured(fresh.export_state())


class TestHealthExportNeverCaptures:
    def test_learned_state_export_has_no_forbidden_keys(self):
        store = HealthStore(provider_server_error_threshold=1)
        store.record_failure("A", "m1", FailureKind.SERVER_ERROR.value)

        export = store.export_learned_state()

        assert not contains_never_captured(export)


class TestQualityExportNeverCaptures:
    def test_quality_export_has_no_forbidden_keys_or_content(self):
        store = QualityStore()
        store.record("A", "a-1", 5, category="speed", correlation_id="cid-1")

        export = store.export_state()

        assert not contains_never_captured(export)
        assert "cid-1" not in json.dumps(export)

    def test_quality_import_round_trip_never_captures(self):
        store = QualityStore()
        store.record("A", "a-1", 4, category="accuracy")

        export = store.export_state()
        fresh = QualityStore()
        fresh.import_state(export)

        assert not contains_never_captured(fresh.export_state())


class TestDecisionStatsExportNeverCaptures:
    def test_decision_stats_export_is_metadata_only(self):
        stats = DecisionStats()
        stats.record(None, 0)

        export = stats.export_state()

        assert not contains_never_captured(export)
        assert set(export) == {"decisions", "candidates", "selected", "by_band"}

    def test_decision_stats_import_round_trip_never_captures(self):
        stats = DecisionStats()
        stats.record(None, 3)

        export = stats.export_state()
        fresh = DecisionStats()
        fresh.import_state(export)

        assert not contains_never_captured(fresh.export_state())


class TestOpsStoreNeverCaptures:
    def test_ops_events_are_metadata_only(self):
        store = RequestStatsStore(window_seconds=0, max_events=100)
        store.record_chat("/chat", False, "A", "a-1", True, False, 10)
        store.record_http("POST", "/chat", 200, 5)

        events = [vars(event) for event in store.events()]

        assert not contains_never_captured(events)

    def test_stats_summary_is_metadata_only(self):
        store = RequestStatsStore(window_seconds=0, max_events=100)
        store.record_chat("/chat", False, "A", "a-1", True, False, 10)

        assert not contains_never_captured(store.stats())


class TestMetricsNeverCaptures:
    def test_render_never_contains_forbidden_substrings(self):
        metrics = RelayMetrics()
        metrics.record_chat(
            "/chat",
            False,
            {"success": True, "provider": "A", "attempts": []},
            10,
            gen_kwargs={"max_tokens": 512},
        )

        text = metrics.render()

        for forbidden in ("api_key", "prompt", "message=", "response="):
            assert forbidden not in text


class TestRequestLoggerNeverCaptures:
    def test_chat_log_payload_is_metadata_only(self, monkeypatch):
        logger = RequestLogger()
        captured = []

        monkeypatch.setattr(
            logger,
            "_emit",
            lambda event, **data: captured.append((event, data)),
        )

        marker = "PROMPT-MARKER-12345"

        result = {
            "success": True,
            "provider": "A",
            "model": "a-1",
            "response": marker,
            "message": marker,
            "correlation_id": "cid-123",
            "latency_ms": 12,
            "attempts": [
                {
                    "provider": "A",
                    "model": "a-1",
                    "attempt": 0,
                    "success": True,
                    "latency_ms": 12,
                    "failure_type": None,
                    "reason": None,
                }
            ],
        }

        logger.chat(result)

        assert len(captured) == 2
        serialized = json.dumps([data for _, data in captured])

        assert marker not in serialized
        assert not contains_never_captured([data for _, data in captured])
