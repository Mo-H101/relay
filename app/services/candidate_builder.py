from dataclasses import dataclass
from typing import List, Optional, Tuple

from app.core.config import settings
from app.providers.base import Provider
from app.services.capabilities import is_chat_testable
from app.services.health_checker import (
    DEGRADED,
    HEALTHY,
    NOT_CHECKED,
    UNAVAILABLE,
)
from app.services.health_store import HealthStore
from app.services.model_catalog import ModelCatalog, model_catalog as default_catalog
from app.services.routing import RoutingEngine
from app.services.scoring import CandidateScorer, Rankable
from app.services.quality import QualityStore
from app.services.telemetry import TelemetryStats, TelemetryStore

_HEALTH_BANDS = {
    HEALTHY: 0,
    DEGRADED: 1,
    NOT_CHECKED: 2,
    UNAVAILABLE: 3,
}


@dataclass
class RankedCandidate:
    """
    A candidate in the final ranking with the signals that produced it.

    This is the observability surface for decision explanations: it
    carries the order (rank), the health band/status, the telemetry
    snapshot, the task preference index, and the scorer breakdown.
    """

    provider: str
    model: str
    rank: int
    health_band: int
    health_status: Optional[str]
    telemetry: Optional[TelemetryStats]
    preference: Optional[int]
    breakdown: dict


class CandidateBuilder:
    """
    Generates the ordered list of (provider, model) chat candidates.

    Responsibility split:
    - ProviderManager: provider-level selection (ranked, best, enabled).
    - RoutingEngine: resolves task-specific model preference refs.
    - HealthStore: latest per-provider health snapshots.
    - CandidateBuilder: turns a ranked provider list into concrete chat
      candidates, applying routing rules, chat-capability filtering, and
      (when enabled) health-aware filtering/ordering while preserving the
      existing candidate order.
    """

    def __init__(
        self,
        routing: RoutingEngine | None = None,
        health_store: HealthStore | None = None,
        telemetry: TelemetryStore | None = None,
        quality_store: QualityStore | None = None,
        config=None,
        catalog: ModelCatalog | None = None,
    ) -> None:
        self.routing = routing or RoutingEngine()
        self.health_store = health_store
        self.telemetry = telemetry
        self.quality_store = quality_store
        self._settings = config or settings
        self._catalog = catalog or default_catalog
        self._scorer = CandidateScorer(config=self._settings)

    def refresh_scorer(self) -> None:
        """
        Rebuild the CandidateScorer so scoring tuning changes from a hot
        configuration reload take effect.
        """
        self._scorer = CandidateScorer(config=self._settings)

    def build(
        self,
        providers: List[Provider],
        task: str | None = None,
        anchor: str | None = None,
    ) -> List[Tuple[Provider, str]]:
        """
        Return ordered chat candidates for the given ranked providers.

        When routing is enabled and the task has resolvable refs, those
        refs define the candidate set and preference order. With
        CROSS_PROVIDER_MODEL_SELECTION enabled, a bare model ref yields a
        candidate on every provider that contains it. Otherwise every
        chat-testable model across the providers is used in
        provider/model order.

        When health-aware routing is enabled, candidates are filtered to
        skip unavailable/unsupported models and reordered by provider
        health band (HEALTHY > DEGRADED > NOT_CHECKED > UNAVAILABLE),
        preserving task-reference preference and priority ordering within
        a band. Learned feedback state can demote providers and remove
        models. Health and telemetry only ever reorder the task's allowed
        candidates; unrelated models are never introduced. If filtering
        would remove every candidate, the original ordering is returned.

        P9B: ``anchor`` is the conversation's last committed logical
        model. When present, the plan is tiered: candidates carrying the
        anchor model (anchor tier) come first, the remaining normal
        routing output (fallback tier) second. Health/scoring may reorder
        within each tier but never across tiers; an anchor model that no
        provider can execute yields an empty anchor tier and the plan
        falls through to the fallback tier. With no anchor the plan is
        the unchanged pre-Phase 9B output.
        """

        candidates = self._initial_candidates(providers, task)

        if not self._settings.health_aware_routing:
            return [
                (provider, model)
                for provider, model, _ in self._order_plan(candidates, anchor)
            ]

        return [
            (provider, model)
            for provider, model, _ in self._health_plan(candidates, task, anchor)
        ]

    def ranked_candidates(
        self,
        providers: List[Provider],
        task: str | None = None,
        anchor: str | None = None,
    ) -> List[RankedCandidate]:
        """
        Return the decision ranking with full signal detail, in the same
        order `build()` produces (P9B: the same anchor-tiered plan). Used
        by the decision explanation endpoint; not part of the chat hot
        path, so it adds no runtime overhead when explanations are
        disabled.
        """
        candidates = self._initial_candidates(providers, task)

        if not self._settings.health_aware_routing:
            ordered = self._order_plan(candidates, anchor)
        else:
            ordered = self._health_plan(candidates, task, anchor)

        return self._rank_candidates(ordered, candidates, task)

    def rankables(
        self,
        providers: List[Provider],
        task: str | None = None,
        anchor: str | None = None,
    ) -> List[Rankable]:
        """
        Return the ordered Rankable candidates for the decision engine,
        in exactly the same order `build()` produces (P9B: the same
        anchor-tiered plan; health-aware filtering/ordering applied, or
        input order when health-aware routing is off). The DecisionEngine
        consumes these to produce explicit DecisionScore objects.
        """
        candidates = self._initial_candidates(providers, task)

        if self._settings.health_aware_routing:
            candidates = self._health_plan(candidates, task, anchor)
        else:
            candidates = self._order_plan(candidates, anchor)

        return self._rankables(candidates, task)

    def _initial_candidates(
        self,
        providers: List[Provider],
        task: str | None,
    ) -> List[Tuple[Provider, str, Optional[int]]]:
        routed = self.routing.candidates_weighted(task, providers)

        if routed:
            return routed

        return [
            (provider, model, None)
            for provider in providers
            for model in provider.models
            if is_chat_testable(model)
        ]

    # ------------------------- P9B anchor tiering -------------------------

    def _tiered(
        self,
        candidates: List[Tuple[Provider, str, Optional[int]]],
        anchor: Optional[str],
    ) -> Tuple[
        Optional[List[Tuple[Provider, str, Optional[int]]]],
        List[Tuple[Provider, str, Optional[int]]],
    ]:
        """
        Split the normal candidate output into the P9B tiers: the anchor
        tier (candidates for the conversation's last committed logical
        model, deduplicated) and the fallback pool (everything else).
        Returns ``(None, candidates)`` when no anchor is given.
        """
        if not anchor:
            return None, candidates

        anchor_tier: List[Tuple[Provider, str, Optional[int]]] = []
        fallback: List[Tuple[Provider, str, Optional[int]]] = []
        seen = set()

        for item in candidates:
            provider, model, _preference = item
            key = (provider.name, model)
            if key in seen:
                continue
            seen.add(key)
            if model == anchor:
                anchor_tier.append(item)
            else:
                fallback.append(item)

        return anchor_tier, fallback

    def _order_plan(
        self,
        candidates: List[Tuple[Provider, str, Optional[int]]],
        anchor: Optional[str],
    ) -> List[Tuple[Provider, str, Optional[int]]]:
        """
        Anchor tier first, fallback tier second, no health reordering
        (used when health-aware routing is off).
        """
        anchor_tier, fallback = self._tiered(candidates, anchor)
        if anchor_tier is None:
            return candidates
        return anchor_tier + fallback

    def _health_plan(
        self,
        candidates: List[Tuple[Provider, str, Optional[int]]],
        task: str | None,
        anchor: Optional[str],
    ) -> List[Tuple[Provider, str, Optional[int]]]:
        """
        Anchor tier first, fallback tier second, each tier health-filtered
        and ordered within the tier by the existing band/scoring logic.
        The anchor tier never falls back to its unfiltered input: an
        anchor model no provider can execute yields an empty anchor tier
        and the plan falls through to the fallback tier (P9B Case D).
        """
        if not anchor:
            return self._health_adjust(candidates, task)

        anchor_tier, fallback = self._tiered(candidates, anchor)
        anchor_ordered = self._health_adjust_strict(anchor_tier, task)
        fallback_ordered = self._health_adjust(fallback, task)
        return anchor_ordered + fallback_ordered

    def _rank_candidates(
        self,
        ordered: List[Tuple[Provider, str, Optional[int]]],
        candidates: List[Tuple[Provider, str, Optional[int]]],
        task: str | None = None,
    ) -> List[RankedCandidate]:
        reports, learned = self._reports_and_learned(candidates)

        rankables = self._rankables(ordered, reports, learned, task)

        by_provider = {
            provider.name: provider for provider, _, _ in candidates
        }

        ranked: List[RankedCandidate] = []

        for rank, rankable in enumerate(rankables, start=1):
            provider = by_provider[rankable.provider]
            report = reports.get(provider.name)
            telemetry = rankable.telemetry

            ranked.append(
                RankedCandidate(
                    provider=rankable.provider,
                    model=rankable.model,
                    rank=rank,
                    health_band=rankable.health_band,
                    health_status=(
                        report.status if report is not None else None
                    ),
                    telemetry=telemetry,
                    preference=rankable.preference,
                    breakdown=self._scorer.breakdown(rankable),
                )
            )

        return ranked

    def _reports_and_learned(
        self,
        candidates: List[Tuple[Provider, str, Optional[int]]],
    ) -> Tuple[dict, dict]:
        """Shared report/learned lookup for band computation."""
        reports = {}
        learned = {}

        if self.health_store is not None:
            for provider, _, _ in candidates:
                if provider.name not in reports:
                    reports[provider.name] = self.health_store.get(
                        provider.name
                    )
                    learned[provider.name] = self.health_store.learned(
                        provider.name
                    )

        return reports, learned

    def _health_adjust(
        self,
        candidates: List[Tuple[Provider, str, Optional[int]]],
        task: str | None = None,
    ) -> List[Tuple[Provider, str, Optional[int]]]:
        if self.health_store is None:
            return candidates

        filtered, reports, learned = self._health_filtered(candidates)

        if not filtered:
            return candidates

        return self._order_health(filtered, reports, learned, task)

    def _health_adjust_strict(
        self,
        candidates: List[Tuple[Provider, str, Optional[int]]],
        task: str | None = None,
    ) -> List[Tuple[Provider, str, Optional[int]]]:
        """
        Like ``_health_adjust`` but without the "return the original
        ordering when everything is filtered out" fallback: an empty
        result means every candidate is unavailable. Used for the P9B
        anchor tier so an unavailable anchor model falls through to the
        fallback tier instead of being force-retained.
        """
        if self.health_store is None:
            return candidates

        filtered, reports, learned = self._health_filtered(candidates)

        if not filtered:
            return []

        return self._order_health(filtered, reports, learned, task)

    def _health_filtered(
        self,
        candidates: List[Tuple[Provider, str, Optional[int]]],
    ) -> Tuple[
        List[Tuple[Provider, str, Optional[int]]],
        dict,
        dict,
    ]:
        """Health-filter candidates; returns (filtered, reports, learned)."""
        reports, learned = self._reports_and_learned(candidates)

        filtered = [
            (provider, model, preference)
            for provider, model, preference in candidates
            if self._keep(
                model,
                reports[provider.name],
                learned[provider.name],
            )
        ]

        return filtered, reports, learned

    def _order_health(
        self,
        filtered: List[Tuple[Provider, str, Optional[int]]],
        reports: dict,
        learned: dict,
        task: str | None = None,
    ) -> List[Tuple[Provider, str, Optional[int]]]:
        """Order already-filtered candidates by band, then by scorer."""
        has_signal = (
            self._has_telemetry(filtered) if self.telemetry is not None else False
        ) or self._has_quality(filtered)

        if not has_signal:
            return sorted(
                filtered,
                key=lambda pair: self._band(
                    reports[pair[0].name],
                    learned[pair[0].name],
                ),
            )

        return self._rank_with_scorer(filtered, reports, learned, task)

    def _has_telemetry(
        self,
        candidates: List[Tuple[Provider, str, Optional[int]]],
    ) -> bool:
        return any(
            self.telemetry.get(provider.name, model) is not None
            for provider, model, _ in candidates
        )

    def _has_quality(
        self,
        candidates: List[Tuple[Provider, str, Optional[int]]],
    ) -> bool:
        return any(
            self._quality_ewma(provider.name, model) is not None
            for provider, model, _ in candidates
        )

    def _rank_with_scorer(
        self,
        filtered: List[Tuple[Provider, str, Optional[int]]],
        reports: dict,
        learned: dict,
        task: str | None = None,
    ) -> List[Tuple[Provider, str, Optional[int]]]:
        rankables = self._rankables(filtered, reports, learned, task)

        ranked = self._scorer.rank(rankables)

        by_provider = {
            provider.name: provider for provider, _, _ in filtered
        }

        return [
            (by_provider[item.provider], item.model, item.preference)
            for item in ranked
        ]

    def _rankables(
        self,
        candidates: List[Tuple[Provider, str, Optional[int]]],
        reports: Optional[dict] = None,
        learned: Optional[dict] = None,
        task: str | None = None,
    ) -> List[Rankable]:
        """
        Build Rankable objects for the given (already ordered) candidates.
        Health bands are taken from the optional per-provider report/
        learned map; when omitted they are fetched here so both the hot
        path and the decision engine share identical band computation.
        """
        if reports is None or learned is None:
            reports, learned = self._reports_and_learned(candidates)

        return [
            Rankable(
                provider=provider.name,
                model=model,
                priority=provider.priority,
                health_band=self._band(
                    reports.get(provider.name),
                    learned.get(provider.name),
                ),
                health_status=(
                    reports[provider.name].status
                    if reports.get(provider.name) is not None
                    else None
                ),
                telemetry=(
                    self.telemetry.get(provider.name, model)
                    if self.telemetry is not None
                    else None
                ),
                preference=preference,
                task_compatibility=self._task_compatibility(model, task),
                quality_ewma=self._quality_ewma(provider.name, model),
                quality_confidence=self._quality_confidence(
                    provider.name, model
                ),
            )
            for provider, model, preference in candidates
        ]

    def _task_compatibility(self, model: str, task: str | None) -> Optional[float]:
        """
        Catalog compatibility for a candidate model, or None when the
        task catalog feature is disabled. The scorer gates the signal's
        contribution, so the value is collected here unconditionally but
        only ever affects ordering when the flag is on.
        """
        if not getattr(self._settings, "task_catalog_enabled", False):
            return None

        return self._catalog.score(model, task)

    def _quality_ewma(self, provider: str, model: str) -> Optional[float]:
        """
        Confidence-gated quality EWMA for a candidate model, or None when
        the quality store is absent or the pair has not reached
        quality_feedback_min_samples ratings. The scorer gates the
        signal's contribution, so the value is collected here
        unconditionally but only ever affects ordering when the flag is
        on and the pair is confident.
        """
        if self.quality_store is None:
            return None

        signal = self.quality_store.quality_signal(provider, model)

        if signal is None:
            return None

        return signal.score

    def _quality_confidence(self, provider: str, model: str) -> Optional[float]:
        """
        Confidence in the quality EWMA for a candidate model, or None when
        the quality store is absent or the pair has no ratings. Sample
        count is bounded (clamped) so it maps to [0, 1]; the candidate
        builder only uses this for decision-engine metadata, never for
        ordering (the scorer's confidence gate is independent).
        """
        if self.quality_store is None:
            return None

        signal = self.quality_store.quality_signal(provider, model)

        if signal is None:
            return None

        return min(1.0, signal.sample_count / 100.0)

    def _keep(
        self,
        model: str,
        report,
        learned_state,
    ) -> bool:
        if report is not None:
            if (
                model in report.unavailable_models
                or model in report.unsupported_models
            ):
                return False

        if learned_state is not None:
            if (
                model in learned_state.degraded_models
                or model in learned_state.unavailable_models
            ):
                return False

        return True

    def _band(self, report, learned_state) -> int:
        band = _HEALTH_BANDS[NOT_CHECKED]

        if report is not None:
            band = _HEALTH_BANDS.get(
                report.status,
                _HEALTH_BANDS[NOT_CHECKED],
            )

        if (
            learned_state is not None
            and learned_state.provider_status in _HEALTH_BANDS
        ):
            band = max(band, _HEALTH_BANDS[learned_state.provider_status])

        return band
