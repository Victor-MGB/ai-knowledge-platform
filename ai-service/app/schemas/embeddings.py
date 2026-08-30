from pydantic import BaseModel, Field

from ..models.results import Usage


class EmbeddingRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=8192)
    model: str | None = None  # optional override; local providers ignore it


class EmbeddingData(BaseModel):
    object: str = "embedding"
    embedding: list[float]
    index: int = 0


class EmbeddingResponse(BaseModel):
    object: str = "list"
    data: list[EmbeddingData]
    model: str
    provider: str
    dimensions: int
    usage: Usage = Field(default_factory=Usage)