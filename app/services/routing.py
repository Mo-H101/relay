from typing import Dict, List, Tuple

from app.core.config import settings
from app.providers.base import Provider
from app.services.capabilities import is_chat_testable

TASK_CATEGORIES = (
    "coding",
    "vision",
    "reasoning",
    "general",
    "creative",
    "translation",
)


class RoutingEngine:
    """
    Resolves optional task-specific model preferences into an ordered
    list of (provider, model) candidates.

    Resolution honors CROSS_PROVIDER_MODEL_SELECTION:
    - disabled (default): a bare model reference matches only the first
      provider that contains it, preserving legacy behavior.
    - enabled: the same bare model reference becomes a candidate on every
      provider that contains it, so health/telemetry can compete.

    Explicit "ProviderName:model" references always resolve to exactly
    that provider and are unaffected by the flag.
    """

    def __init__(self, config=None) -> None:
        self._settings = config or settings

        self._preferences: Dict[str, List[str]] = {
            "coding": list(self._settings.task_coding),
            "vision": list(self._settings.task_vision),
            "reasoning": list(self._settings.task_reasoning),
            "general": list(self._settings.task_general),
            "creative": list(self._settings.task_creative),
            "translation": list(self._settings.task_translation),
        }

    def refresh(self) -> None:
        """
        Re-read task preferences and routing flags from settings, so a
        hot configuration reload takes effect without rebuilding the
        engine.
        """
        self._preferences = {
            "coding": list(self._settings.task_coding),
            "vision": list(self._settings.task_vision),
            "reasoning": list(self._settings.task_reasoning),
            "general": list(self._settings.task_general),
            "creative": list(self._settings.task_creative),
            "translation": list(self._settings.task_translation),
        }

    def is_enabled(self) -> bool:
        """
        Routing is active only when explicitly enabled.
        """
        return bool(self._settings.task_routing_enabled)

    def cross_provider_enabled(self) -> bool:
        """
        Whether a bare model reference may match multiple providers.
        """
        return bool(
            getattr(
                self._settings,
                "cross_provider_model_selection",
                False,
            )
        )

    def refs_for(self, task: str) -> List[str]:
        """
        Return the raw preference references for a task category.
        """
        return list(self._preferences.get(task, []))

    def resolve_weighted(
        self,
        refs: List[str],
        providers: List[Provider],
    ) -> List[Tuple[Provider, str, int]]:
        """
        Resolve preference references to (provider, model, ref_index)
        candidates. ref_index is the position of the reference that
        produced the candidate; earlier refs are preferred.

        A reference may be:
        - a bare model id, matched against providers in order
        - "ProviderName:model id", matched against a specific provider
        """

        pairs: List[Tuple[Provider, str, int]] = []
        seen = set()
        cross_provider = self.cross_provider_enabled()

        for index, ref in enumerate(refs):
            if ":" in ref:
                provider_name, model = ref.split(":", 1)

                provider = next(
                    (
                        candidate
                        for candidate in providers
                        if candidate.name.lower() == provider_name.lower()
                    ),
                    None,
                )

                if (
                    provider is not None
                    and model in provider.models
                    and is_chat_testable(model)
                    and (provider.name, model) not in seen
                ):
                    seen.add((provider.name, model))
                    pairs.append((provider, model, index))

                continue

            for provider in providers:
                if (
                    ref in provider.models
                    and is_chat_testable(ref)
                    and (provider.name, ref) not in seen
                ):
                    seen.add((provider.name, ref))
                    pairs.append((provider, ref, index))

                    if not cross_provider:
                        break

        return pairs

    def resolve(
        self,
        refs: List[str],
        providers: List[Provider],
    ) -> List[Tuple[Provider, str]]:
        """
        Resolve preference references to (provider, model) candidates.
        """
        return [
            (provider, model)
            for provider, model, _ in self.resolve_weighted(refs, providers)
        ]

    def candidates_weighted(
        self,
        task: str | None,
        providers: List[Provider],
    ) -> List[Tuple[Provider, str, int]]:
        """
        Return ordered (provider, model, ref_index) candidates for a
        task, or an empty list when routing should be skipped.
        """

        if not task or not self.is_enabled():
            return []

        refs = self.refs_for(task.strip().lower())

        if not refs:
            return []

        return self.resolve_weighted(refs, providers)

    def candidates(
        self,
        task: str | None,
        providers: List[Provider],
    ) -> List[Tuple[Provider, str]]:
        """
        Return ordered (provider, model) candidates for a task, or an
        empty list when routing should be skipped.
        """
        return [
            (provider, model)
            for provider, model, _ in self.candidates_weighted(
                task, providers
            )
        ]
