from __future__ import annotations

from contextlib import contextmanager

import httpx
import pytest

from app.embedding.schemas import (
    ChunkEmbedding,
    DocumentEmbeddingRecord,
    EmbeddingPipelineResult,
)
from app.embedding.service import EmbeddingPipeline
from app.models.results import EmbeddingResult

DIM = 4


def vector(*values: float) -> list[float]:
    return list(values) if values else [1.0, 0.0, 0.0, 0.0]


class FakeRepo:
    """In-memory EmbeddingRepository with a scriptable claim + insert."""

    def __init__(
        self,
        doc: DocumentEmbeddingRecord,
        chunks: list[ChunkEmbedding],
        *,
        claim_wins: bool = True,
        insert_error: Exception | None = None,
    ):
        self.doc = doc
        self.chunks = chunks
        self.claim_wins = claim_wins
        self.insert_error = insert_error
        self.rows: list[tuple] = []
        self.ready: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.claimed: bool = False

    def get_document(self, document_id: str):
        return self.doc

    def claim_for_embedding(self, document_id: str) -> bool:
        if not self.claim_wins:
            return False
        if self.doc.embedding_status not in ("none", "failed"):
            return False
        self.claimed = True
        return True

    def list_chunks(self, document_id: str):
        return self.chunks

    def insert_embeddings(self, rows: list[tuple]):
        if self.insert_error is not None:
            raise self.insert_error
        self.rows.extend(rows)

    def mark_embedding_ready(self, document_id: str):
        self.ready.append(document_id)

    def mark_embedding_failed(self, document_id: str, error: str):
        self.failed.append((document_id, error))

    @contextmanager
    def transaction(self):
        yield


class RecordingVectorizer:
    """Records every embed() call; scriptable failures per call sequence."""

    model = "test-model"
    dimensions = DIM

    def __init__(
        self,
        *,
        retryable_first: int = 0,
        non_retryable: bool = False,
        wrong_dim: bool = False,
        empty_vector: bool = False,
    ):
        self.retryable_first = retryable_first
        self.non_retryable = non_retryable
        self.wrong_dim = wrong_dim
        self.empty_vector = empty_vector
        self.calls = 0
        self.seen: list[str] = []

    def embed(self, text: str) -> EmbeddingResult:
        self.calls += 1
        self.seen.append(text)
        if self.calls <= self.retryable_first:
            raise httpx.ConnectError("provider unreachable", request=httpx.Request("POST", "http://x"))
        if self.non_retryable:
            request = httpx.Request("POST", "http://x")
            raise httpx.HTTPStatusError(
                "400 bad request", request=request, response=httpx.Response(400, request=request)
            )
        if self.empty_vector:
            dims = 0
            values: list[float] = []
        elif self.wrong_dim:
            dims = DIM + 1
            values = [1.0, 0.0, 0.0, 0.0, 0.0]
        else:
            dims = DIM
            values = [1.0, -1.0, 1.0, -1.0]
        return EmbeddingResult(vector=values, dimensions=dims, model=self.model, provider="fake")


def record(*, status="ready", embedding_status="none", **overrides) -> DocumentEmbeddingRecord:
    values = dict(
        id="d1",
        organization_id="org-1",
        status=status,
        embedding_status=embedding_status,
    )
    values.update(overrides)
    return DocumentEmbeddingRecord(**values)


def chunk(chunk_id: str, content: str = "some chunk text") -> ChunkEmbedding:
    return ChunkEmbedding(chunk_id=chunk_id, content=content)


def pipeline(repo, vectorizer, **kwargs) -> EmbeddingPipeline:
    return EmbeddingPipeline(repo, vectorizer, **kwargs)


def test_happy_path_embeds_every_chunk_and_marks_ready():
    repo = FakeRepo(record(), [chunk("c1"), chunk("c2")])
    svc = pipeline(repo, RecordingVectorizer())

    result = svc.embed_document("d1")

    assert result.status == "embedded"
    assert result.chunks == 2
    assert result.vectors == 2
    assert result.model == "test-model"
    assert result.dimensions == DIM
    assert repo.claimed is True
    assert repo.ready == ["d1"]
    assert not repo.failed
    assert len(repo.rows) == 2
    assert all(str(r[0]) == "org-1" for r in repo.rows)
    assert {r[1] for r in repo.rows} == {"c1", "c2"}
    assert all(r[2] == "test-model" for r in repo.rows)
    assert all(r[3] == DIM for r in repo.rows)
    assert all(r[4].startswith("[") and r[4].endswith("]") for r in repo.rows)


def test_batches_are_cut_at_batch_size_and_processed_in_order():
    repo = FakeRepo(record(), [chunk(f"c{i}", f"body {i}") for i in range(5)])
    vec = RecordingVectorizer()
    svc = pipeline(repo, vec, batch_size=2)

    result = svc.embed_document("d1")

    assert result.vectors == 5
    assert vec.seen == ["body 0", "body 1", "body 2", "body 3", "body 4"]


def test_retry_re_runs_the_failing_batch_then_succeeds():
    class FailOnceOn:
        """Raises a transient error the first time it sees `fail_text`."""

        model = "test-model"
        dimensions = DIM

        def __init__(self, fail_text: str):
            self.fail_text = fail_text
            self.calls = 0
            self.seen: list[str] = []

        def embed(self, text: str) -> EmbeddingResult:
            self.calls += 1
            self.seen.append(text)
            if text == self.fail_text and self.seen.count(text) == 1:
                raise httpx.ConnectError("boom", request=httpx.Request("POST", "http://x"))
            return EmbeddingResult(vector=vector(), dimensions=DIM, model=self.model, provider="fake")

    repo = FakeRepo(record(), [chunk("c0", "fine"), chunk("c1", "boom")])
    vec = FailOnceOn("boom")
    svc = pipeline(repo, vec, batch_size=2, max_retries=3, retry_backoff=0)

    result = svc.embed_document("d1")

    assert result.status == "embedded"
    # attempt 1 embeds "fine" (ok) then "boom" (raises); the whole slice is
    # re-run on attempt 2 (calls 3-4) — including the already successful
    # "fine" — proving the retry unit is the batch, not the text
    assert vec.seen == ["fine", "boom", "fine", "boom"]
    assert vec.calls == 4
    assert result.vectors == 2


def test_retry_count_is_per_batch():
    repo = FakeRepo(record(), [chunk("c0"), chunk("c1"), chunk("c2")])
    vec = RecordingVectorizer(retryable_first=2)
    # batch_size=1 -> three batches; the first two consume 2 attempts each
    # (fail + succeed), the third succeeds first try: 2+2+1 = 5 calls
    svc = pipeline(repo, vec, batch_size=1, max_retries=2, retry_backoff=0)

    result = svc.embed_document("d1")

    assert result.status == "embedded"
    assert vec.calls == 5


def test_transient_failures_exhaust_retries_and_fail_the_run():
    repo = FakeRepo(record(), [chunk("c0")])
    vec = RecordingVectorizer(retryable_first=99)
    svc = pipeline(repo, vec, max_retries=2, retry_backoff=0)

    result = svc.embed_document("d1")

    assert result.status == "failed"
    assert repo.rows == []  # nothing was inserted
    assert repo.ready == []
    assert repo.failed == [("d1", result.detail)]
    assert "retries exhausted" in result.detail
    assert "ConnectError" in result.detail
    assert vec.calls == 3  # 1 attempt + 2 retries


def test_non_retryable_provider_error_fails_immediately():
    repo = FakeRepo(record(), [chunk("c0"), chunk("c1")])
    vec = RecordingVectorizer(non_retryable=True)
    svc = pipeline(repo, vec, max_retries=5, retry_backoff=0)

    result = svc.embed_document("d1")

    assert result.status == "failed"
    assert "not retryable" in result.detail
    assert "400 bad request" in result.detail
    assert vec.calls == 1  # no point retrying a 4xx


def test_dimension_mismatch_fails_instead_of_storing_garbage():
    repo = FakeRepo(record(), [chunk("c0")])
    vec = RecordingVectorizer(wrong_dim=True)
    svc = pipeline(repo, vec)

    result = svc.embed_document("d1")

    assert result.status == "failed"
    assert "dimension" in result.detail
    assert repo.rows == []
    assert repo.failed[0][0] == "d1"


def test_empty_vector_from_provider_fails():
    repo = FakeRepo(record(), [chunk("c0")])
    vec = RecordingVectorizer(empty_vector=True)
    svc = pipeline(repo, vec)

    result = svc.embed_document("d1")

    assert result.status == "failed"
    assert "empty vector" in result.detail


def test_document_with_no_chunks_is_not_embeddable():
    repo = FakeRepo(record(), [])
    svc = pipeline(repo, RecordingVectorizer())

    result = svc.embed_document("d1")

    assert result.status == "not_embeddable"
    assert "no chunks" in result.detail
    assert repo.claimed is False  # did not mark the run, did not flip status
    assert repo.ready == []
    assert repo.failed == []


def test_document_that_is_not_ready_is_not_embeddable():
    repo = FakeRepo(record(status="queued"), [chunk("c0")])
    svc = pipeline(repo, RecordingVectorizer())

    result = svc.embed_document("d1")

    assert result.status == "not_embeddable"
    assert "not ready" in result.detail


def test_already_embedded_document_is_refused():
    repo = FakeRepo(record(embedding_status="ready"), [chunk("c0")])
    svc = pipeline(repo, RecordingVectorizer())

    result = svc.embed_document("d1")

    assert result.status == "not_embeddable"
    assert "already embedded" in result.detail
    assert repo.rows == []


def test_in_flight_document_is_refused():
    repo = FakeRepo(record(embedding_status="processing"), [chunk("c0")])
    svc = pipeline(repo, RecordingVectorizer())

    result = svc.embed_document("d1")

    assert result.status == "not_embeddable"
    assert "in progress" in result.detail


def test_failed_document_can_be_re_embedded():
    repo = FakeRepo(record(embedding_status="failed"), [chunk("c0")])
    svc = pipeline(repo, RecordingVectorizer())

    result = svc.embed_document("d1")

    assert result.status == "embedded"
    assert repo.claimed is True


def test_claim_race_lost_is_not_embeddable():
    repo = FakeRepo(record(), [chunk("c0")], claim_wins=False)
    svc = pipeline(repo, RecordingVectorizer())

    result = svc.embed_document("d1")

    assert result.status == "not_embeddable"
    assert "race" in result.detail
    assert repo.rows == []


def test_unknown_document_is_not_found():
    class NotFoundRepo(FakeRepo):
        def get_document(self, document_id):
            return None

    repo = NotFoundRepo(record(), [chunk("c0")])
    result = pipeline(repo, RecordingVectorizer()).embed_document("ghost")
    assert result.status == "not_found"


def test_persist_failure_marks_run_failed_cleanly():
    repo = FakeRepo(record(), [chunk("c0")], insert_error=RuntimeError("pg on fire"))
    svc = pipeline(repo, RecordingVectorizer())

    result = svc.embed_document("d1")

    assert result.status == "failed"
    assert "could not persist" in result.detail
    assert repo.failed == [("d1", result.detail)]
    assert repo.ready == []