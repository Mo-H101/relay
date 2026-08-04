"""
Registry-driven runtime provider construction (P4.1).

Every runtime provider is built from its ``ProviderDefinition`` so the
provider registry is the single source of runtime truth: adding a provider
is a registry entry, never a new factory branch. The per-provider factory
modules (``nvidia``, ``openai``, ``lmstudio``) are thin wrappers around
``build_runtime_provider`` kept for import compatibility.

Behavior mirrors the legacy factories exactly: cloud providers discover
models only when they have an API key; local providers (keyless) always
discover; discovery failures yield a provider with no models rather than
crashing startup; model priority is applied from settings.
"""

from app.core.config import settings
from app.providers.base import Provider, apply_model_priority
from app.providers.registry import ProviderDefinition


def _settings_value(attr: str | None, default):
    """
    Read one settings attribute, falling back to ``default`` when the
    attribute is absent or falsy (empty string).
    """
    if not attr:
        return default

    return getattr(settings, attr, default) or default


def build_runtime_provider(defn: ProviderDefinition) -> Provider:
    """
    Create and return the provider described by ``defn`` using the active
    settings (API key, base URL override, priority override) and live
    model discovery.
    """
    base_url = _settings_value(defn.base_url_attr, None) or defn.base_url_default

    provider = defn.build_provider(
        api_key=_settings_value(defn.key_attr, ""),
        base_url=base_url,
    )

    provider.priority = _settings_value(
        defn.priority_attr,
        defn.runtime_priority,
    )

    if provider.has_api_key() or not provider.requires_api_key:
        try:
            models = defn.client().list_models(provider)
        except Exception:
            models = []

        priority = _settings_value(defn.priority_env.lower(), [])
        provider.models = apply_model_priority(models, priority)
        provider.priority_models = [
            model for model in priority if model in provider.models
        ]

    return provider
