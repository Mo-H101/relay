from pydantic import BaseModel
from typing import Optional, Union, List


class ChatRequest(BaseModel):
    message: str
    task: str | None = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stop: Optional[Union[str, List[str]]] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    seed: Optional[int] = None


class ChatResponse(BaseModel):
    provider: str
    model: str
    response: str