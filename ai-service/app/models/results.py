from pydantic import BaseModel, Field


class EmbeddingResult(BaseModel):
    """Internal contract from an embedding provider: the vector + provenance."""

    vector: list[float]
    dimensions: int
    model: str
    provider: str


class Usage(BaseModel):
    """Token accounting, mirrors the OpenAI usage shape."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class CompletionResult(BaseModel):
    """Internal contract from an LLM provider."""

    reply: str
    model: str
    provider: str
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str = "stop"