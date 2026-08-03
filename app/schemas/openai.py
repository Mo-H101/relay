"""
OpenAI-compatible request/response schemas for Relay's /v1 API layer.

The request schema models the OpenAI Chat Completions surface so Relay
can act as a drop-in gateway: full message-structure passthrough
(system/user/assistant/tool, multi-turn, tool_call_id, multimodal
content parts), first-class tool calling, stream_options, and stable
response shapes. Serialization is verbatim: fields the caller did not
send are not invented, and explicitly-null content is preserved on
assistant tool-call messages.
"""
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field
import uuid
import time


class ContentPart(BaseModel):
    """A single multimodal content part (text, image_url, ...)."""

    model_config = ConfigDict(extra="allow")

    type: str
    text: Optional[str] = None
    image_url: Optional[Any] = None


class FunctionCall(BaseModel):
    name: str
    arguments: str


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: FunctionCall


class ChatMessageObject(BaseModel):
    role: Literal["system", "assistant", "user", "tool"]
    content: Optional[Union[str, List[ContentPart]]] = None
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None

    def to_wire(self) -> dict:
        """
        Serialize exactly what the caller sent: only explicitly-provided
        fields are emitted, and content is kept even when explicitly null
        (semantic on assistant tool-call messages).
        """
        data: Dict[str, Any] = {}

        if "role" in self.model_fields_set:
            data["role"] = self.role
        if "name" in self.model_fields_set:
            data["name"] = self.name
        if "content" in self.model_fields_set:
            content = self.content
            if isinstance(content, list):
                data["content"] = [
                    part.model_dump(exclude_none=True) for part in content
                ]
            else:
                data["content"] = content
        if "tool_call_id" in self.model_fields_set:
            data["tool_call_id"] = self.tool_call_id
        if "tool_calls" in self.model_fields_set and self.tool_calls is not None:
            data["tool_calls"] = [
                tc.model_dump(exclude_none=True) for tc in self.tool_calls
            ]

        return data


class FunctionDefinition(BaseModel):
    name: str
    description: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None


class ToolDefinition(BaseModel):
    type: Literal["function"] = "function"
    function: FunctionDefinition


class OpenAIChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessageObject]
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: bool = False
    stop: Optional[Union[str, List[str]]] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    seed: Optional[int] = None
    user: Optional[str] = None
    tools: Optional[List[ToolDefinition]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    stream_options: Optional[Dict[str, Any]] = None

    def to_provider_payload(self) -> dict:
        """
        Build the wire payload sent to the upstream provider.

        Only fields the caller explicitly set are forwarded, so the
        message array reaches the provider verbatim with no invented
        defaults.
        """
        payload: Dict[str, Any] = {}
        payload["messages"] = [
            message.to_wire() for message in self.messages
        ]

        for field in (
            "model",
            "temperature",
            "top_p",
            "max_tokens",
            "stream",
            "stop",
            "frequency_penalty",
            "presence_penalty",
            "seed",
            "user",
            "tool_choice",
            "stream_options",
        ):
            if field in self.model_fields_set:
                payload[field] = getattr(self, field)

        if "tools" in self.model_fields_set and self.tools is not None:
            payload["tools"] = [
                tool.model_dump(exclude_none=True) for tool in self.tools
            ]

        return payload


class ChatMessageResponse(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None


class ChoiceObject(BaseModel):
    index: int = 0
    message: ChatMessageResponse
    finish_reason: Literal["stop", "length", "tool_calls", "content_filter", "function_call"] = "stop"


class OpenAIChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChoiceObject]
    usage: dict = Field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})


class ModelObject(BaseModel):
    id: str
    object: Literal["model"] = "model"
    owned_by: str


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: List[ModelObject]
