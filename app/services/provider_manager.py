from threading import RLock
from typing import Dict, List

from app.providers.base import Provider


class ProviderManager:
    """
    Stores and manages all configured providers.

    Thread-safe: the provider dict is guarded by a reentrant lock so a
    runtime registration (e.g. hot reload re-enabling an absent provider)
    never races with request-path iteration over the provider list.
    """

    def __init__(self) -> None:
        self._providers: Dict[str, Provider] = {}
        self._lock = RLock()

    def register(self, provider: Provider) -> None:
        """
        Register a provider.
        """
        with self._lock:
            self._providers[provider.name] = provider

    def get(self, name: str) -> Provider | None:
        """
        Retrieve a provider by name.
        """
        with self._lock:
            return self._providers.get(name)

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