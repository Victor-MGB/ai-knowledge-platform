from contextlib import contextmanager
from typing import Iterator, Protocol

from psycopg import Connection
from psycopg.rows import dict_row

from .schemas import ChunkEmbedding, DocumentEmbeddingRecord


class EmbeddingRepository(Protocol):
    def get_document(self, document_id: str) -> DocumentEmbeddingRecord | None: ...

    def claim_for_embedding(self, document_id: str) -> bool:
        """Atomically flip embeddable (none|failed) -> processing.

        Returns False when another worker already claimed the run, so two
        workers can never embed the same document concurrently.
        """

    def list_chunks(self, document_id: str) -> list[ChunkEmbedding]:
        """All chunks of the document in chunk_index order (id + content)."""

    def insert_embeddings(self, rows: list[tuple]) -> None:
        """Upsert rows: (organization_id, chunk_id, model, dimensions, vector)."""

    def mark_embedding_ready(self, document_id: str) -> None: ...

    def mark_embedding_failed(self, document_id: str, error: str) -> None: ...

    @contextmanager
    def transaction(self) -> Iterator[None]: ...


_SELECTED = "id, organization_id, status, embedding_status, embedding_error"


class PostgresEmbeddingRepository:
    """psycopg3 row access for the Day-11 embedding phase.

    Same discipline as the processor's repository: autocommit isolates reads
    from writes, `with transaction():` issues a real BEGIN/COMMIT, and the
    claim is an UPDATE ... RETURNING so concurrent workers serialize on the
    row instead of duplicating vectors.
    """

    def __init__(self, connection: Connection):
        self._conn = connection
        self._conn.autocommit = True
        self._conn.row_factory = dict_row

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._conn.transaction():
            yield

    def get_document(self, document_id: str) -> DocumentEmbeddingRecord | None:
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT {_SELECTED} FROM documents WHERE id = %s",
                (document_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return DocumentEmbeddingRecord(
            id=str(row["id"]),
            organization_id=str(row["organization_id"]),
            status=row["status"],
            embedding_status=row["embedding_status"],
            embedding_error=row["embedding_error"],
        )

    def claim_for_embedding(self, document_id: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE documents SET embedding_status = 'processing' "
                "WHERE id = %s AND embedding_status IN ('none', 'failed') "
                "RETURNING id",
                (document_id,),
            )
            return cur.fetchone() is not None

    def list_chunks(self, document_id: str) -> list[ChunkEmbedding]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, content FROM chunks "
                "WHERE document_id = %s ORDER BY chunk_index",
                (document_id,),
            )
            return [
                ChunkEmbedding(chunk_id=str(row["id"]), content=row["content"])
                for row in cur.fetchall()
            ]

    def insert_embeddings(self, rows: list[tuple]) -> None:
        """Upsert vectors; re-embedding the same (chunk, model) overwrites.

        The embedding is passed as its text form and cast with ::vector, so
        no python-side pgvector adapter is needed; the dim-flexible column
        accepts whatever `dimensions` describes.
        """
        if not rows:
            return
        with self._conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO embeddings "
                "(organization_id, chunk_id, model, dimensions, embedding) "
                "VALUES (%s, %s, %s, %s, %s::vector) "
                "ON CONFLICT (chunk_id, model) DO UPDATE SET "
                "  embedding = EXCLUDED.embedding, "
                "  dimensions = EXCLUDED.dimensions",
                rows,
            )

    def mark_embedding_ready(self, document_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE documents SET embedding_status = 'ready', "
                "embedding_error = NULL WHERE id = %s",
                (document_id,),
            )

    def mark_embedding_failed(self, document_id: str, error: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE documents SET embedding_status = 'failed', "
                "embedding_error = %s WHERE id = %s",
                (error[:1000], document_id),
            )