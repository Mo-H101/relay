from typing import Optional
import logging
import threading

from app.services.provider_manager import ProviderManager

_logger = logging.getLogger("relay")


class HealthRefresher:
    """
    Periodically runs health checks against every enabled provider and
    stores the results through the health checker's store.

    Runs on a single daemon thread. Injectable and inert until start()
    is called, so tests can drive it with fakes and the default
    configuration never spawns a background thread.
    """

    def __init__(
        self,
        provider_manager: ProviderManager,
        health_checker,
        interval_seconds: int = 300,
        deep: bool = False,
    ) -> None:
        self._provider_manager = provider_manager
        self._health_checker = health_checker
        self._interval_seconds = max(1, int(interval_seconds))
        self._deep = deep
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def refresh_once(self) -> int:
        """
        Run one check pass over all enabled providers.

        Returns the number of providers checked.
        """
        providers = self._provider_manager.enabled()

        for provider in providers:
            self._health_checker.check(provider, deep=self._deep)

        return len(providers)

    def start(self) -> None:
        """
        Begin the periodic refresh loop. Safe to call multiple times.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return

            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="health-refresher",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """
        Signal the loop to stop and wait for the current pass to finish.
        """
        self._stop.set()

        with self._lock:
            thread = self._thread

        if thread is not None:
            thread.join(timeout=self._interval_seconds + 5)

    @property
    def is_running(self) -> bool:
        """
        Whether the background loop is currently alive.
        """
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self.refresh_once()
            except Exception:
                _logger.exception(
                    "health refresh pass failed; continuing with the "
                    "previous health snapshot"
                )
