from pydantic import BaseModel, Field, model_validator
from typing import Optional, Union, List

from app.schemas.openai import MAX_REQUEST_STRING_CHARS, _validate_request_shape


class ChatRequest(BaseModel):
    message: str = Field(max_length=MAX_REQUEST_STRING_CHARS)
    task: str | None = Field(default=None, max_length=1024)
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = Field(default=None, ge=1, le=1_000_000)
    stop: Optional[Union[str, List[str]]] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    seed: Optional[int] = None

    @model_validator(mode="before")
    @classmethod
    def _bounded_request_shape(cls, value):
        return _validate_request_shape(value)


class ChatResponse(BaseModel):
    provider: str
    model: str
    response: str
