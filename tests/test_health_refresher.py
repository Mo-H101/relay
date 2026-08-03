import time

import pytest

from app.core.config import settings
from app.core.relay import Relay
from app.providers.base import Provider
from app.services.health_refresher import HealthRefresher
from app.services.provider_manager import ProviderManager


def make_provider(name, models):
    return Provider(
        name=name,
        base_url=f"https://{name.lower()}.invalid",
        api_key="test-key",
        enabled=True,
        priority=1,
        models=list(models),
    )


class FakeChecker:
    def __init__(self):
        self.calls = []

    def check(self, provider, deep=False):
        self.calls.append((provider.name, deep))


def build(providers=None, interval=300, deep=False):
    manager = ProviderManager()

    for provider in providers or []:
        manager.register(provider)

    checker = FakeChecker()

    refresher = HealthRefresher(
        provider_manager=manager,
        health_checker=checker,
        interval_seconds=interval,
        deep=deep,
    )

    return manager, checker, refresher


class TestHealthRefresher:
    def test_refresh_once_calls_checker_for_each_provider(self):
        _, checker, refresher = build(
            [make_provider("A", ["a-1"]), make_provider("B", ["b-1"])]
        )

        count = refresher.refresh_once()

        assert count == 2
        assert checker.calls == [("A", False), ("B", False)]

    def test_refresh_once_skips_disabled_providers(self):
        manager = ProviderManager()
        manager.register(make_provider("A", ["a-1"]))
        manager.register(
            Provider(
                name="B",
                base_url="https://b.invalid",
                api_key="key",
                enabled=False,
                models=["b-1"],
            )
        )
        checker = FakeChecker()
        refresher = HealthRefresher(manager, checker)

        count = refresher.refresh_once()

        assert count == 1
        assert checker.calls == [("A", False)]

    def test_deep_flag_forwarded_to_checker(self):
        _, checker, refresher = build(
            [make_provider("A", ["a-1"])], deep=True
        )

        refresher.refresh_once()

        assert checker.calls == [("A", True)]

    def test_not_running_before_start(self):
        _, _, refresher = build([make_provider("A", ["a-1"])])

        assert refresher.is_running is False

    def test_start_launches_loop_and_interval_drives_passes(self):
        _, checker, refresher = build(
            [make_provider("A", ["a-1"])], interval=1
        )

        refresher.start()

        try:
            assert refresher.is_running is True
            time.sleep(2.4)
        finally:
            refresher.stop()

        assert len(checker.calls) >= 2

    def test_graceful_stop_halts_further_passes(self):
        _, checker, refresher = build(
            [make_provider("A", ["a-1"])], interval=1
        )

        refresher.start()
        refresher.stop()

        assert refresher.is_running is False

        calls_after_stop = len(checker.calls)
        time.sleep(1.5)

        assert len(checker.calls) == calls_after_stop

    def test_start_is_idempotent(self):
        _, checker, refresher = build(
            [make_provider("A", ["a-1"])], interval=1
        )

        refresher.start()
        refresher.start()

        assert refresher.is_running is True

        refresher.stop()

    def test_stop_without_start_is_safe(self):
        _, _, refresher = build([make_provider("A", ["a-1"])])

        refresher.stop()

        assert refresher.is_running is False


class TestRefresherDefaults:
    def test_relay_does_not_start_refresher_when_disabled(self):
        relay = Relay()

        assert relay.health_refresher.is_running is False

    def test_default_settings_are_off(self):
        assert settings.health_refresh_enabled is False
        assert settings.health_deep_refresh_enabled is False

    def test_relay_refresher_uses_configured_interval(self, monkeypatch):
        monkeypatch.setattr(settings, "health_refresh_interval_seconds", 60)

        relay = Relay()

        assert relay.health_refresher._interval_seconds == 60
