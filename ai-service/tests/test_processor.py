from contextlib import contextmanager
from pathlib import Path

import pytest

from app.processor.downloader import ObjectNotFoundError
from app.processor.pdf import PdfExtractor
from app.processor.processor import ProcessorService
from app.processor.repository import DocumentRepository
from app.processor.schemas import ChunkRecord, DocumentRecord, PageRecord

FIXTURES = Path(__file__).parent / "fixtures"
PDF_BYTES = (FIXTURES / "three_pages.pdf").read_bytes()
CORRUPT_BYTES = (FIXTURES / "corrupt.pdf").read_bytes()


class FakeRepo:
    """In-memory DocumentRepository with a claim race condition built in."""

    def __init__(self, docs: dict[str, DocumentRecord]):
        self.docs = docs
        self.inserted: list[PageRecord] = []
        self.chunks: list[ChunkRecord] = []
        self.ready: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def get(self, document_id: str):
        return self.docs.get(document_id)

    def claim(self, document_id: str):
        doc = self.docs.get(document_id)
        if doc is None or doc.status != "queued":
            return None
        claimed = doc.model_copy(update={"status": "processing"})
        self.docs[document_id] = claimed
        return claimed

    def insert_pages(self, pages: list[PageRecord]):
        self.inserted.extend(pages)

    def insert_chunks(self, chunks: list[ChunkRecord]):
        self.chunks.extend(chunks)

    def mark_ready(self, document_id: str):
        self.ready.append(document_id)
        self.docs[document_id] = self.docs[document_id].model_copy(
            update={"status": "ready"}
        )

    def mark_failed(self, document_id: str, error: str):
        self.failed.append((document_id, error))
        self.docs[document_id] = self.docs[document_id].model_copy(
            update={"status": "failed"}
        )

    @contextmanager
    def transaction(self):
        yield


class FakeStore:
    def __init__(self, objects: dict[str, bytes], *, missing_key: str | None = None):
        self.objects = objects
        self.missing_key = missing_key

    def get(self, key: str) -> bytes:
        if self.missing_key == key:
            raise ObjectNotFoundError(f"knowflow/{key}: NoSuchKey")
        if key in self.objects:
            return self.objects[key]
        raise ObjectNotFoundError(f"knowflow/{key}: NoSuchKey")

    def reachable(self) -> bool:
        return True


def doc(
    document_id: str,
    *,
    status: str = "queued",
    source_type: str = "pdf",
    storage_key: str | None = "org-1/x.pdf",
) -> DocumentRecord:
    return DocumentRecord(
        id=document_id,
        organization_id="org-1",
        title="x",
        filename="x.pdf",
        source_type=source_type,
        storage_key=storage_key,
        status=status,
    )


def processor(repo, store) -> ProcessorService:
    return ProcessorService(repo, store, PdfExtractor(), bucket="knowflow")


def test_happy_path_extracts_chunks_and_marks_ready():
    repo = FakeRepo({"d1": doc("d1")})
    svc = processor(repo, FakeStore({"org-1/x.pdf": PDF_BYTES}))

    result = svc.process("d1")

    assert result.status == "ready"
    assert result.pages == 3
    assert result.chunk_strategy == "paragraph"
    assert result.empty is False
    assert repo.ready == ["d1"]
    assert repo.docs["d1"].status == "ready"
    assert [p.page_number for p in repo.inserted] == [1, 2, 3]
    assert [p.section for p in repo.inserted] == [
        "Introduction",
        "Architecture",
        "Retrieval",
    ]
    assert all(p.source == "s3://knowflow/org-1/x.pdf" for p in repo.inserted)

    # paragraph strategy (the production default) merges the three short pages
    # into one coherent chunk that keeps the citation chain
    assert repo.chunks, "chunking must run as part of processing"
    assert len(repo.chunks) == 1
    chunk = repo.chunks[0]
    assert chunk.chunk_index == 0
    assert chunk.page_number == 1
    assert chunk.token_count > 0
    assert chunk.metadata["strategy"] == "paragraph"
    assert chunk.metadata["section"] == "Introduction"
    assert chunk.metadata["source"] == "s3://knowflow/org-1/x.pdf"
    assert chunk.metadata["page_range"] == [1, 3]
    assert "Introduction to KnowFlow" in chunk.content
    assert "Search and retrieval" in chunk.content


def test_chunking_strategy_is_pluggable_per_call():
    repo = FakeRepo({"d1": doc("d1")})
    svc = processor(repo, FakeStore({"org-1/x.pdf": PDF_BYTES}))

    result = svc.process("d1", "fixed")

    assert result.status == "ready"
    assert result.chunk_strategy == "fixed"
    assert repo.chunks[0].metadata["strategy"] == "fixed"


def test_implausible_chunk_params_fail_the_document_cleanly():
    repo = FakeRepo({"d1": doc("d1")})
    svc = processor(repo, FakeStore({"org-1/x.pdf": PDF_BYTES}))

    result = svc.process("d1", "overlap", chunk_tokens=16, overlap_tokens=16)

    assert result.status == "failed"
    assert "chunking parameters rejected" in result.detail
    assert repo.docs["d1"].status == "failed"
    assert repo.inserted == []
    assert repo.chunks == []


def test_empty_pdf_marks_ready_with_zero_pages():
    repo = FakeRepo({"d1": doc("d1")})
    svc = processor(repo, FakeStore({"org-1/x.pdf": (FIXTURES / "blank_page.pdf").read_bytes()}))

    result = svc.process("d1")

    assert result.status == "ready"
    assert result.empty is True
    assert result.pages == 0
    assert repo.inserted == []
    assert repo.docs["d1"].status == "ready"


def test_unknown_document():
    svc = processor(FakeRepo({}), FakeStore({}))
    assert svc.process("ghost").status == "not_found"


def test_claim_race_leaves_a_busy_document_alone():
    repo = FakeRepo({"d1": doc("d1", status="processing")})
    svc = processor(repo, FakeStore({}))

    result = svc.process("d1")

    assert result.status == "not_queued"
    assert repo.inserted == []
    assert repo.failed == []


def test_already_ready_is_not_queued():
    repo = FakeRepo({"d1": doc("d1", status="ready")})
    svc = processor(repo, FakeStore({}))
    assert svc.process("d1").status == "not_queued"


def test_unsupported_source_type_is_refused_without_touching_status():
    repo = FakeRepo({"d1": doc("d1", source_type="docx")})
    svc = processor(repo, FakeStore({}))

    result = svc.process("d1")

    assert result.status == "unsupported_type"
    assert repo.docs["d1"].status == "queued"
    assert repo.ready == []
    assert repo.failed == []


def test_corrupt_pdf_fails_the_document():
    repo = FakeRepo({"d1": doc("d1")})
    svc = processor(repo, FakeStore({"org-1/x.pdf": CORRUPT_BYTES}))

    result = svc.process("d1")

    assert result.status == "failed"
    assert "pdf extraction failed" in result.detail
    assert repo.docs["d1"].status == "failed"
    assert repo.failed[0][0] == "d1"
    assert repo.inserted == []


def test_missing_object_fails_cleanly():
    repo = FakeRepo({"d1": doc("d1", storage_key="org-1/gone.pdf")})
    svc = processor(repo, FakeStore({}, missing_key="org-1/gone.pdf"))

    result = svc.process("d1")

    assert result.status == "failed"
    assert "missing from storage" in result.detail
    assert repo.docs["d1"].status == "failed"


def test_network_error_fails_cleanly_instead_of_crashing():
    class ExplodingStore:
        def get(self, key: str) -> bytes:
            raise ConnectionError("minio refused the connection")

        def reachable(self) -> bool:
            return False

    repo = FakeRepo({"d1": doc("d1")})
    svc = processor(repo, ExplodingStore())

    result = svc.process("d1")

    assert result.status == "failed"
    assert "processor error" in result.detail
    assert repo.docs["d1"].status == "failed"