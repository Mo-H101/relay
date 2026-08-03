"""
Feedback request/response models (Phase 7D).

The feedback schema is intentionally minimal and strictly validated:
metadata only (provider, model, rating, optional category and correlation
id). ``extra="forbid"`` rejects any payload carrying prompt/message/
response/content (or any other undeclared field) with HTTP 422 before it
can reach the store.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

RATING_MIN = 1
RATING_MAX = 5


class FeedbackCategory(str, Enum):
    """
    Optional category label for a quality rating.
    """

    QUALITY = "quality"
    SPEED = "speed"
    ACCURACY = "accuracy"
    CLARITY = "clarity"
    RELEVANCE = "relevance"
    OTHER = "other"


class FeedbackRequest(BaseModel):
    """
    Metadata-only user quality feedback for one (provider, model).
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    rating: int = Field(ge=RATING_MIN, le=RATING_MAX)
    category: FeedbackCategory | None = None
    correlation_id: str | None = None
