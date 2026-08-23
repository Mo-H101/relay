"""
Background recovery for providers whose startup discovery failed (F1).

A provider that fails model discovery while Relay is starting (for
example a local LM Studio server that is still booting) used to stay
demoted until the next process restart: the registration recorded
``discovery_failed`` with an empty model catalog and nothing ever retried
it, so routing silently skipped the provider forever.

``ProviderRecovery`` closes that gap:

- A single daemon worker thread re-runs the same registry-driven factory
  used at startup, so a recovered provider is indistinguishable from a
  normally-started one (fresh key/base-url resolution, model priority,
  bounded catalogs).
- Per-provider exponential backoff (interval doubling up to a cap) keeps
  a permanently-down upstream from turning into a discovery storm.
- Successful discovery re-registers the provider through the manager,
  which atomically swaps in the fresh object and flips the registration
  status back to ``registered``; failed attempts refresh the safe
  registration classification and grow the backoff.
- Healthy providers are never touched, disabled providers are never
  recovered, and passes are serialized so manual and background passes
  can never duplicate discovery work concurrently.

The service is injectable and inert until ``start()``: tests drive single
passes deterministically, and default construction never spawns a thread.
"""

import logging
import threading
import time
from typing import Callable, Dict, Optional

from app.core.config import settings
from app.providers.factory import build_runtime_provider_detailed
from app.providers.registry import PROVIDER_REGISTRY, RUNTIME_READY
from app.services.failure_classifier import classify
from app.services.metrics import relay_metrics
from app.services.provider_manager import ProviderManager

_logger = logging.getLogger("relay")

# Registration statuses that qualify for background rediscovery. These are
# exactly the startup outcomes recorded by Relay._load_providers when the
# provider object exists but could not be made routable.
RETRYABLE_STATUSES = frozenset({"discovery_failed", "initialization_failed"})


class ProviderRecovery:
    """
    Periodically retries model discovery for providers that failed it at
    startup, until they become routable again.
    """

    def __init__(
        self,
        provider_manager: ProviderManager,
        interval_seconds: float = 30,
        max_interval_seconds: float = 600,
        builder: Optional[Callable] = None,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider_manager = provider_manager
        self._builder = builder or build_runtime_provider_detailed
        self._time_fn = time_fn
        self._base_interval = max(float(interval_seconds), 0.001)
        self._max_interval = max(
            float(max_interval_seconds), self._base_interval
        )
        # How often the idle loop wakes to notice newly-failed providers.
        # Bounded well below the retry interval so a failure observed mid
        # -cycle is picked up on the next tick without busy-spinning.
        self._scan_seconds = min(2.0, max(0.05, self._base_interval / 4))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # Serializes passes: the worker thread and any manual
        # recover_once() call never run discovery concurrently.
        self._pass_lock = threading.RLock()
        # provider_id -> {"failures": int, "next_due": float}. Only ever
        # mutated under _pass_lock.
        self._backoff: Dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Single pass
    # ------------------------------------------------------------------

    def recover_once(self) -> int:
        """
        Run one recovery pass over currently-failed providers.

        Returns the number of rediscovery attempts actually made;
        providers still inside their backoff window are skipped. Never
        raises: a failing factory or manager surfaces as a refreshed
        failure classification instead.
        """
        with self._pass_lock:
            now = self._time_fn()
            candidates = self._failed_candidates()

            # Prune state for providers that no longer qualify (recovered
            # via reload, disabled, or removed) so stale backoff entries
            # cannot suppress or mis-time future retries.
            for provider_id in list(self._backoff):
                if provider_id not in candidates:
                    del self._backoff[provider_id]

            attempts = 0
            for provider_id in sorted(candidates):
                state = self._backoff.get(provider_id)
                if state is not None and now < state["next_due"]:
                    continue

                attempts += 1
                defn = candidates[provider_id]
                try:
                    provider, discovery_error = self._builder(defn)
                except Exception as exc:  # noqa: BLE001
                    self._record_failure(
                        provider_id,
                        defn.provider_name,
                        status="initialization_failed",
                        stage="runtime",
                        error_kind=classify(exc).value,
                    )
                    self._schedule_backoff(provider_id, now)
                    continue

                if discovery_error is None:
                    # Re-register through the manager: swaps in the fresh
                    # provider object atomically and flips the recorded
                    # registration status back to "registered".
                    self._provider_manager.register(provider)
                    self._backoff.pop(provider_id, None)
                    relay_metrics.provider_recovery_attempts.inc(
                        provider=provider_id, outcome="recovered"
                    )
                    _logger.info(
                        "provider recovery: %s rediscovered successfully",
                        provider_id,
                    )
                else:
                    self._record_failure(
                        provider_id,
                        defn.provider_name,
                        status="discovery_failed",
                        stage="model_discovery",
                        error_kind=classify(discovery_error).value,
                    )
                    self._schedule_backoff(provider_id, now)

            return attempts

    def _failed_candidates(self) -> dict:
        """
        Registry definitions for runtime-ready, settings-enabled providers
        whose current registration status qualifies for retry.
        """
        statuses = {
            entry["id"]: entry
            for entry in self._provider_manager.registration_status()
        }

        candidates = {}
        for provider_id in RUNTIME_READY:
            defn = PROVIDER_REGISTRY[provider_id]
            if not getattr(settings, defn.enabled_attr, False):
                continue

            entry = statuses.get(provider_id)
            if entry is None:
                continue
            if not entry.get("enabled"):
                continue
            if entry.get("status") not in RETRYABLE_STATUSES:
                continue

            candidates[provider_id] = defn

        return candidates

    def _record_failure(
        self,
        provider_id: str,
        provider_name: str,
        *,
        status: str,
        stage: str,
        error_kind: str,
    ) -> None:
        """Refresh the safe registration classification for a failure."""
        try:
            self._provider_manager.record_registration(
                provider_id,
                provider_name=provider_name,
                status=status,
                stage=stage,
                enabled=True,
                error_kind=error_kind,
            )
        except Exception:  # noqa: BLE001 - visibility must never break recovery
            _logger.exception(
                "provider recovery: recording failure for %s failed",
                provider_id,
            )

        relay_metrics.provider_recovery_attempts.inc(
            provider=provider_id, outcome="failed"
        )

    def _schedule_backoff(self, provider_id: str, now: float) -> None:
        """
        Grow the per-provider retry delay exponentially, capped at the
        configured maximum, so repeated failures cannot storm discovery.
        """
        state = self._backoff.setdefault(
            provider_id, {"failures": 0, "next_due": now}
        )
        state["failures"] += 1
        delay = min(
            self._base_interval * (2 ** (state["failures"] - 1)),
            self._max_interval,
        )
        state["next_due"] = now + delay

    # ------------------------------------------------------------------
    # Background loop lifecycle (mirrors HealthRefresher)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Begin the periodic recovery loop. Safe to call multiple times.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return

            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="provider-recovery",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """
        Signal the loop to stop and wait for the in-flight pass to finish.
        Safe to call without start() and safe to call repeatedly.
        """
        self._stop.set()

        with self._lock:
            thread = self._thread

        if thread is not None:
            thread.join(timeout=min(self._base_interval, 60.0) + 5.0)

    @property
    def is_running(self) -> bool:
        """
        Whether the background loop is currently alive.
        """
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.wait(self._poll_seconds()):
            try:
                self.recover_once()
            except Exception:  # noqa: BLE001 - the loop must survive bugs
                _logger.exception(
                    "provider recovery pass failed; continuing"
                )

    def _poll_seconds(self) -> float:
        """
        Sleep until the nearest due retry, bounded by the scan interval so
        newly-failed providers are noticed promptly.
        """
        now = self._time_fn()

        with self._pass_lock:
            due_times = [
                state["next_due"] - now
                for state in self._backoff.values()
                if state["next_due"] > now
            ]

        if not due_times:
            return self._scan_seconds

        return max(0.01, min(min(due_times), self._scan_seconds))
