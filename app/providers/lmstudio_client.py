from app.providers.openai_compat_client import OpenAICompatibleClient


class LMStudioClient(OpenAICompatibleClient):
    """
    Sends chat requests to LM Studio via the shared OpenAI-compatible
    client. LM Studio is a local endpoint with an optional API key.
    """

    def __init__(self) -> None:
        super().__init__(name="LM Studio")
