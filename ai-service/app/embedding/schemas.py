from pydantic import BaseModel


class DocumentEmbeddingRecord(BaseModel):
    """The `documents` row view the embedding pipeline acts on.

    `status` is the ingestion lifecycle (queued -> processing -> ready /
    failed, Days 9-10); `embedding_status` is the vectorization lifecycle
    (none -> processing -> ready / failed, Day 11). Retrieval (Day 15) will
    only serve documents where both read `ready`.
    """

    id: str
    organization_id: str
    status: str
    embedding_status: str
    embedding_error: str | None = None


class ChunkEmbedding(BaseModel):
    """One chunk waiting for its vector: id for storage, content for the model."""

    chunk_id: str
    content: str


class EmbeddingPipelineResult(BaseModel):
    """Outcome of one embed_document() run, for the API and the CLI."""

    document_id: str
    status: str  # embedded | failed | not_found | not_embeddable
    chunks: int = 0
    vectors: int = 0
    model: str = ""
    dimensions: int = 0
    detail: str = ""