from app.providers.nvidia_client import NvidiaClient
from app.providers.openai_client import OpenAIClient
from app.providers.lmstudio_client import LMStudioClient
from app.providers.anthropic_client import AnthropicClient
from app.providers.gemini_client import GeminiClient
from app.providers.ollama_client import OllamaClient


class ClientRegistry:
    """
    Maps provider names to their corresponding client implementation.
    """

    def __init__(self) -> None:
        self._clients = {
            "NVIDIA": NvidiaClient(),
            "OpenAI": OpenAIClient(),
            "LM Studio": LMStudioClient(),
            "Anthropic": AnthropicClient(),
            "Google Gemini": GeminiClient(),
            "Ollama": OllamaClient(),
        }

    def get(self, provider_name: str):
        client = self._clients.get(provider_name)

        if client is None:
            raise RuntimeError(
                f"No client registered for provider '{provider_name}'."
            )

        return client