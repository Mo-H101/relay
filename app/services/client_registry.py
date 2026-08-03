from app.providers.nvidia_client import NvidiaClient
from app.providers.openai_client import OpenAIClient
from app.providers.lmstudio_client import LMStudioClient


class ClientRegistry:
    """
    Maps provider names to their corresponding client implementation.
    """

    def __init__(self) -> None:
        self._clients = {
            "NVIDIA": NvidiaClient(),
            "OpenAI": OpenAIClient(),
            "LM Studio": LMStudioClient(),
        }

    def get(self, provider_name: str):
        client = self._clients.get(provider_name)

        if client is None:
            raise RuntimeError(
                f"No client registered for provider '{provider_name}'."
            )

        return client