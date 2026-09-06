import json
from contextlib import contextmanager
from typing import Iterator, Protocol

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.sql import SQL, Identifier

from .schemas import ChunkRecord, DocumentRecord, PageRecord


class DocumentRepository(Protocol):
    def get(self, document_id: str) -> DocumentRecord | None: ...

    def claim(self, document_id: str) -> DocumentRecord | None:
        """Flip queued -> processing and hand back the row, atomically."""

    def insert_pages(self, pages: list[PageRecord]) -> None: ...

    def insert_chunks(self, chunks: list[ChunkRecord]) -> None: ...

    def mark_ready(self, document_id: str) -> None: ...

    def mark_failed(self, document_id: str, error: str) -> None: ...

    @contextmanager
    def transaction(self) -> Iterator[None]: ...


_SELECTED = (
    "id, organization_id, title, filename, source_type, storage_key, status"
)

# composable column list; safe from string interpolation
_COLUMNS = SQL(", ").join(Identifier(c) for c in _SELECTED.split(", "))


class PostgresDocumentRepository:
    """Row access over the live Day-3/5 database (psycopg 3).

    The claim is an UPDATE ... RETURNING so two workers cannot both grab the
    same queued document; the rest of the writes ride one transaction per
    phase owned by the processor. A schema change here ships as a migration
    in backend/src/database/migrations/ and is mirrored in db/schema.sql.
    """

    def __init__(self, connection: Connection):
        # autocommit isolates reads (get) from writes; `with transaction():`
        # then issues a real BEGIN/COMMIT instead of joining the implicit
        # transaction a plain execute() opens (which psycopg3 never closes).
        self._conn = connection
        self._conn.autocommit = True
        self._conn.row_factory = dict_row

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._conn.transaction():
            yield

    def get(self, document_id: str) -> DocumentRecord | None:
        with self._conn.cursor() as cur:
            cur.execute(
                SQL("SELECT {} FROM documents WHERE id = %s").format(_COLUMNS),
                (document_id,),
            )
            return _to_record(cur.fetchone())

    def claim(self, document_id: str) -> DocumentRecord | None:
        with self._conn.cursor() as cur:
            cur.execute(
                SQL(
                    "UPDATE documents SET status = 'processing' "
                    "WHERE id = %s AND status = 'queued' "
                    "RETURNING {}"
                ).format(_COLUMNS),
                (document_id,),
            )
            return _to_record(cur.fetchone())

    def insert_pages(self, pages: list[PageRecord]) -> None:
        if not pages:
            return
        rows = [
            (
                p.organization_id,
                p.document_id,
                p.page_number,
                p.content,
                p.token_count,
                p.section,
                p.source,
            )
            for p in pages
        ]
        with self._conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO document_pages "
                "(organization_id, document_id, page_number, content, token_count, section, source) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                rows,
            )

    def insert_chunks(self, chunks: list[ChunkRecord]) -> None:
        if not chunks:
            return
        rows = [
            (
                c.organization_id,
                c.document_id,
                c.content,
                c.chunk_index,
                c.page_number,
                c.token_count,
                json.dumps(c.metadata),
            )
            for c in chunks
        ]
        with self._conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO chunks "
                "(organization_id, document_id, content, chunk_index, page_number, token_count, metadata) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                rows,
            )

    def mark_ready(self, document_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE documents SET status = 'ready', error = NULL "
                "WHERE id = %s",
                (document_id,),
            )

    def mark_failed(self, document_id: str, error: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE documents SET status = 'failed', error = %s "
                "WHERE id = %s",
                (error[:1000], document_id),
            )


def _to_record(row: dict | None) -> DocumentRecord | None:
    if row is None:
        return None
    return DocumentRecord(
        id=str(row["id"]),
        organization_id=str(row["organization_id"]),
        title=row["title"],
        filename=row["filename"],
        source_type=row["source_type"],
        storage_key=row["storage_key"],
        status=row["status"],
    )