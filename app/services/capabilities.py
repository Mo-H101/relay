from enum import Enum


class ModelCapability(str, Enum):
    """
    Capability of a model, used to decide whether it can be health-checked
    via chat completion probes.
    """

    CHAT = "chat"
    VISION = "vision"
    EMBEDDING = "embedding"
    SAFETY = "safety"
    TRANSLATION = "translation"
    REWARD = "reward"
    PARSER = "parser"
    DETECTOR = "detector"
    IMAGE = "image"
    VIDEO = "video"
    UNKNOWN = "unknown"


CHAT_TESTABLE = {ModelCapability.CHAT, ModelCapability.VISION}

_CAPABILITY_RULES = [
    ("parser", ("parse",)),
    ("image", ("diffusion",)),
    ("video", ("cosmos",)),
    ("detector", ("detector",)),
    ("reward", ("reward",)),
    ("translation", ("translate",)),
    ("safety", ("guard", "safety", "topic-control")),
    ("embedding", ("embed", "bge", "retriever", "rerank", "clip")),
    ("vision", ("vision", "vlm", "neva", "kosmos", "fuyu", "deplot", "vila", "omni", "vl")),
]


def detect_capability(model_id: str) -> ModelCapability:
    """
    Classify a model id into a ModelCapability by its name.
    """

    model = model_id.lower()

    for capability, keywords in _CAPABILITY_RULES:
        if any(keyword in model for keyword in keywords):
            return ModelCapability(capability)

    return ModelCapability.CHAT


def is_chat_testable(model_id: str) -> bool:
    """
    Whether the model can be probed with a chat completion request.
    """

    return detect_capability(model_id) in CHAT_TESTABLE
