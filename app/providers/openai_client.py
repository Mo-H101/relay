from app.providers.openai_compat_client import OpenAICompatibleClient


class OpenAIClient(OpenAICompatibleClient):
    """
    Sends chat requests to OpenAI via the shared OpenAI-compatible client.
    """

    def __init__(self) -> None:
        super().__init__(name="OpenAI")
