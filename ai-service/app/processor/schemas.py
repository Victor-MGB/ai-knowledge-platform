from pydantic import BaseModel, Field


class DocumentRecord(BaseModel):
    """A row from `documents`, as the processor needs it."""

    id: str
    organization_id: str
    title: str
    filename: str
    source_type: str
    storage_key: str | None
    status: str


class PageRecord(BaseModel):
    """One page of extracted text with the citation metadata preserved.

    `document_id` + `page_number` are the anchor; `section` is the structural
    heading (PDF outline title) that contains the page, when one exists;
    `source` is the object URI (s3://bucket/key) the text came from.
    """

    document_id: str
    organization_id: str
    page_number: int
    content: str
    token_count: int = 0
    section: str | None = None
    source: str | None = None


class ChunkRecord(BaseModel):
    """One row of `chunks` — the retrieval unit produced by chunking.

    A chunk is text that a chunking strategy traversed together. It preserves
    the citation chain: `page_number` is the page the chunk starts on, and
    `metadata` carries the section, page_range and s3:// source so a hit can
    be cited back to the source object (Day 18).
    """

    document_id: str
    organization_id: str
    chunk_index: int
    page_number: int
    content: str
    token_count: int = 0
    metadata: dict[str, object] = Field(default_factory=dict)


class ExtractResult(BaseModel):
    """Outcome of parsing one PDF's bytes."""

    pages: list[PageRecord] = Field(default_factory=list)
    page_errors: int = 0  # pages whose text streams failed, read as blank
    truncated: bool = False  # stuck the page_limit guard
    empty: bool = True  # nothing extractable (zero pages or all blank)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def total_tokens(self) -> int:
        return sum(p.token_count for p in self.pages)


class ProcessingResult(BaseModel):
    """What `processor.process(id)` did, for APIs and the CLI."""

    document_id: str
    status: str  # ready | failed | not_found | not_queued | unsupported_type
    pages: int = 0
    chunks: int = 0
    chunk_strategy: str = "paragraph"
    page_errors: int = 0
    total_tokens: int = 0
    empty: bool = False
    truncated: bool = False
    detail: str = ""