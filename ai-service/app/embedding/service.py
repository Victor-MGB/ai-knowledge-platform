"""Day-11 embedding pipeline: ready document chunks -> vectors in `embeddings`.

Ingestion is deliberately two phases. Extraction+chunking (Days 9-10) is
fast and DB-bound; embedding makes provider calls (network for real models)
per batch, so it must not hold the processor transaction open. It runs as its
own claimed work unit tracked by `documents.embedding_status`, which is what
retrieval (Day 15) will key on.

Batching, retry and failure semantics:
  * chunk texts are embedded in slices of `batch_size`;
  * a slice is retried with exponential backoff on retryable failures
    (timeouts, transport errors, 5xx, 429) up to `max_retries`;
  * 4xx provider errors are fatal immediately — a missing key or an input
    the model rejects will not heal by retrying;
  * any un-recovered batch fails the whole run with the reason stored in
    `documents.embedding_error`. A run never stores a partial vector set:
    inserts + the ready flip ride one transaction, so a mid-run failure
    rolls back or marks the run failed — never silently half-embedded.
"""

import math
import time
from typing import Protocol, Sequence

import httpx

from ..core.config import Settings
from ..models.results import EmbeddingResult
from ..services import build_embedding_service
from .repository import EmbeddingRepository, PostgresEmbeddingRepository
from .schemas import EmbeddingPipelineResult


class Vectorizer(Protocol):
    """The minimum a vector provider must expose — EmbeddingService fits."""

    model: str
    dimensions: int | None  # None = known only after the first call (openai)

    def embed(self, text: str) -> EmbeddingResult: ...


class EmbeddingFailedError(Exception):
    """A batch died and will not heal by retrying."""


class EmbeddingDataError(EmbeddingFailedError):
    """The provider returned something unusable (wrong shape / non-finite)."""


def is_retryable(exc: Exception) -> bool:
    """Transient failures deserve a retry; provider rejections do not."""
    if isinstance(exc, (httpx.TimeoutException, httpx.RequestError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return False


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(repr(value) for value in vector) + "]"


class EmbeddingPipeline:
    """Embed every chunk of one ready document with retries and validation."""

    def __init__(
        self,
        repository: EmbeddingRepository,
        vectorizer: Vectorizer,
        *,
        model: str | None = None,
        dimensions: int | None = None,
        batch_size: int = 64,
        max_retries: int = 3,
        retry_backoff: float = 0.5,
    ):
        self._repo = repository
        self._vectorizer = vectorizer
        self._model = model if model is not None else vectorizer.model
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._batch_size = max(1, batch_size)
        self._expected_dim = (
            dimensions if dimensions is not None else vectorizer.dimensions
        )

    def embed_document(self, document_id: str) -> EmbeddingPipelineResult:
        document_id = str(document_id)  # psycopg hands back UUID objects
        document = self._repo.get_document(document_id)
        if document is None:
            return EmbeddingPipelineResult(document_id=document_id, status="not_found")

        if document.embedding_status == "ready":
            return EmbeddingPipelineResult(
                document_id=document_id,
                status="not_embeddable",
                detail="document already embedded (embedding_status=ready)",
            )
        if document.embedding_status == "processing":
            return EmbeddingPipelineResult(
                document_id=document_id,
                status="not_embeddable",
                detail="embedding already in progress (embedding_status=processing)",
            )
        if document.status != "ready":
            return EmbeddingPipelineResult(
                document_id=document_id,
                status="not_embeddable",
                detail=f"document status is {document.status}, not ready",
            )

        chunks = self._repo.list_chunks(document_id)
        if not chunks:
            # A ready document with zero chunks is a legacy/seed row or a
            # pre-chunking artifact; checking BEFORE the claim means a refused
            # run never flips embedding_status (or wedges it in 'processing').
            return EmbeddingPipelineResult(
                document_id=document_id,
                status="not_embeddable",
                detail="document has no chunks to embed (pre-pipeline seed row?)",
            )

        if not self._repo.claim_for_embedding(document_id):
            return EmbeddingPipelineResult(
                document_id=document_id,
                status="not_embeddable",
                detail="claim lost the race (another worker is embedding it)",
            )

        try:
            vectors = self._embed_all([c.content for c in chunks])
            rows = [
                (
                    document.organization_id,
                    chunks[index].chunk_id,
                    self._model,
                    len(vector),
                    _vector_literal(vector),
                )
                for index, vector in enumerate(vectors)
            ]
        except EmbeddingFailedError as exc:
            return self._fail(document_id, str(exc))
        except Exception as exc:  # provider/network; never let a worker crash
            return self._fail(
                document_id,
                f"embedding run failed: {type(exc).__name__}: {exc}",
            )

        try:
            with self._repo.transaction():
                self._repo.insert_embeddings(rows)
                self._repo.mark_embedding_ready(document_id)
        except Exception as exc:  # storage; the run must end as 'failed', not half-embedded
            return self._fail(
                document_id,
                f"could not persist embeddings: {type(exc).__name__}: {exc}",
            )

        return EmbeddingPipelineResult(
            document_id=document_id,
            status="embedded",
            chunks=len(chunks),
            vectors=len(vectors),
            model=self._model,
            dimensions=self._dimensions_of(vectors),
            detail=f"embedded {len(vectors)} chunk(s) with {self._model}",
        )

    def _embed_all(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed `texts` as `batch_size` slices, each retried independently."""
        self._expected_dim = self._vectorizer.dimensions
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            vectors.extend(self._embed_batch(texts[start : start + self._batch_size]))
        return vectors

    def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        for attempt in range(self._max_retries + 1):
            try:
                vectors = [self._embed_one(text) for text in texts]
                self._coerce_dimensions(vectors)
                return vectors
            except EmbeddingFailedError:
                raise
            except Exception as exc:  # noqa: BLE001 - retry decision below
                retryable = is_retryable(exc)
                if not retryable or attempt == self._max_retries:
                    reason = "transient failure, retries exhausted" if retryable else "not retryable"
                    raise EmbeddingFailedError(
                        f"embedding batch failed ({reason}, "
                        f"attempt {attempt + 1}): {type(exc).__name__}: {exc}"
                    ) from exc
                time.sleep(self._retry_backoff * (2**attempt))
        raise AssertionError("unreachable: loop always returns or raises")

    def _embed_one(self, text: str) -> list[float]:
        result = self._vectorizer.embed(text)
        vector = result.vector
        if not vector:
            raise EmbeddingDataError("provider returned an empty vector")
        if not all(math.isfinite(value) for value in vector):
            raise EmbeddingDataError("provider returned non-finite values")
        return vector

    def _coerce_dimensions(self, vectors: list[list[float]]) -> None:
        """Enforce one consistent dimension across the run.

        The first vector (or a known provider dimension) sets the bar; every
        later vector must match, so a provider switching models or truncating
        a batch mid-run is caught instead of silently mis-storing vectors.
        """
        if not vectors:
            return
        if self._expected_dim is None:
            self._expected_dim = len(vectors[0])
        for vector in vectors:
            if len(vector) != self._expected_dim:
                raise EmbeddingDataError(
                    f"embedding dimension {len(vector)} != expected {self._expected_dim}"
                )

    @staticmethod
    def _dimensions_of(vectors: Sequence[Sequence[float]]) -> int:
        return len(vectors[0]) if vectors else 0

    def _fail(self, document_id: str, detail: str) -> EmbeddingPipelineResult:
        try:
            with self._repo.transaction():
                self._repo.mark_embedding_failed(document_id, detail)
        except Exception:
            pass  # the original failure is the reportable one
        return EmbeddingPipelineResult(
            document_id=document_id, status="failed", detail=detail
        )


def build_embedding_pipeline(settings: Settings) -> EmbeddingPipeline:
    from ..core.db import connect

    connection = connect(settings.database_url)
    return EmbeddingPipeline(
        repository=PostgresEmbeddingRepository(connection),
        vectorizer=build_embedding_service(settings),
        batch_size=settings.embedding_batch_size,
        max_retries=settings.embedding_max_retries,
        retry_backoff=settings.embedding_retry_backoff,
    )