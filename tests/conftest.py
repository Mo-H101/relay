from app.core.config import settings

# The module-level Relay() singleton in app/core/relay.py performs network
# I/O (provider model discovery) at import time. Disable provider loading
# for the whole test session so importing app.main never hits the network.
settings.nvidia_enabled = False
settings.openai_enabled = False
settings.anthropic_enabled = False
settings.gemini_enabled = False
settings.lmstudio_enabled = False
settings.ollama_enabled = False
