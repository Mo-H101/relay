from app.providers.registry import PROVIDER_REGISTRY


class ClientRegistry:
    """
    Maps provider identities to their corresponding client implementation.

    Keyed by stable provider id (P4.1). ``get`` also resolves a legacy
    provider name to its id, so both ``get("lmstudio")`` and the older
    ``get("LM Studio")`` callers work during the transition.
    """

    def __init__(self) -> None:
        self._by_id = {
            defn.id: defn.client() for defn in PROVIDER_REGISTRY.values()
        }
        self._by_name = {
            defn.provider_name: self._by_id[defn.id]
            for defn in PROVIDER_REGISTRY.values()
        }

    def get(self, key: str):
        client = self._by_id.get(key)

        if client is None:
            client = self._by_name.get(key)

        if client is None:
            raise RuntimeError(
                f"No client registered for provider '{key}'."
            )

        return client
