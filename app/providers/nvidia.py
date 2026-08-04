from app.providers.base import Provider
from app.providers.factory import build_runtime_provider
from app.providers.registry import PROVIDER_REGISTRY


def create_provider() -> Provider:
    """
    Create and return the NVIDIA provider.

    Delegates to the registry-driven factory (P4.1) so the provider
    registry is the single source of runtime truth. Available models are
    discovered dynamically from the API; on discovery failure the provider
    is still returned (with no models) so it remains registered rather
    than crashing Relay startup.
    """
    return build_runtime_provider(PROVIDER_REGISTRY["nvidia"])
