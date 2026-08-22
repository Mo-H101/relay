import pytest
from pydantic import ValidationError

from app.models.chat import ChatRequest
from app.schemas.openai import (
    MAX_MESSAGES,
    MAX_REQUEST_NESTING_DEPTH,
    MAX_REQUEST_STRING_CHARS,
    MAX_TOOLS,
    OpenAIChatCompletionRequest,
)


def _message(content="hello"):
    return {"role": "user", "content": content}


def test_openai_request_rejects_oversized_arbitrary_field():
    with pytest.raises(ValidationError, match="request string exceeds"):
        OpenAIChatCompletionRequest(
            model="a-1",
            messages=[_message("x" * (MAX_REQUEST_STRING_CHARS + 1))],
        )


def test_openai_request_rejects_deeply_nested_tool_parameters():
    nested = "value"
    for _ in range(MAX_REQUEST_NESTING_DEPTH + 1):
        nested = {"next": nested}

    with pytest.raises(ValidationError, match="request nesting exceeds"):
        OpenAIChatCompletionRequest(
            model="a-1",
            messages=[_message()],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "parameters": nested,
                    },
                }
            ],
        )


def test_openai_request_rejects_excessive_tool_catalog():
    tools = [
        {
            "type": "function",
            "function": {"name": f"tool-{index}"},
        }
        for index in range(MAX_TOOLS + 1)
    ]

    with pytest.raises(ValidationError, match="request array exceeds"):
        OpenAIChatCompletionRequest(
            model="a-1",
            messages=[_message()],
            tools=tools,
        )


def test_openai_request_retains_established_message_limit():
    with pytest.raises(
        ValidationError, match=f"request array exceeds the {MAX_MESSAGES}-item limit"
    ):
        OpenAIChatCompletionRequest(
            model="a-1",
            messages=[_message()] * (MAX_MESSAGES + 1),
        )


def test_legacy_chat_rejects_oversized_message():
    with pytest.raises(
        ValidationError,
        match=f"request string exceeds the {MAX_REQUEST_STRING_CHARS}-character limit",
    ):
        ChatRequest(message="x" * (MAX_REQUEST_STRING_CHARS + 1))


def test_legacy_chat_rejects_deeply_nested_unknown_input():
    nested = "value"
    for _ in range(MAX_REQUEST_NESTING_DEPTH + 1):
        nested = {"next": nested}

    with pytest.raises(ValidationError, match="request nesting exceeds"):
        ChatRequest(message="hello", metadata=nested)
