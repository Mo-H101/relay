import threading

from app.services.health_checker import HEALTHY, DEGRADED, ProviderHealth
from app.services.health_store import HealthStore


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


class TestHealthStore:
    def test_get_returns_saved_snapshot(self):
        store = HealthStore(ttl_seconds=300)
        store.save(make_report("A"))

        report = store.get("A")

        assert report is not None
        assert report.name == "A"
        assert report.status == HEALTHY

    def test_get_returns_none_for_unknown_provider(self):
        store = HealthStore()

        assert store.get("missing") is None

    def test_get_returns_none_after_ttl_expiry(self):
        store = HealthStore(ttl_seconds=-1)
        store.save(make_report("A"))

        assert store.get("A") is None

    def test_save_overwrites_previous_snapshot(self):
        store = HealthStore()
        store.save(make_report("A", status=HEALTHY))
        store.save(make_report("A", status=DEGRADED))

        assert store.get("A").status == DEGRADED

    def test_clear_removes_snapshots(self):
        store = HealthStore()
        store.save(make_report("A"))

        store.clear()

        assert store.get("A") is None

    def test_concurrent_reads_and_writes_are_safe(self):
        store = HealthStore(ttl_seconds=300)
        errors = []

        def writer():
            try:
                for i in range(200):
                    store.save(make_report(f"P{i % 10}"))
            except Exception as exc:
                errors.append(exc)

        def reader():
            try:
                for _ in range(200):
                    store.get("P1")
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer) for _ in range(4)
        ] + [
            threading.Thread(target=reader) for _ in range(4)
        ]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()

        assert errors == []


class TestHealthStoreFreshness:
    def test_freshness_is_full_when_just_saved(self):
        store = HealthStore(ttl_seconds=300)
        store.save(make_report("A"))

        assert store.freshness("A") == 1.0

    def test_freshness_zero_for_unknown_provider(self):
        store = HealthStore()

        assert store.freshness("missing") == 0.0

    def test_freshness_zero_after_expiry(self):
        store = HealthStore(ttl_seconds=-1)
        store.save(make_report("A"))

        assert store.freshness("A") == 0.0
        assert store.get("A") is None

    def test_freshness_decays_over_ttl(self):
        times = iter([100.0, 100.0, 115.0, 130.0, 131.0, 132.0])
        store = HealthStore(
            ttl_seconds=30,
            now=lambda: next(times),
        )
        store.save(make_report("A"))

        assert store.freshness("A") == 1.0

        assert store.freshness("A") == 0.5

        assert store.get("A") is not None

        assert store.freshness("A") == 0.0
        assert store.get("A") is None

    def test_freshness_read_only_keeps_get_hard_expiry(self):
        store = HealthStore(ttl_seconds=5)
        store.save(make_report("A"))

        assert store.freshness("A") == 1.0
        assert store.get("A") is not None


class TestHealthStoreFeedback:
    def test_no_learned_state_initially(self):
        store = HealthStore()

        assert store.learned("A") is None

    def test_auth_error_marks_provider_unavailable(self):
        store = HealthStore()
        store.record_failure("A", "m1", "auth_error")

        state = store.learned("A")

        assert state is not None
        assert state.provider_status == "unavailable"

    def test_rate_limit_marks_provider_degraded(self):
        store = HealthStore()
        store.record_failure("A", "m1", "rate_limit")

        state = store.learned("A")

        assert state.provider_status == "degraded"

    def test_server_error_marks_model_degraded_only(self):
        store = HealthStore()
        store.record_failure("A", "m1", "server_error")

        state = store.learned("A")

        assert state.provider_status is None
        assert state.degraded_models == frozenset({"m1"})

    def test_repeated_server_error_marks_provider_degraded(self):
        store = HealthStore()
        for _ in range(3):
            store.record_failure("A", "m1", "server_error")

        state = store.learned("A")

        assert state.provider_status == "degraded"
        assert state.degraded_models == frozenset({"m1"})

    def test_timeout_ignored_below_threshold(self):
        store = HealthStore()
        store.record_failure("A", "m1", "timeout")

        assert store.learned("A") is None

    def test_success_clears_learned_degradation(self):
        store = HealthStore()
        store.record_failure("A", "m1", "server_error")

        store.record_success("A", "m1")

        assert store.learned("A") is None

    def test_unavailable_expires_after_ttl(self):
        store = HealthStore(
            ttl_seconds=300,
            degraded_ttl_seconds=60,
            unavailable_ttl_seconds=-1,
        )
        store.record_failure("A", "m1", "auth_error")

        assert store.learned("A") is None

    def test_degraded_expires_after_ttl(self):
        store = HealthStore(
            ttl_seconds=300,
            degraded_ttl_seconds=-1,
            unavailable_ttl_seconds=900,
        )
        store.record_failure("A", "m1", "rate_limit")

        assert store.learned("A") is None

    def test_record_failure_and_success_are_thread_safe(self):
        store = HealthStore()
        errors = []

        def worker():
            try:
                for _ in range(200):
                    store.record_failure("P1", "m1", "server_error")
                    store.record_success("P1", "m1")
                    store.learned("P1")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()

        assert errors == []
