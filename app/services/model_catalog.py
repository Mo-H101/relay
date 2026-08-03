"""
Model capability catalog (Phase 7A).

Maps a model id to a per-task compatibility score in [0, 1] using a
deterministic fallback chain:

1. exact id match
2. longest family-prefix match ("gpt-4o-mini" matches "gpt-4o-mini-2024-07-18")
3. keyword fallback on the model id (vision/translate/code ...)

Unknown models resolve to the neutral compatibility so they are never
penalized or rewarded. The catalog is read-only and stateless: it never
stores messages, requests, or any runtime data.
"""

from dataclasses import dataclass
from typing import Dict, Optional

from app.services.routing import TASK_CATEGORIES

NEUTRAL_COMPATIBILITY = 0.5


def _default_compat(default: float = NEUTRAL_COMPATIBILITY, **overrides) -> Dict[str, float]:
    """
    Build a per-task compatibility dict for a profile.
    """
    compat = {task: default for task in TASK_CATEGORIES}
    compat.update(overrides)
    return compat


@dataclass(frozen=True)
class ModelProfile:
    """
    Compatibility profile for a model family (or an exact model id).

    ``model`` is the exact id or family prefix used for matching;
    ``compatibility`` maps a task category to a score in [0, 1].
    """

    model: str
    family: str
    compatibility: Dict[str, float]


MODEL_CATALOG_SEED = (
    ModelProfile(
        model="gpt-3.5-turbo",
        family="gpt-3.5",
        compatibility=_default_compat(
            coding=0.85,
            reasoning=0.6,
            general=0.75,
            creative=0.6,
            translation=0.5,
            vision=0.1,
        ),
    ),
    ModelProfile(
        model="gpt-4o",
        family="gpt-4o",
        compatibility=_default_compat(
            coding=0.85,
            reasoning=0.7,
            general=0.8,
            creative=0.6,
            translation=0.55,
            vision=0.9,
        ),
    ),
    ModelProfile(
        model="gpt-4o-mini",
        family="gpt-4o",
        compatibility=_default_compat(
            coding=0.75,
            reasoning=0.65,
            general=0.8,
            creative=0.55,
            translation=0.5,
            vision=0.5,
        ),
    ),
    ModelProfile(
        model="gpt-5.5",
        family="gpt-5",
        compatibility=_default_compat(
            coding=0.9,
            reasoning=0.9,
            general=0.9,
            creative=0.8,
            translation=0.7,
            vision=0.65,
        ),
    ),
    ModelProfile(
        model="gpt-5.6",
        family="gpt-5",
        compatibility=_default_compat(
            coding=0.9,
            reasoning=0.9,
            general=0.9,
            creative=0.85,
            translation=0.75,
            vision=0.7,
        ),
    ),
)

# Keyword fallback profiles for models not covered by the seed families.
_KEYWORD_PROFILES = {
    "vision": _default_compat(vision=0.9, general=0.4),
    "translation": _default_compat(translation=0.9, general=0.4),
    "reasoning": _default_compat(reasoning=0.9, general=0.4),
    "creative": _default_compat(creative=0.9, general=0.4),
    "coding": _default_compat(coding=0.9, general=0.4),
}

_KEYWORD_RULES = (
    ("vision", ("vision", "vlm", "kosmos", "neva", "deplot", "omni", "vl")),
    ("translation", ("translate", "nmt")),
    ("reasoning", ("reasoning", "think", "math")),
    ("creative", ("creative", "story", "poem")),
    ("coding", ("code", "coder", "coding")),
)


class ModelCatalog:
    """
    Read-only catalog resolving a model id to a compatibility profile.
    """

    def __init__(self, profiles=MODEL_CATALOG_SEED) -> None:
        self._profiles = tuple(profiles)
        self._by_exact: Dict[str, ModelProfile] = {
            profile.model.lower(): profile for profile in self._profiles
        }

    def lookup(self, model: str) -> Optional[ModelProfile]:
        """
        Resolve a model id to a profile via the exact -> family ->
        keyword fallback chain, or None when nothing matches.
        """
        model_id = (model or "").strip().lower()

        if not model_id:
            return None

        exact = self._by_exact.get(model_id)

        if exact is not None:
            return exact

        family = self._match_family(model_id)

        if family is not None:
            return family

        return self._match_keywords(model_id)

    def score(self, model: str, task: str | None = None) -> float:
        """
        Compatibility of a model for a task in [0, 1].

        Unknown models and unknown tasks resolve to the neutral value.
        """
        profile = self.lookup(model)

        if profile is None:
            return NEUTRAL_COMPATIBILITY

        return profile.compatibility.get(
            (task or "general").strip().lower(),
            NEUTRAL_COMPATIBILITY,
        )

    def _match_family(self, model: str) -> Optional[ModelProfile]:
        best: Optional[ModelProfile] = None
        best_len = 0

        for profile in self._profiles:
            key = profile.model.lower()

            if model.startswith(key + "-") and len(key) > best_len:
                best = profile
                best_len = len(key)

        return best

    def _match_keywords(self, model: str) -> Optional[ModelProfile]:
        for category, keywords in _KEYWORD_RULES:
            if any(keyword in model for keyword in keywords):
                return ModelProfile(
                    model=category,
                    family=category,
                    compatibility=dict(_KEYWORD_PROFILES[category]),
                )

        return None


model_catalog = ModelCatalog()
