# DEPRECATED (P6.3): thin import-compatibility facade only.
#
# No runtime code imports this module: the provider registry
# (``app.providers.registry``) and the registry-driven factory
# (``app.providers.factory``) resolve NVIDIA providers straight to
# ``app.providers.nvidia_client`` and ``build_runtime_provider``. The only
# consumers are tests. Keep this module only until those test imports are
# re-pointed onto the registry; it is slated for removal in a later cleanup
# release.

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
