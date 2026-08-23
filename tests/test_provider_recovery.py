import threading
import time

import pytest

from app.core.config import settings
from app.core.relay import Relay
from app.providers.base import Provider
from app.providers.registry import PROVIDER_REGISTRY
from app.services.metrics import relay_metrics
from app.services.provider_manager import ProviderManager
from app.services.provider_recovery import ProviderRecovery


def make_provider(defn, models):
    return Provider(
        name=defn.provider_name,
        base_url=defn.base_url_default,
        id=defn.id,
        api_key="",
        enabled=True,
        priority=defn.runtime_priority,
        requires_api_key=defn.requires_api_key,
        models=list(models),
    )


class ScriptedBuilder:
    """
    Stand-in for build_runtime_provider_detailed. Each call consumes the
    next script entry; the last entry repeats. Entries are:
      ("ok", [models])          -> success
      ("discovery", exc)        -> provider built, discovery failed
      ("raise", exc)            -> factory itself raised
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, defn):
        self.calls.append(defn.id)
        index = min(len(self.calls) - 1, len(self.script) - 1)
        kind, payload = self.script[index]

        if kind == "raise":
            raise payload

        models = [] if kind == "discovery" else list(payload)
        error = payload if kind == "discovery" else None
        return make_provider(defn, models), error


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def seed_startup_failure(manager, defn, status="discovery_failed"):
    """
    Reproduce the exact state Relay._load_providers leaves behind when
    startup discovery fails: an empty-catalog provider registered under
    its stable identity plus a failed registration record.
    """
    manager.register(make_provider(defn, []))
    if status == "initialization_failed":
        stage = "runtime"
    else:
        stage = "model_discovery"
    manager.record_registration(
        defn.id,
        provider_name=defn.provider_name,
        status=status,
        stage=stage,
        enabled=True,
        error_kind="network",
    )


def build(script, interval=30.0, max_interval=600.0, clock=None):
    """
    Build a ProviderRecovery wired to ``clock``. When no clock is given
    the service runs on real monotonic time (thread-driven tests);
    clock-driven tests inject a FakeClock explicitly.
    """
    manager = ProviderManager()
    builder = ScriptedBuilder(script)
    time_fn = clock if clock is not None else time.monotonic
    recovery = ProviderRecovery(
        provider_manager=manager,
        interval_seconds=interval,
        max_interval_seconds=max_interval,
        builder=builder,
        time_fn=time_fn,
    )
    return manager, builder, recovery, clock


def wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


LMSTUDIO = PROVIDER_REGISTRY["lmstudio"]


@pytest.fixture(autouse=True)
def enable_lmstudio(monkeypatch):
    monkeypatch.setattr(settings, "lmstudio_enabled", True)


class TestF1Regression:
    def test_provider_down_at_startup_recovers_without_restart(self):
        manager, _, recovery, _ = build(
            [("discovery", ConnectionError("refused")), ("ok", ["m1", "m2"])],
            interval=0.05,
            max_interval=0.2,
        )
        seed_startup_failure(manager, LMSTUDIO)

        recovery.start()
        try:
            recovered = wait_until(
                lambda: (
                    manager.registration_status_for("lmstudio")["status"]
                    == "registered"
                )
            )
        finally:
            recovery.stop()

        assert recovered, "provider was never rediscovered"

        provider = manager.get("lmstudio")
        assert provider is not None
        assert provider.models == ["m1", "m2"]

        ranked = manager.ranked()
        assert ranked and ranked[0].identity() == "lmstudio"
        assert ranked[0].models == ["m1", "m2"]

    def test_recovered_registration_is_clean(self):
        manager, _, recovery, _ = build([("ok", ["m1"])], interval=0.05)
        seed_startup_failure(manager, LMSTUDIO)

        assert recovery.recover_once() == 1

        entry = manager.registration_status_for("lmstudio")
        assert entry["status"] == "registered"
        assert entry["stage"] == "runtime"
        assert entry["enabled"] is True
        assert entry["error_kind"] is None

    def test_initialization_failure_also_recovers(self):
        manager, _, recovery, clock = build(
            [("raise", RuntimeError("boom")), ("ok", ["m1"])],
            interval=10.0,
            clock=FakeClock(),
        )
        seed_startup_failure(manager, LMSTUDIO, status="initialization_failed")

        assert recovery.recover_once() == 1
        entry = manager.registration_status_for("lmstudio")
        assert entry["status"] == "initialization_failed"

        clock.advance(10.0)
        assert recovery.recover_once() == 1
        assert (
            manager.registration_status_for("lmstudio")["status"]
            == "registered"
        )


class TestNoDiscoveryStorms:
    def test_backoff_suppresses_immediate_retries(self):
        manager, builder, recovery, _ = build(
            [("discovery", ConnectionError("down"))], interval=30.0
        )
        seed_startup_failure(manager, LMSTUDIO)

        assert recovery.recover_once() == 1
        assert recovery.recover_once() == 0
        assert recovery.recover_once() == 0
        assert builder.calls == ["lmstudio"]

    def test_backoff_doubles_and_caps(self):
        manager, builder, recovery, clock = build(
            [("discovery", ConnectionError("down"))],
            interval=10.0,
            max_interval=40.0,
            clock=FakeClock(),
        )
        seed_startup_failure(manager, LMSTUDIO)

        # Attempt 1 at t=0 -> next retry due at +10.
        assert recovery.recover_once() == 1
        clock.advance(9.0)
        assert recovery.recover_once() == 0
        clock.advance(1.0)
        # Attempt 2 -> delay doubles to 20.
        assert recovery.recover_once() == 1
        clock.advance(19.0)
        assert recovery.recover_once() == 0
        clock.advance(1.0)
        # Attempt 3 -> delay doubles to 40 (capped from 80 onward).
        assert recovery.recover_once() == 1
        clock.advance(39.0)
        assert recovery.recover_once() == 0
        clock.advance(1.0)
        assert recovery.recover_once() == 1
        clock.advance(39.0)
        assert recovery.recover_once() == 0
        clock.advance(1.0)
        # Still capped at 40, never grows further.
        assert recovery.recover_once() == 1

        assert len(builder.calls) == 5

    def test_single_worker_thread_no_matter_how_often_started(self):
        _, _, recovery, _ = build(
            [("discovery", ConnectionError("down"))], interval=0.05
        )

        threads_before = threading.active_count()

        recovery.start()
        recovery.start()
        recovery.start()

        assert recovery.is_running is True

        worker = recovery._thread
        time.sleep(0.15)

        assert recovery._thread is worker
        recovery.stop()

        assert wait_until(
            lambda: threading.active_count() == threads_before
        )

    def test_manual_and_background_passes_serialize(self):
        manager = ProviderManager()
        seed_startup_failure(manager, LMSTUDIO)

        build_calls = []
        gate = threading.Event()

        def slow_failing_builder(defn):
            build_calls.append(defn.id)
            gate.wait(timeout=5.0)
            return make_provider(defn, []), ConnectionError("down")

        recovery = ProviderRecovery(
            provider_manager=manager,
            interval_seconds=30.0,
            builder=slow_failing_builder,
        )

        recovery.start()
        try:
            # Background pass enters the slow builder and holds the pass
            # lock until the gate opens.
            assert wait_until(lambda: len(build_calls) >= 1)

            results = []
            manual = threading.Thread(
                target=lambda: results.append(recovery.recover_once())
            )
            manual.start()
            time.sleep(0.1)

            # The manual pass must be blocked, never run concurrently.
            assert results == []

            gate.set()
            manual.join(timeout=5.0)

            # It ran strictly afterwards and was suppressed by the fresh
            # backoff window (interval 30s >> elapsed), so no second
            # discovery build ever overlapped or duplicated.
            assert results == [0]
            assert len(build_calls) == 1
        finally:
            gate.set()
            recovery.stop()


class TestShutdown:
    def test_stop_cancels_background_work_cleanly(self):
        manager, builder, recovery, _ = build(
            [("discovery", ConnectionError("down"))], interval=0.05
        )
        seed_startup_failure(manager, LMSTUDIO)

        threads_before = threading.active_count()

        recovery.start()
        assert wait_until(lambda: len(builder.calls) >= 1)

        recovery.stop()

        assert recovery.is_running is False
        assert wait_until(
            lambda: threading.active_count() == threads_before
        )

        calls_after_stop = len(builder.calls)
        time.sleep(0.15)
        assert len(builder.calls) == calls_after_stop

    def test_stop_without_start_and_double_stop_are_safe(self):
        _, _, recovery, _ = build([("ok", ["m1"])])

        recovery.stop()
        assert recovery.is_running is False

        recovery.start()
        recovery.stop()
        recovery.stop()


class TestReloadPathsRemainCorrect:
    def test_healthy_providers_are_never_touched(self):
        manager, builder, recovery, _ = build(
            [("ok", ["fresh"])], interval=0.05
        )
        healthy = make_provider(LMSTUDIO, ["m1"])
        manager.register(healthy)

        assert recovery.recover_once() == 0
        assert builder.calls == []
        assert manager.get("lmstudio") is healthy
        assert manager.get("lmstudio").models == ["m1"]

    def test_reload_success_prunes_backoff_state(self):
        manager, builder, recovery, _ = build(
            [("discovery", ConnectionError("down"))], interval=60.0
        )
        seed_startup_failure(manager, LMSTUDIO)

        assert recovery.recover_once() == 1
        assert recovery._backoff

        # Simulate /admin/reload rebuilding the provider successfully.
        manager.record_registration(
            "lmstudio",
            provider_name=LMSTUDIO.provider_name,
            status="registered",
            stage="runtime",
            enabled=True,
        )

        assert recovery.recover_once() == 0
        assert recovery._backoff == {}

        # A later failure is retried immediately, not suppressed by the
        # stale backoff window.
        manager.record_registration(
            "lmstudio",
            provider_name=LMSTUDIO.provider_name,
            status="discovery_failed",
            stage="model_discovery",
            enabled=True,
            error_kind="network",
        )
        assert recovery.recover_once() == 1

    def test_disabled_provider_is_never_recovered(self, monkeypatch):
        monkeypatch.setattr(settings, "lmstudio_enabled", False)
        manager, builder, recovery, _ = build(
            [("ok", ["m1"])], interval=0.05
        )
        seed_startup_failure(manager, LMSTUDIO)

        assert recovery.recover_once() == 0
        assert builder.calls == []


class TestMetricsAndVisibility:
    def test_recovery_outcomes_are_counted(self):
        manager, _, recovery, clock = build(
            [("discovery", ConnectionError("down")), ("ok", ["m1"])],
            interval=5.0,
            clock=FakeClock(),
        )
        seed_startup_failure(manager, LMSTUDIO)

        failed_before = relay_metrics.provider_recovery_attempts.value(
            provider="lmstudio", outcome="failed"
        )
        recovered_before = relay_metrics.provider_recovery_attempts.value(
            provider="lmstudio", outcome="recovered"
        )

        assert recovery.recover_once() == 1
        clock.advance(5.0)
        assert recovery.recover_once() == 1

        assert (
            relay_metrics.provider_recovery_attempts.value(
                provider="lmstudio", outcome="failed"
            )
            - failed_before
            == 1
        )
        assert (
            relay_metrics.provider_recovery_attempts.value(
                provider="lmstudio", outcome="recovered"
            )
            - recovered_before
            == 1
        )


class TestRecoveryDefaults:
    def test_default_settings_are_on_with_bounded_backoff(self):
        assert settings.provider_recovery_enabled is True
        assert settings.provider_recovery_interval_seconds == 30
        assert settings.provider_recovery_max_interval_seconds == 600

    def test_relay_exposes_inert_provider_recovery(self, monkeypatch):
        monkeypatch.setattr(settings, "lmstudio_enabled", False)
        relay = Relay()

        assert isinstance(relay.provider_recovery, ProviderRecovery)
        assert relay.provider_recovery.is_running is False

    def test_relay_recovery_uses_configured_intervals(self, monkeypatch):
        monkeypatch.setattr(
            settings, "provider_recovery_interval_seconds", 77
        )
        monkeypatch.setattr(
            settings, "provider_recovery_max_interval_seconds", 770
        )
        monkeypatch.setattr(settings, "lmstudio_enabled", False)

        relay = Relay()

        assert relay.provider_recovery._base_interval == 77.0
        assert relay.provider_recovery._max_interval == 770.0
