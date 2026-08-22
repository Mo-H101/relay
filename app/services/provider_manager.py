from threading import RLock
from typing import Dict, List

from app.providers.base import Provider, bound_model_catalog
from app.providers.registry import PROVIDER_REGISTRY
from app.services.failure_classifier import FailureKind

# Legacy lookup: display-ish provider names → stable ids. Lets callers
# that still hold a provider name (e.g. the TUI) resolve a registry-built
# provider registered under its id.
_LEGACY_NAME_TO_ID = {
    defn.provider_name: defn.id for defn in PROVIDER_REGISTRY.values()
}


class ProviderManager:
    """
    Stores and manages all configured providers.

    Thread-safe: the provider dict is guarded by a reentrant lock so a
    runtime registration (e.g. hot reload re-enabling an absent provider)
    never races with request-path iteration over the provider list.

    Providers are keyed by ``identity()`` (stable id when set, name
    fallback for legacy hand-built providers). ``get`` also resolves a
    legacy provider name to its stable id, so both keys work.
    """

    def __init__(self) -> None:
        self._providers: Dict[str, Provider] = {}
        self._registration: Dict[str, dict] = {}
        self._lock = RLock()

    def register(self, provider: Provider) -> None:
        """
        Register a provider under its stable identity.
        """
        with self._lock:
            provider.models = bound_model_catalog(provider.models)
            provider.priority_models = bound_model_catalog(
                provider.priority_models
            )
            identity = provider.identity()
            self._providers[identity] = provider
            self._registration[identity] = {
                "id": identity,
                "name": provider.name,
                "status": "registered",
                "stage": "runtime",
                "enabled": bool(provider.enabled),
                "error_kind": None,
            }

    def record_registration(
        self,
        provider_id: str,
        *,
        provider_name: str,
        status: str,
        stage: str,
        enabled: bool,
        error_kind: str | None = None,
    ) -> None:
        """Record safe, bounded visibility for provider setup outcomes."""
        if status not in {
            "disabled",
            "registered",
            "initialization_failed",
            "discovery_failed",
        }:
            raise ValueError("invalid provider registration status")
        if stage not in {"configuration", "runtime", "model_discovery"}:
            raise ValueError("invalid provider registration stage")

        safe_error_kind = None
        if error_kind is not None:
            allowed_error_kinds = {kind.value for kind in FailureKind}
            safe_error_kind = (
                error_kind if error_kind in allowed_error_kinds else "unknown"
            )

        with self._lock:
            self._registration[provider_id] = {
                "id": provider_id,
                "name": provider_name,
                "status": status,
                "stage": stage,
                "enabled": bool(enabled),
                "error_kind": safe_error_kind,
            }

    def registration_status_for(self, provider_id: str) -> dict:
        """Return one safe registration status, or an empty mapping."""
        with self._lock:
            return dict(self._registration.get(provider_id) or {})

    def registration_status(self) -> list[dict]:
        """Return safe registration statuses in stable identity order."""
        with self._lock:
            return [
                dict(self._registration[provider_id])
                for provider_id in sorted(self._registration)
            ]

    def get(self, key: str) -> Provider | None:
        """
        Retrieve a provider by stable id (or legacy provider name).
        """
        with self._lock:
            provider = self._providers.get(key)

            if provider is None:
                provider = self._providers.get(
                    _LEGACY_NAME_TO_ID.get(key, key)
                )

            return provider

    def all(self) -> List[Provider]:
        """
        Return all registered providers.
        """
        with self._lock:
            return list(self._providers.values())

    def enabled(self) -> List[Provider]:
        """
        Return only enabled providers.
        """
        with self._lock:
            return [
                provider
                for provider in self._providers.values()
                if provider.enabled
            ]

    def ranked(self) -> List[Provider]:
        """
        Return enabled providers, best first.

        A provider is selectable when it has an API key or does not
        require one (e.g. a local endpoint like LM Studio). Cloud
        providers keep their key requirement, so their behavior is
        unchanged.
        """
        with self._lock:
            candidates = [
                provider
                for provider in self._providers.values()
                if provider.enabled
                and (provider.has_api_key() or not provider.requires_api_key)
            ]

            candidates.sort(
                key=lambda provider: provider.priority,
                reverse=True,
            )

            return candidates

    def best(self) -> Provider | None:
        """
        Return the highest-priority selectable provider.
        """
        ranked = self.ranked()

        return ranked[0] if ranked else None
