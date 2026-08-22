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
from app.services.provider_key_store import provider_key_store


def _settings_value(attr: str | None, default):
    """
    Read one settings attribute, falling back to ``default`` when the
    attribute is absent or falsy (empty string).
    """
    if not attr:
        return default

    return getattr(settings, attr, default) or default


def resolve_provider_key(defn: ProviderDefinition, source=None) -> str:
    """
    Resolve the effective API key for a provider (P5 Phase 2).

    Precedence: keyring-stored key for ``defn.id`` when keyring is enabled
    and an entry exists; otherwise the settings/env value; otherwise empty
    string. ``source`` selects the Settings instance to read the fallback
    from (defaults to the global ``settings``) so reload can pass a fresh
    validated Settings while factory uses the singleton. With keyring
    disabled this returns exactly the settings value.
    """
    src = source if source is not None else settings

    if not defn.key_attr:
        return ""

    if getattr(src, "relay_keyring_enabled", False):
        try:
            stored = provider_key_store.get(defn.id)
        except Exception:
            stored = ""

        if stored:
            return stored

    return getattr(src, defn.key_attr, "") or ""


def build_runtime_provider_detailed(
    defn: ProviderDefinition,
) -> tuple[Provider, Exception | None]:
    """
    Create a provider and retain a model-discovery failure for the caller.

    The exception is returned only for in-process classification. Callers
    must expose a safe status rather than the exception text.
    """
    base_url = _settings_value(defn.base_url_attr, None) or defn.base_url_default

    provider = defn.build_provider(
        api_key=resolve_provider_key(defn),
        base_url=base_url,
    )

    provider.priority = _settings_value(
        defn.priority_attr,
        defn.runtime_priority,
    )

    discovery_error = None

    if provider.has_api_key() or not provider.requires_api_key:
        try:
            models = defn.client().list_models(provider)
        except Exception as exc:
            discovery_error = exc
            models = []

        priority = _settings_value(defn.priority_env.lower(), [])
        provider.models = apply_model_priority(models, priority)
        provider.priority_models = [
            model for model in priority if model in provider.models
        ]

    return provider, discovery_error


def build_runtime_provider(defn: ProviderDefinition) -> Provider:
    """Create a runtime provider, preserving the legacy return contract."""
    provider, _ = build_runtime_provider_detailed(defn)
    return provider
