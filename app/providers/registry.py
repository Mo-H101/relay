"""
Provider definition registry.

The single source of truth describing every provider Relay can configure.
The setup wizard, key validation, config persistence, and (from P4) the
runtime wiring all read from this registry, so adding a provider is a
registry entry, never a new wizard branch.

Display vs. runtime name: ``display_name`` is the human-facing label in
the setup menu ("NVIDIA NIM", "LM Studio (local)"); ``provider_name`` is
the runtime identity used for ``Provider.name`` and the client registry
("NVIDIA", "LM Studio").
"""

from dataclasses import dataclass
from typing import List

from app.providers.anthropic_client import AnthropicClient
from app.providers.base import Provider
from app.providers.gemini_client import GeminiClient
from app.providers.lmstudio_client import LMStudioClient
from app.providers.nvidia_client import NvidiaClient
from app.providers.ollama_client import OllamaClient
from app.providers.openai_client import OpenAIClient


@dataclass(frozen=True)
class ProviderDefinition:
    """
    Static description of one configurable provider.
    """

    id: str
    display_name: str
    provider_name: str
    kind: str                      # "cloud" | "local"
    requires_api_key: bool
    key_env: str | None
    enabled_env: str
    key_attr: str | None
    enabled_attr: str
    base_url_env: str | None
    base_url_default: str
    priority_env: str | None
    health_endpoint: str
    client_class: type
    base_url_attr: str | None = None
    priority_attr: str | None = None
    runtime_priority: int = 0

    def client(self):
        """
        Build a setup-capable client instance for this provider.
        """
        return self.client_class()

    def build_provider(
        self,
        api_key: str = "",
        base_url: str | None = None,
    ) -> Provider:
        """
        Build a runtime Provider object.
        """
        return Provider(
            id=self.id,
            name=self.provider_name,
            base_url=base_url or self.base_url_default,
            api_key=api_key,
            enabled=True,
            priority=self.runtime_priority,
            requires_api_key=self.requires_api_key,
            health_endpoint=self.health_endpoint,
        )


PROVIDER_REGISTRY = {
    "nvidia": ProviderDefinition(
        id="nvidia",
        display_name="NVIDIA NIM",
        provider_name="NVIDIA",
        kind="cloud",
        requires_api_key=True,
        key_env="NVIDIA_API_KEY",
        enabled_env="NVIDIA_ENABLED",
        key_attr="nvidia_api_key",
        enabled_attr="nvidia_enabled",
        base_url_env="NVIDIA_BASE_URL",
        base_url_default="https://integrate.api.nvidia.com/v1",
        priority_env="NVIDIA_MODEL_PRIORITY",
        health_endpoint="/models",
        client_class=NvidiaClient,
        runtime_priority=10,
    ),
    "openai": ProviderDefinition(
        id="openai",
        display_name="OpenAI",
        provider_name="OpenAI",
        kind="cloud",
        requires_api_key=True,
        key_env="OPENAI_API_KEY",
        enabled_env="OPENAI_ENABLED",
        key_attr="openai_api_key",
        enabled_attr="openai_enabled",
        base_url_env="OPENAI_BASE_URL",
        base_url_default="https://api.openai.com/v1",
        priority_env="OPENAI_MODEL_PRIORITY",
        health_endpoint="/models",
        client_class=OpenAIClient,
        runtime_priority=5,
    ),
    "anthropic": ProviderDefinition(
        id="anthropic",
        display_name="Anthropic",
        provider_name="Anthropic",
        kind="cloud",
        requires_api_key=True,
        key_env="ANTHROPIC_API_KEY",
        enabled_env="ANTHROPIC_ENABLED",
        key_attr="anthropic_api_key",
        enabled_attr="anthropic_enabled",
        base_url_env="ANTHROPIC_BASE_URL",
        base_url_attr="anthropic_base_url",
        base_url_default="https://api.anthropic.com/v1",
        priority_env="ANTHROPIC_MODEL_PRIORITY",
        health_endpoint="/models",
        client_class=AnthropicClient,
        runtime_priority=8,
    ),
    "gemini": ProviderDefinition(
        id="gemini",
        display_name="Google Gemini",
        provider_name="Google Gemini",
        kind="cloud",
        requires_api_key=True,
        key_env="GEMINI_API_KEY",
        enabled_env="GEMINI_ENABLED",
        key_attr="gemini_api_key",
        enabled_attr="gemini_enabled",
        base_url_env="GEMINI_BASE_URL",
        base_url_attr="gemini_base_url",
        base_url_default="https://generativelanguage.googleapis.com/v1beta",
        priority_env="GEMINI_MODEL_PRIORITY",
        health_endpoint="/models",
        client_class=GeminiClient,
        runtime_priority=7,
    ),
    "lmstudio": ProviderDefinition(
        id="lmstudio",
        display_name="LM Studio (local)",
        provider_name="LM Studio",
        kind="local",
        requires_api_key=False,
        key_env="LMSTUDIO_API_KEY",
        enabled_env="LMSTUDIO_ENABLED",
        key_attr="lmstudio_api_key",
        enabled_attr="lmstudio_enabled",
        base_url_env="LMSTUDIO_BASE_URL",
        base_url_attr="lmstudio_base_url",
        base_url_default="http://localhost:1234/v1",
        priority_env="LMSTUDIO_MODEL_PRIORITY",
        priority_attr="lmstudio_priority",
        health_endpoint="/models",
        client_class=LMStudioClient,
        runtime_priority=1,
    ),
    "ollama": ProviderDefinition(
        id="ollama",
        display_name="Ollama (local)",
        provider_name="Ollama",
        kind="local",
        requires_api_key=False,
        key_env=None,
        enabled_env="OLLAMA_ENABLED",
        key_attr=None,
        enabled_attr="ollama_enabled",
        base_url_env="OLLAMA_BASE_URL",
        base_url_attr="ollama_base_url",
        base_url_default="http://localhost:11434",
        priority_env="OLLAMA_MODEL_PRIORITY",
        health_endpoint="/api/tags",
        client_class=OllamaClient,
        runtime_priority=2,
    ),
}

# Menu order matches the product spec ([1]..[6]).
PROVIDER_MENU: List[ProviderDefinition] = [
    PROVIDER_REGISTRY["nvidia"],
    PROVIDER_REGISTRY["openai"],
    PROVIDER_REGISTRY["anthropic"],
    PROVIDER_REGISTRY["gemini"],
    PROVIDER_REGISTRY["lmstudio"],
    PROVIDER_REGISTRY["ollama"],
]

# Providers wired into runtime chat routing. Setup may configure others,
# but only these are loaded by Relay and reloaded by /admin/reload until
# P4.2 wires the remaining clients into routing.
RUNTIME_READY = {
    "nvidia",
    "openai",
    "lmstudio",
    "ollama",
    "anthropic",
    "gemini",
}
