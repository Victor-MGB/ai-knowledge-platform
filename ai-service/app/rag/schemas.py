"""Wire models for the RAG generation endpoint (/v1/rag/generate)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..models.results import Usage

MAX_CONTEXT_ITEMS = 10
MIN_CONTEXT_TOKENS = 50
MAX_CONTEXT_TOKENS = 2000
MAX_HISTORY_MESSAGES = 20
MAX_HISTORY_MESSAGE_CHARS = 1000


class ChatHistoryMessage(BaseModel):
    """Day 20 — one prior turn from the conversation. Only `user`/`assistant`
    roles are valid; the conversation layer already resolved any internal
    `system` notes out of the window before it reaches generation."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=4000)


class ContextItem(BaseModel):
    """One retrieved chunk. Text is the payload; the rest is provenance the
    answer's evidence can cite (and Day-18 citations will extend)."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=4000)
    similarity: float | None = Field(None, ge=0.0, le=1.0)
    section: str | None = Field(None, max_length=200)
    page: int | None = Field(None, ge=1)
    chunk_id: str | None = None
    document_id: str | None = None
    document_title: str | None = Field(None, max_length=300)


class RagGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., min_length=1, max_length=500, pattern=r"\S")
    context: list[ContextItem] = Field(
        default_factory=list, max_length=MAX_CONTEXT_ITEMS
    )
    history: list[ChatHistoryMessage] = Field(
        default_factory=list, max_length=MAX_HISTORY_MESSAGES
    )
    system_prompt: str | None = Field(None, max_length=2000)
    temperature: float = Field(0.0, ge=0.0, le=2.0)
    max_context_tokens: int | None = Field(
        None, ge=MIN_CONTEXT_TOKENS, le=MAX_CONTEXT_TOKENS
    )
    min_score: float | None = Field(None, ge=0.0, le=1.0)


class Evidence(BaseModel):
    """Which context item(s) an answer is grounded on. `index` is the item's
    position in the sorted context the generator handed the provider: 0 = the
    strongest evidence. Some fields are null when the caller supplied none."""

    index: int
    chunk_id: str | None = None
    document_id: str | None = None
    document_title: str | None = None
    section: str | None = None
    page: int | None = None
    similarity: float | None = None


class Citation(BaseModel):
    """Day 18 — one source the answer points at, keyed by the marker that
    appears inline in `answer` as `[id]`. `id` is the 1-based citation number
    matching that marker; `page` is the page reference for the source
    (page the chunk starts on), so a client can render:

        Sources
        [1] Employee Handbook — Page 14
    """

    id: int = Field(..., ge=1)
    title: str | None = Field(None, max_length=300)
    section: str | None = Field(None, max_length=200)
    page: int | None = Field(None, ge=1)
    chunk_id: str | None = None
    document_id: str | None = None
    similarity: float | None = Field(None, ge=0.0, le=1.0)


class RagGenerationResponse(BaseModel):
    answer: str
    refused: bool
    provider: str
    model: str
    evidence: list[Evidence] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)