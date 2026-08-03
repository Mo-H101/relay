from app.core.config import settings
from app.providers.base import Provider, apply_model_priority
from app.providers.openai_client import OpenAIClient


def create_provider() -> Provider:
    """
    Create and return the OpenAI provider.

    Available models are discovered dynamically from the API. If discovery
    fails, the provider is still returned (with no models) so it remains
    registered rather than crashing Relay startup.
    """

    provider = Provider(
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key=settings.openai_api_key,
        priority=5,
    )

    if provider.has_api_key():
        try:
            models = OpenAIClient().list_models(provider)
        except Exception:
            models = []

        provider.models = apply_model_priority(
            models,
            settings.openai_model_priority,
        )
        provider.priority_models = [
            model
            for model in settings.openai_model_priority
            if model in provider.models
        ]

    return provider
