from datetime import UTC, datetime
from typing import List, Optional

from app.services.candidate_builder import RankedCandidate
from app.services.scoring import (
    BAND_DEGRADED,
    BAND_HEALTHY,
    BAND_NOT_CHECKED,
    BAND_UNAVAILABLE,
)

BAND_LABELS = {
    BAND_HEALTHY: "healthy",
    BAND_DEGRADED: "degraded",
    BAND_NOT_CHECKED: "not_checked",
    BAND_UNAVAILABLE: "unavailable",
}

_SIGNAL_LABELS = [
    ("priority", "lower priority contribution"),
    ("success", "lower success-rate contribution"),
    ("latency", "worse average-latency contribution"),
    ("failure", "higher failure penalty"),
    ("preference", "lower task-preference contribution"),
    ("task_compatibility", "lower task-compatibility contribution"),
    ("adaptive_reliability", "lower adaptive reliability contribution"),
    ("adaptive_latency", "worse adaptive-latency contribution"),
    ("quality", "lower quality-feedback contribution"),
    ("cost", "higher cost contribution"),
]


class ExplanationService:
    """
    Generates structured, human-readable explanations of routing
    decisions from ranking information.

    Consumes:
    - candidate ranking information (CandidateBuilder.ranked_candidates)
    - health signals (health band/status on each ranked candidate)
    - telemetry/scoring details (score breakdown on each candidate)

    Produces a structured explanation of why a provider/model was
    selected, why the other candidates ranked lower, and which signals
    affected the ranking. Pure observation: it never changes routing,
    ordering, or any store.
    """

    def explain(
        self,
        ranked: List[RankedCandidate],
        task: Optional[str] = None,
        health_aware: bool = True,
    ) -> dict:
        """
        Build the structured explanation response.
        """
        selected = ranked[0] if ranked else None

        candidates = [
            {
                "provider": candidate.provider,
                "model": candidate.model,
                "rank": candidate.rank,
                "score_breakdown": candidate.breakdown,
                "reasons": self._reasons(
                    candidate,
                    selected,
                    health_aware,
                ),
            }
            for candidate in ranked
        ]

        return {
            "selected": (
                {
                    "provider": selected.provider,
                    "model": selected.model,
                }
                if selected is not None
                else None
            ),
            "candidates": candidates,
            "task": task,
            "generated_at": datetime.now(UTC).isoformat(),
        }

    def _reasons(
        self,
        candidate: RankedCandidate,
        selected: Optional[RankedCandidate],
        health_aware: bool,
    ) -> List[str]:
        reasons = [
            f"Health band: {self._band_label(candidate)}."
        ]

        if not health_aware:
            reasons.append(
                "Health-aware routing disabled; health data shown for "
                "reference only."
            )

        reasons.append(self._telemetry_reason(candidate.telemetry))
        reasons.append(self._preference_reason(candidate.preference))

        if selected is not None and candidate is not selected:
            reasons.append(self._lower_reason(candidate, selected))
        else:
            reasons.append("Selected: best-ranked candidate.")

        return reasons

    def _lower_reason(
        self,
        candidate: RankedCandidate,
        selected: RankedCandidate,
    ) -> str:
        if candidate.health_band != selected.health_band:
            return (
                f"Ranks lower than selected ({selected.provider}/"
                f"{selected.model}): worse health band "
                f"({self._band_label(candidate)} vs "
                f"{self._band_label(selected)})."
            )

        diffs = []

        for key, label in _SIGNAL_LABELS:
            gap = selected.breakdown[key] - candidate.breakdown[key]

            if gap > 1e-4:
                diffs.append((gap, key, label))

        if not diffs:
            return (
                f"Tied with selected ({selected.provider}/"
                f"{selected.model}); input order preserved."
            )

        diffs.sort(key=lambda item: item[0], reverse=True)

        parts = [
            f"{label} ({candidate.breakdown[key]:.2f} vs "
            f"{selected.breakdown[key]:.2f})"
            for _, key, label in diffs[:2]
        ]

        return (
            f"Ranks lower than selected ({selected.provider}/"
            f"{selected.model}): " + "; ".join(parts) + "."
        )

    @staticmethod
    def _band_label(candidate: RankedCandidate) -> str:
        return BAND_LABELS.get(candidate.health_band, "unknown")

    @staticmethod
    def _telemetry_reason(telemetry) -> str:
        if telemetry is None:
            return "No telemetry recorded yet (cold start)."

        return (
            f"Telemetry: {telemetry.success_count}/{telemetry.request_count} "
            f"successful, avg {telemetry.average_latency_ms}ms latency, "
            f"{telemetry.failure_count} recent failures."
        )

    @staticmethod
    def _preference_reason(preference: Optional[int]) -> str:
        if preference is None:
            return "No task preference (provider/model priority order)."

        return f"Task preference: reference #{preference + 1}."
