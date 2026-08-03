from app.providers.openai_compat_client import OpenAICompatibleClient


class NvidiaClient(OpenAICompatibleClient):
    """
    Sends chat requests to NVIDIA via the shared OpenAI-compatible client.
    """

    def __init__(self) -> None:
        super().__init__(name="NVIDIA")
