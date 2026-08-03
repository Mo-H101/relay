from app.core.config import settings
from app.providers.base import Provider, apply_model_priority
from app.providers.lmstudio_client import LMStudioClient


def create_provider() -> Provider:
    """
    Create and return the LM Studio provider.

    LM Studio is a local OpenAI-compatible endpoint. Unlike cloud
    providers it does not require an API key, so model discovery runs
    regardless of key configuration. If discovery fails, the provider is
    still returned (with no models) so it remains registered rather than
    crashing Relay startup.
    """

    provider = Provider(
        name="LM Studio",
        base_url=settings.lmstudio_base_url,
        api_key=settings.lmstudio_api_key,
        priority=settings.lmstudio_priority,
        requires_api_key=False,
    )

    try:
        models = LMStudioClient().list_models(provider)
    except Exception:
        models = []

    provider.models = apply_model_priority(
        models,
        settings.lmstudio_model_priority,
    )
    provider.priority_models = [
        model
        for model in settings.lmstudio_model_priority
        if model in provider.models
    ]

    return provider
