from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import threading
import time

_MAX_FAILURE_HISTORY = 50

# Cap applied to the EWMA learning rate so no single observation can move
# an estimate by more than its full weight.
_MAX_EWMA_ALPHA = 1.0


@dataclass(frozen=True)
class FailureEvent:
    """
    A single recorded failure for a (provider, model) pair.
    """

    failure_type: str
    ts: float


@dataclass(frozen=True)
class TelemetryStats:
    """
    Read-only snapshot of telemetry for a (provider, model) pair.
    """

    provider: str
    model: str
    request_count: int
    success_count: int
    failure_count: int
    average_latency_ms: float
    recent_failures: List[FailureEvent]
    ewma_success: Optional[float] = None
    ewma_latency_ms: Optional[float] = None


@dataclass
class _Entry:
    request_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    total_latency_ms: int = 0
    recent_failures: deque = field(default_factory=lambda: deque())
    ewma_success: Optional[float] = None
    ewma_latency_ms: Optional[float] = None
    last_updated: float = 0.0


class TelemetryStore:
    """
    Thread-safe, in-memory operational telemetry keyed by
    (provider, model).

    Collects request/success/failure counts, average latency, a bounded
    recent failure history, and EWMA estimates of reliability and latency
    used by adaptive routing (Phase 7C). Data collection only: it does
    not itself influence routing or ordering.
    """

    def __init__(
        self,
        max_failure_history: int = _MAX_FAILURE_HISTORY,
        ewma_alpha: float = 0.1,
    ) -> None:
        self._max_failure_history = max_failure_history
        self._ewma_alpha = min(_MAX_EWMA_ALPHA, max(0.0, float(ewma_alpha)))
        self._lock = threading.Lock()
        self._entries: Dict[Tuple[str, str], _Entry] = {}

    def set_ewma_alpha(self, alpha: float) -> None:
        """
        Update the EWMA learning rate applied to future observations.
        The value is capped to [0, 1]; existing estimates are unchanged.
        """
        with self._lock:
            self._ewma_alpha = min(_MAX_EWMA_ALPHA, max(0.0, float(alpha)))

    def record_attempt(
        self,
        provider: str,
        model: str,
        success: bool,
        latency_ms: int = 0,
        failure_type: str | None = None,
    ) -> None:
        """
        Record a single chat attempt outcome.
        """
        with self._lock:
            entry = self._entries.setdefault((provider, model), _Entry())
            entry.request_count += 1
            entry.total_latency_ms += max(0, int(latency_ms))
            entry.last_updated = time.monotonic()

            latency = max(0.0, float(latency_ms))

            if entry.ewma_success is None:
                entry.ewma_success = 1.0 if success else 0.0
            else:
                outcome = 1.0 if success else 0.0
                entry.ewma_success += self._ewma_alpha * (
                    outcome - entry.ewma_success
                )

            if entry.ewma_latency_ms is None:
                entry.ewma_latency_ms = latency
            else:
                entry.ewma_latency_ms += self._ewma_alpha * (
                    latency - entry.ewma_latency_ms
                )

            if success:
                entry.success_count += 1
            else:
                entry.failure_count += 1
                entry.recent_failures.append(
                    FailureEvent(
                        failure_type=failure_type or "unknown",
                        ts=time.monotonic(),
                    )
                )
                if len(entry.recent_failures) > self._max_failure_history:
                    entry.recent_failures.popleft()

    def get(
        self,
        provider: str,
        model: str,
    ) -> Optional[TelemetryStats]:
        """
        Return a snapshot for a (provider, model) pair, or None when no
        attempts have been recorded.
        """
        with self._lock:
            entry = self._entries.get((provider, model))

            if entry is None:
                return None

            return self._snapshot(provider, model, entry)

    def recent_failures(
        self,
        provider: str,
        model: str,
        window_seconds: int | None = None,
    ) -> List[FailureEvent]:
        """
        Return recorded failures for a pair, newest first. With a window
        provided, only failures within the last window_seconds are kept.
        """
        with self._lock:
            entry = self._entries.get((provider, model))

            if entry is None:
                return []

            now = time.monotonic()
            events = list(entry.recent_failures)

            if window_seconds is not None:
                events = [
                    event for event in events if now - event.ts <= window_seconds
                ]

            return list(reversed(events))

    def all(self) -> List[TelemetryStats]:
        """
        Return snapshots for every (provider, model) pair with data.
        """
        with self._lock:
            return [
                self._snapshot(provider, model, entry)
                for (provider, model), entry in self._entries.items()
            ]

    def export_state(self) -> List[dict]:
        """
        Export all telemetry for persistence.

        Returns a list of per-(provider, model) dicts in StateStore
        format. Monotonic failure timestamps are converted to wall-clock
        timestamps so the export survives process restarts.
        """
        with self._lock:
            wall_now = time.time()
            mono_now = time.monotonic()
            result = []

            for (provider, model), entry in self._entries.items():
                failures = []

                for event in entry.recent_failures:
                    age = max(0.0, mono_now - event.ts)
                    failures.append(
                        {
                            "failure_type": event.failure_type,
                            "ts": wall_now - age,
                        }
                    )

                result.append(
                    {
                        "provider": provider,
                        "model": model,
                        "request_count": entry.request_count,
                        "success_count": entry.success_count,
                        "failure_count": entry.failure_count,
                        "total_latency_ms": entry.total_latency_ms,
                        "recent_failures": failures,
                        "ewma_success": entry.ewma_success,
                        "ewma_latency_ms": entry.ewma_latency_ms,
                        "last_updated_wall": (
                            wall_now
                            if entry.last_updated <= 0
                            else wall_now - max(0.0, mono_now - entry.last_updated)
                        ),
                    }
                )

            return result

    def import_state(self, entries: List[dict]) -> None:
        """
        Restore telemetry from an export (replacing any existing data).
        Wall-clock failure timestamps are converted back to monotonic
        timestamps and capped to the configured failure history bound.
        """
        with self._lock:
            self._entries.clear()
            wall_now = time.time()
            mono_now = time.monotonic()

            for data in entries:
                provider = data.get("provider")
                model = data.get("model")

                if not provider or not model:
                    continue

                entry = _Entry(
                    request_count=int(data.get("request_count", 0)),
                    success_count=int(data.get("success_count", 0)),
                    failure_count=int(data.get("failure_count", 0)),
                    total_latency_ms=int(data.get("total_latency_ms", 0)),
                    ewma_success=_opt_float(data.get("ewma_success")),
                    ewma_latency_ms=_opt_float(data.get("ewma_latency_ms")),
                )

                last_updated_wall = _opt_float(data.get("last_updated_wall"))
                if last_updated_wall is not None:
                    entry.last_updated = mono_now - max(0.0, wall_now - last_updated_wall)

                for event in data.get("recent_failures") or []:
                    try:
                        ts = float(event.get("ts"))
                    except (TypeError, ValueError):
                        continue

                    age = max(0.0, wall_now - ts)

                    entry.recent_failures.append(
                        FailureEvent(
                            str(event.get("failure_type") or "unknown"),
                            mono_now - age,
                        )
                    )

                    if len(entry.recent_failures) > self._max_failure_history:
                        entry.recent_failures.popleft()

                self._entries[(provider, model)] = entry

    def clear(self) -> None:
        """
        Remove all recorded telemetry.
        """
        with self._lock:
            self._entries.clear()

    def _snapshot(
        self,
        provider: str,
        model: str,
        entry: _Entry,
    ) -> TelemetryStats:
        average_latency = (
            entry.total_latency_ms / entry.request_count
            if entry.request_count
            else 0.0
        )

        return TelemetryStats(
            provider=provider,
            model=model,
            request_count=entry.request_count,
            success_count=entry.success_count,
            failure_count=entry.failure_count,
            average_latency_ms=round(average_latency, 2),
            recent_failures=list(reversed(entry.recent_failures)),
            ewma_success=(
                round(entry.ewma_success, 4)
                if entry.ewma_success is not None
                else None
            ),
            ewma_latency_ms=(
                round(entry.ewma_latency_ms, 2)
                if entry.ewma_latency_ms is not None
                else None
            ),
        )


def _opt_float(value) -> Optional[float]:
    """
    Coerce a persisted numeric value to float, tolerating None/absent
    keys so older exports (without EWMA fields) still import cleanly.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
