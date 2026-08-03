from app.core.config import settings
from app.providers.base import Provider, apply_model_priority
from app.providers.nvidia_client import NvidiaClient


def create_provider() -> Provider:
    """
    Create and return the NVIDIA provider.

    Available models are discovered dynamically from the API. If discovery
    fails, the provider is still returned (with no models) so it remains
    registered rather than crashing Relay startup.
    """

    provider = Provider(
        name="NVIDIA",
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=settings.nvidia_api_key,
        priority=10,
    )

    if provider.has_api_key():
        try:
            models = NvidiaClient().list_models(provider)
        except Exception:
            models = []

        provider.models = apply_model_priority(
            models,
            settings.nvidia_model_priority,
        )
        provider.priority_models = [
            model
            for model in settings.nvidia_model_priority
            if model in provider.models
        ]

    return provider