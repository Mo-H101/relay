import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, Optional, Tuple

from app.core.config import settings
from app.services.feedback import (
    DEGRADED,
    MODEL,
    PROVIDER,
    UNAVAILABLE,
    DEFAULT_DEGRADED_TTL_SECONDS,
    DEFAULT_UNAVAILABLE_TTL_SECONDS,
    MODEL_INVALID_REQUEST_UNAVAILABLE_THRESHOLD,
    MODEL_SERVER_ERROR_THRESHOLD,
    MODEL_TIMEOUT_DEGRADED_THRESHOLD,
    MODEL_TIMEOUT_UNAVAILABLE_THRESHOLD,
    MODEL_UNKNOWN_DEGRADED_THRESHOLD,
    PROVIDER_SERVER_ERROR_THRESHOLD,
    action_for,
)

if TYPE_CHECKING:
    from app.services.health_checker import ProviderHealth


@dataclass
class LearnedState:
    """
    Read-only view of learned feedback state for a provider.
    """

    provider_status: Optional[str] = None
    degraded_models: frozenset = field(default_factory=frozenset)
    unavailable_models: frozenset = field(default_factory=frozenset)


@dataclass
class _LearnedEntry:
    provider_status: Optional[str] = None
    provider_status_expires: float = 0.0
    model_marks: Dict[str, Dict[str, float]] = field(default_factory=dict)
    model_counts: Dict[str, Dict[str, Tuple[int, float]]] = field(
        default_factory=dict
    )
    provider_counts: Dict[str, Tuple[int, float]] = field(default_factory=dict)


class HealthStore:
    """
    Thread-safe, TTL-bounded cache of provider health.

    Two independent layers:
    - explicit snapshots produced by HealthChecker.check()
    - learned state derived from chat-outcome feedback
    """

    def __init__(
        self,
        ttl_seconds: int = 300,
        degraded_ttl_seconds: int = DEFAULT_DEGRADED_TTL_SECONDS,
        unavailable_ttl_seconds: int = DEFAULT_UNAVAILABLE_TTL_SECONDS,
        now=None,
        freshness_exponent: float = 1.0,
        model_server_error_threshold: int = MODEL_SERVER_ERROR_THRESHOLD,
        provider_server_error_threshold: int = PROVIDER_SERVER_ERROR_THRESHOLD,
        model_timeout_degraded_threshold: int = MODEL_TIMEOUT_DEGRADED_THRESHOLD,
        model_timeout_unavailable_threshold: int = (
            MODEL_TIMEOUT_UNAVAILABLE_THRESHOLD
        ),
        model_invalid_request_unavailable_threshold: int = (
            MODEL_INVALID_REQUEST_UNAVAILABLE_THRESHOLD
        ),
        model_unknown_degraded_threshold: int = MODEL_UNKNOWN_DEGRADED_THRESHOLD,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._degraded_ttl = degraded_ttl_seconds
        self._unavailable_ttl = unavailable_ttl_seconds
        self._freshness_exponent = float(freshness_exponent)
        self._model_server_error_threshold = model_server_error_threshold
        self._provider_server_error_threshold = provider_server_error_threshold
        self._model_timeout_degraded_threshold = model_timeout_degraded_threshold
        self._model_timeout_unavailable_threshold = (
            model_timeout_unavailable_threshold
        )
        self._model_invalid_request_unavailable_threshold = (
            model_invalid_request_unavailable_threshold
        )
        self._model_unknown_degraded_threshold = model_unknown_degraded_threshold
        self._lock = threading.Lock()
        self._snapshots: Dict[str, Tuple[float, "ProviderHealth"]] = {}
        self._learned: Dict[str, _LearnedEntry] = {}
        self._now = now or time.monotonic

    def refresh_thresholds(self) -> None:
        """
        Re-read TTL and feedback-threshold values from settings so a hot
        configuration reload takes effect. Existing snapshots and learned
        state are preserved.
        """
        with self._lock:
            self._ttl_seconds = getattr(
                settings, "health_ttl_seconds", self._ttl_seconds
            )
            self._degraded_ttl = getattr(
                settings,
                "health_degraded_ttl_seconds",
                self._degraded_ttl,
            )
            self._unavailable_ttl = getattr(
                settings,
                "health_unavailable_ttl_seconds",
                self._unavailable_ttl,
            )
            self._freshness_exponent = float(
                getattr(
                    settings,
                    "health_freshness_exponent",
                    self._freshness_exponent,
                )
            )
            self._model_server_error_threshold = getattr(
                settings,
                "health_feedback_model_server_error_threshold",
                self._model_server_error_threshold,
            )
            self._provider_server_error_threshold = getattr(
                settings,
                "health_feedback_provider_server_error_threshold",
                self._provider_server_error_threshold,
            )
            self._model_timeout_degraded_threshold = getattr(
                settings,
                "health_feedback_model_timeout_degraded_threshold",
                self._model_timeout_degraded_threshold,
            )
            self._model_timeout_unavailable_threshold = getattr(
                settings,
                "health_feedback_model_timeout_unavailable_threshold",
                self._model_timeout_unavailable_threshold,
            )
            self._model_invalid_request_unavailable_threshold = getattr(
                settings,
                "health_feedback_model_invalid_request_unavailable_threshold",
                self._model_invalid_request_unavailable_threshold,
            )
            self._model_unknown_degraded_threshold = getattr(
                settings,
                "health_feedback_model_unknown_degraded_threshold",
                self._model_unknown_degraded_threshold,
            )

    # ============================
    # Explicit snapshots
    # ============================

    def save(self, report: "ProviderHealth") -> None:
        """
        Store the latest health snapshot for a provider.
        """
        with self._lock:
            self._snapshots[report.name] = (self._now(), report)

    def get(self, provider_name: str) -> Optional["ProviderHealth"]:
        """
        Return the latest explicit snapshot for a provider, or None when
        absent or expired.
        """
        with self._lock:
            entry = self._snapshots.get(provider_name)

            if entry is None:
                return None

            checked_at, report = entry

            if self._now() - checked_at > self._ttl_seconds:
                return None

            return report

    def freshness(self, provider_name: str) -> float:
        """
        Decay factor for the current snapshot: 1.0 when just saved,
        fading to 0.0 at the TTL. The default exponent keeps it linear;
        a configurable exponent changes the decay curve. Returns 0.0 when
        absent or expired. This is read-only and does not affect get()
        semantics.
        """
        with self._lock:
            entry = self._snapshots.get(provider_name)

            if entry is None:
                return 0.0

            checked_at, _ = entry
            age = self._now() - checked_at

            if self._ttl_seconds <= 0:
                return 0.0

            if age > self._ttl_seconds:
                return 0.0

            ratio = 1.0 - (age / self._ttl_seconds)

            if ratio <= 0.0:
                return 0.0

            return ratio ** self._freshness_exponent

    # ============================
    # Learned feedback state
    # ============================

    def record_failure(
        self,
        provider_name: str,
        model: str,
        failure_type: str,
    ) -> None:
        """
        Record a chat failure and apply the corresponding feedback action.
        """
        with self._lock:
            entry = self._learned.setdefault(provider_name, _LearnedEntry())
            now = self._now()
            self._prune(entry, now)

            model_count, provider_count = self._bump(
                entry,
                model,
                failure_type,
                now,
            )

            action = action_for(
                failure_type,
                model_failures=model_count,
                provider_failures=provider_count,
                degraded_ttl=self._degraded_ttl,
                unavailable_ttl=self._unavailable_ttl,
                model_server_error_threshold=self._model_server_error_threshold,
                provider_server_error_threshold=self._provider_server_error_threshold,
                model_timeout_degraded_threshold=(
                    self._model_timeout_degraded_threshold
                ),
                model_timeout_unavailable_threshold=(
                    self._model_timeout_unavailable_threshold
                ),
                model_invalid_request_unavailable_threshold=(
                    self._model_invalid_request_unavailable_threshold
                ),
                model_unknown_degraded_threshold=(
                    self._model_unknown_degraded_threshold
                ),
            )

            self._apply(entry, model, action, now)

    def record_success(self, provider_name: str, model: str) -> None:
        """
        Clear learned degradation for a model after a successful chat.
        """
        with self._lock:
            entry = self._learned.get(provider_name)

            if entry is None:
                return

            entry.model_marks.pop(model, None)
            entry.model_counts.pop(model, None)
            entry.provider_status = None
            entry.provider_status_expires = 0.0

            if self._empty(entry):
                self._learned.pop(provider_name, None)

    def learned(self, provider_name: str) -> Optional[LearnedState]:
        """
        Return active learned state for a provider, or None when absent,
        expired, or already recovered.
        """
        with self._lock:
            entry = self._learned.get(provider_name)

            if entry is None:
                return None

            now = self._now()
            self._prune(entry, now)

            degraded = frozenset(
                model
                for model, marks in entry.model_marks.items()
                if DEGRADED in marks
            )
            unavailable = frozenset(
                model
                for model, marks in entry.model_marks.items()
                if UNAVAILABLE in marks
            )

            if (
                entry.provider_status is None
                and not degraded
                and not unavailable
            ):
                self._learned.pop(provider_name, None)
                return None

            return LearnedState(
                provider_status=entry.provider_status,
                degraded_models=degraded,
                unavailable_models=unavailable,
            )

    def export_learned_state(self) -> Dict[str, dict]:
        """
        Export active learned feedback state for persistence.

        Returns a dict keyed by provider in StateStore format. Expiry
        timestamps are converted to remaining seconds AND wall-clock
        expiries so elapsed downtime is accounted for on the next load.
        Entries already expired (or past their counter reset window) are
        dropped.
        """
        with self._lock:
            now = self._now()
            wall_now = time.time()
            window = self._degraded_ttl
            result = {}

            for provider, entry in self._learned.items():
                status = entry.provider_status
                status_remaining = None
                status_expires_wall = None

                if status is not None:
                    status_remaining = round(
                        max(0.0, entry.provider_status_expires - now),
                        6,
                    )

                    if status_remaining <= 0:
                        status_remaining = None
                    else:
                        status_expires_wall = wall_now + status_remaining

                model_marks = {}

                for model, marks in entry.model_marks.items():
                    rebuilt = {}

                    for mark_status, expires in marks.items():
                        remaining = round(expires - now, 6)

                        if remaining > 0:
                            rebuilt[mark_status] = [
                                remaining,
                                wall_now + remaining,
                            ]

                    if rebuilt:
                        model_marks[model] = rebuilt

                model_counts = {}

                for model, counts in entry.model_counts.items():
                    rebuilt = {}

                    for failure_type, (count, ts) in counts.items():
                        remaining = round(window - (now - ts), 6)

                        if remaining > 0:
                            rebuilt[failure_type] = [
                                count,
                                remaining,
                                wall_now - (now - ts),
                            ]

                    if rebuilt:
                        model_counts[model] = rebuilt

                provider_counts = {}

                for failure_type, (count, ts) in entry.provider_counts.items():
                    remaining = round(window - (now - ts), 6)

                    if remaining > 0:
                        provider_counts[failure_type] = [
                            count,
                            remaining,
                            wall_now - (now - ts),
                        ]

                if (
                    status_remaining is None
                    and not model_marks
                    and not model_counts
                    and not provider_counts
                ):
                    continue

                result[provider] = {
                    "provider_status": (
                        status if status_remaining is not None else None
                    ),
                    "provider_status_remaining_seconds": status_remaining,
                    "provider_status_expires_wall": status_expires_wall,
                    "model_marks": model_marks,
                    "model_counts": model_counts,
                    "provider_counts": provider_counts,
                }

            return result

    def import_learned_state(self, state: Dict[str, dict]) -> None:
        """
        Restore learned feedback state from an export (replacing any
        existing learned state). Wall-clock expiries are converted to
        monotonic expiry timers; anything already expired during
        downtime is dropped. Exports without wall-clock info fall back
        to the stored remaining seconds.
        """
        with self._lock:
            self._learned.clear()
            now = self._now()
            wall_now = time.time()
            window = self._degraded_ttl

            for provider, data in state.items():
                if not isinstance(provider, str) or not isinstance(data, dict):
                    continue

                entry = _LearnedEntry()
                status = data.get("provider_status")

                if status:
                    wall_expiry = data.get("provider_status_expires_wall")

                    if wall_expiry:
                        remaining = wall_expiry - wall_now

                        if remaining > 0:
                            entry.provider_status = status
                            entry.provider_status_expires = now + remaining
                    else:
                        remaining = data.get(
                            "provider_status_remaining_seconds"
                        )

                        if remaining and remaining > 0:
                            entry.provider_status = status
                            entry.provider_status_expires = now + remaining

                for model, marks in (data.get("model_marks") or {}).items():
                    rebuilt = {}

                    for mark_status, payload in (marks or {}).items():
                        expiry = self._import_expiry(payload, now, wall_now)

                        if expiry is not None:
                            rebuilt[mark_status] = expiry

                    if rebuilt:
                        entry.model_marks[model] = rebuilt

                for model, counts in (data.get("model_counts") or {}).items():
                    rebuilt = {}

                    for failure_type, payload in (counts or {}).items():
                        count, ts = self._import_count(
                            payload, now, wall_now, window
                        )

                        if count is not None:
                            rebuilt[failure_type] = (count, ts)

                    if rebuilt:
                        entry.model_counts[model] = rebuilt

                for failure_type, payload in (
                    data.get("provider_counts") or {}
                ).items():
                    count, ts = self._import_count(
                        payload, now, wall_now, window
                    )

                    if count is not None:
                        entry.provider_counts[failure_type] = (count, ts)

                self._learned[provider] = entry

    @staticmethod
    def _import_expiry(payload, now, wall_now):
        """
        Rebuild a monotonic expiry for a model mark. payload is either a
        [remaining, expires_wall] list or a bare remaining-seconds number
        (legacy format). Returns None when already expired.
        """
        if isinstance(payload, (list, tuple)) and len(payload) >= 2:
            wall_expiry = payload[1]

            if wall_expiry:
                remaining = wall_expiry - wall_now

                if remaining > 0:
                    return now + remaining

                return None

            payload = payload[0]

        if isinstance(payload, (list, tuple)) and payload:
            payload = payload[0]

        if not payload or payload <= 0:
            return None

        return now + payload

    @staticmethod
    def _import_count(payload, now, wall_now, window):
        """
        Rebuild a counter's (count, monotonic ts). payload is either a
        legacy [count, remaining] list or a [count, remaining,
        last_bump_wall] list. Returns (None, None) when the counter would
        already have reset.
        """
        if not isinstance(payload, (list, tuple)) or not payload:
            return None, None

        count = payload[0]

        if len(payload) >= 3 and payload[2]:
            age = max(0.0, wall_now - payload[2])

            if age >= window:
                return None, None

            return count, now - age

        remaining = payload[1] if len(payload) >= 2 else None

        if not remaining or remaining <= 0:
            return None, None

        return count, now + remaining - window

    def clear(self) -> None:
        """
        Remove all stored snapshots and learned state.
        """
        with self._lock:
            self._snapshots.clear()
            self._learned.clear()

    # ============================
    # Internals
    # ============================

    def _bump(self, entry, model, failure_type, now):
        window = self._degraded_ttl

        model_counts = entry.model_counts.setdefault(model, {})
        model_count = self._bump_counter(
            model_counts,
            failure_type,
            now,
            window,
        )

        provider_count = self._bump_counter(
            entry.provider_counts,
            failure_type,
            now,
            window,
        )

        return model_count, provider_count

    def _bump_counter(self, counts, key, now, window):
        current = counts.get(key)

        if current is None or now - current[1] > window:
            counts[key] = (1, now)
            return 1

        counts[key] = (current[0] + 1, now)
        return current[0] + 1

    def _apply(self, entry, model, action, now):
        if action.scope == PROVIDER:
            if action.effect in (DEGRADED, UNAVAILABLE):
                entry.provider_status = action.effect
                entry.provider_status_expires = now + action.ttl_seconds
            return

        if action.scope == MODEL and action.effect in (DEGRADED, UNAVAILABLE):
            marks = entry.model_marks.setdefault(model, {})
            marks[action.effect] = now + action.ttl_seconds

    def _prune(self, entry, now):
        if (
            entry.provider_status is not None
            and now - entry.provider_status_expires > 0
        ):
            entry.provider_status = None
            entry.provider_status_expires = 0.0

        for model in list(entry.model_marks):
            marks = entry.model_marks[model]
            expired = [status for status in marks if now - marks[status] > 0]

            for status in expired:
                del marks[status]

            if not marks:
                del entry.model_marks[model]

    def _empty(self, entry):
        return (
            entry.provider_status is None
            and not entry.model_marks
            and not entry.model_counts
            and not entry.provider_counts
        )
