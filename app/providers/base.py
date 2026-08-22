from dataclasses import dataclass, field
from typing import Iterable, List


# A provider catalog is untrusted input. This is intentionally generous for
# real model registries while bounding memory, routing fan-out, health probe
# work, and /v1/models response cardinality.
MAX_PROVIDER_MODELS = 4096
MAX_MODEL_NAME_CHARS = 1024


@dataclass
class ModelProbe:
    """
    Result of probing a single model.
    """

    healthy: bool
    latency_ms: int
    status_code: int | None = None
    error: str = ""


@dataclass
class Provider:
    """
    Represents an AI provider.
    """

    name: str
    base_url: str
    health_endpoint: str = "/models"

    # Stable runtime identity (e.g. "nvidia"). Empty for providers built
    # without a registry definition; identity() then falls back to name so
    # legacy hand-built providers keep name-keyed behavior.
    id: str = ""

    api_key: str = ""
    enabled: bool = True
    priority: int = 0
    requires_api_key: bool = True

    # Proxy override. None defers to global PROXY_ENABLED + HTTP(S)_PROXY
    # handling; "" explicitly bypasses the proxy; a URL forces that proxy.
    proxy: str | None = None

    models: List[str] = field(default_factory=list)
    priority_models: List[str] = field(default_factory=list)

    def has_api_key(self) -> bool:
        return bool(self.api_key.strip())

    def identity(self) -> str:
        """
        Stable runtime identity used for lookups and keying.

        Returns ``id`` when set (registry-built providers) and falls back
        to ``name`` for legacy providers constructed without an id.
        """
        return self.id or self.name


def apply_model_priority(
    models: List[str],
    priority: List[str],
) -> List[str]:
    """
    Reorder models so priority-listed models come first.
    """

    models = bound_model_catalog(models)
    priority = bound_model_catalog(priority)

    if not priority:
        return models

    ordered = [
        model
        for model in priority
        if model in models
    ]

    remaining = [
        model
        for model in models
        if model not in ordered
    ]

    return ordered + remaining


def bound_model_catalog(
    models: Iterable[str] | None,
    max_models: int = MAX_PROVIDER_MODELS,
) -> List[str]:
    """Return a bounded, de-duplicated catalog of safe model identifiers."""
    if not models or max_models <= 0:
        return []

    bounded: List[str] = []
    seen = set()

    for model in models:
        if not isinstance(model, str) or not model:
            continue
        if len(model) > MAX_MODEL_NAME_CHARS or model in seen:
            continue
        seen.add(model)
        bounded.append(model)
        if len(bounded) >= max_models:
            break

    return bounded
