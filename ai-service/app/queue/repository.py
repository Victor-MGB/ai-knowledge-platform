"""Durable job ledger for the Day-12 worker pool (`ingestion_jobs`).

RQ owns the transient task in Redis; this table is the source of truth for
what happened, and the retry budget. The claim (`claim`) is the concurrency
guard: an atomic UPDATE ... RETURNING is the only path from queued to
processing, so two workers can never run the same (document, phase) twice.
Retries go back through `requeue` (status -> queued) and round-trip the claim,
which is also what bumps `attempts`.

One connection powers the queue's rows AND the `fail_sink` (documents status
flips) so a worker holds a single pool of connections for its whole lifetime.
"""

import threading
from typing import Protocol, Sequence

from psycopg import Connection, OperationalError
from psycopg.rows import dict_row

from .schemas import JobRecord


class ResilientPostgresConnection:
    """A single logical PG connection that self-heals when its socket dies.

    The API/worker cache ONE long-lived psycopg connection for the queue's
    job ledger and fail sink. If that connection drops (idle TCP close,
    postgres restart, network blip), every later statement raises
    psycopg.OperationalError: "the connection is closed" and the whole
    FastAPI process stops enqueueing until the container restarts (seen at
    1,000-doc scale: 102 consecutive 503s).

    This wrapper transparently re-opens the connection: before a statement
    when the handle is closed, and once immediately after an in-flight
    OperationalError. A lock serializes statements because the shared socket
    is used by the FastAPI threadpool when uploads arrive concurrently.
    """

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn: Connection | None = None
        self._lock = threading.Lock()

    def execute(
        self, sql: str, params: Sequence | None = None, *, one: bool = False
    ) -> dict | list[dict] | None:
        with self._lock:
            for attempt in range(2):
                try:
                    self._ensure_open()
                    with self._conn.cursor() as cur:
                        cur.execute(sql, params)
                        if cur.description is None:
                            return None
                        return cur.fetchone() if one else cur.fetchall()
                except OperationalError:
                    self._drop()
                    if attempt:
                        raise
        return None

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _ensure_open(self) -> None:
        if self._conn is None or self._conn.closed:
            import psycopg

            self._conn = psycopg.connect(self._dsn)
            self._conn.autocommit = True
            self._conn.row_factory = dict_row

    def _drop(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


class JobRepository(Protocol):
    """The minimum a QueueService needs from persistent storage."""

    def ensure(
        self, document_id: str, kind: str, *, max_attempts: int
    ) -> JobRecord | None:
        """Idempotent insert (one row per (document, kind)); None if the
        document doesn't exist. Does not disturb an existing row's state."""

    def get(self, document_id: str, kind: str) -> JobRecord | None: ...

    def list_for_document(self, document_id: str) -> list[JobRecord]: ...

    def document_status(self, document_id: str) -> tuple[str, str] | None:
        """(documents.status, documents.embedding_status) or None."""

    def claim(self, document_id: str, kind: str) -> JobRecord | None:
        """queued -> processing, attempts += 1; None if not claimable."""

    def succeed(self, document_id: str, kind: str) -> None: ...

    def fail(self, document_id: str, kind: str, error: str) -> None: ...

    def requeue(self, document_id: str, kind: str, error: str) -> JobRecord | None:
        """processing -> queued (keeps attempts); None if not retryable."""


class FailSink(Protocol):
    """How a worker signals an unrecovered failure to the document row."""

    def mark_document_failed(self, document_id: str, error: str) -> None:
        """documents.status -> failed (used when a worker run crashes)."""

    def mark_embedding_failed(self, document_id: str, error: str) -> None:
        """documents.embedding_status -> failed (guarded to 'processing')."""


class PostgresJobRepository:
    def __init__(self, connection: ResilientPostgresConnection):
        self._conn = connection

    def ensure(
        self, document_id: str, kind: str, *, max_attempts: int
    ) -> JobRecord | None:
        self._conn.execute(
            "INSERT INTO ingestion_jobs (organization_id, document_id, kind, max_attempts) "
            "SELECT organization_id, id, %s, %s FROM documents WHERE id = %s "
            "ON CONFLICT (document_id, kind) DO NOTHING",
            (kind, max_attempts, document_id),
        )
        return self.get(document_id, kind)

    def get(self, document_id: str, kind: str) -> JobRecord | None:
        row = self._conn.execute(
            "SELECT * FROM ingestion_jobs WHERE document_id = %s AND kind = %s",
            (document_id, kind),
            one=True,
        )
        return _to_record(row)

    def list_for_document(self, document_id: str) -> list[JobRecord]:
        rows = self._conn.execute(
            "SELECT * FROM ingestion_jobs WHERE document_id = %s ORDER BY kind",
            (document_id,),
        )
        return [_to_record(row) for row in rows]

    def document_status(self, document_id: str) -> tuple[str, str] | None:
        row = self._conn.execute(
            "SELECT status, embedding_status FROM documents WHERE id = %s",
            (document_id,),
            one=True,
        )
        if row is None:
            return None
        return str(row["status"]), str(row["embedding_status"])

    def claim(self, document_id: str, kind: str) -> JobRecord | None:
        row = self._conn.execute(
            "UPDATE ingestion_jobs "
            "SET status = 'processing', attempts = attempts + 1, "
            "    started_at = now(), "
            "    error = NULL, finished_at = NULL "
            "WHERE document_id = %s AND kind = %s AND status = 'queued' "
            "RETURNING *",
            (document_id, kind),
            one=True,
        )
        return _to_record(row)

    def succeed(self, document_id: str, kind: str) -> None:
        self._conn.execute(
            "UPDATE ingestion_jobs "
            "SET status = 'succeeded', error = NULL, finished_at = now() "
            "WHERE document_id = %s AND kind = %s AND status = 'processing'",
            (document_id, kind),
        )

    def fail(self, document_id: str, kind: str, error: str) -> None:
        self._conn.execute(
            "UPDATE ingestion_jobs "
            "SET status = 'failed', error = %s, finished_at = now() "
            "WHERE document_id = %s AND kind = %s",
            (error[:2000], document_id, kind),
        )

    def requeue(self, document_id: str, kind: str, error: str) -> JobRecord | None:
        row = self._conn.execute(
            "UPDATE ingestion_jobs "
            "SET status = 'queued', error = %s, "
            "    started_at = NULL, finished_at = NULL "
            "WHERE document_id = %s AND kind = %s "
            "  AND status IN ('processing', 'failed') "
            "RETURNING *",
            (error[:2000], document_id, kind),
            one=True,
        )
        return _to_record(row)

    def delete(self, document_id: str, kind: str) -> None:
        self._conn.execute(
            "DELETE FROM ingestion_jobs WHERE document_id = %s AND kind = %s",
            (document_id, kind),
        )


class PostgresFailSink:
    def __init__(self, connection: ResilientPostgresConnection):
        self._conn = connection

    def mark_document_failed(self, document_id: str, error: str) -> None:
        self._conn.execute(
            "UPDATE documents SET status = 'failed', error = %s WHERE id = %s",
            (error[:1000], document_id),
        )

    def mark_embedding_failed(self, document_id: str, error: str) -> None:
        self._conn.execute(
            "UPDATE documents "
            "SET embedding_status = 'failed', embedding_error = %s "
            "WHERE id = %s AND embedding_status = 'processing'",
            (error[:1000], document_id),
        )


def _to_record(row: dict | None) -> JobRecord | None:
    if row is None:
        return None
    return JobRecord(
        job_id=str(row["id"]),
        organization_id=str(row["organization_id"]),
        document_id=str(row["document_id"]),
        kind=str(row["kind"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        error=row["error"],
        enqueued_at=row["enqueued_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )