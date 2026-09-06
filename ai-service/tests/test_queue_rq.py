"""Day 12 (live integration): enqueue -> RQ worker -> process -> embed chain.

Uses the REAL stack (Postgres + MinIO + Redis) and the REAL RQ path: a job is
enqueued through the production `build_default_queue_service`, then a
`SimpleWorker` burst loop pops it off redis and executes the module-level task
(which chains the embed phase from inside the process run). Verifies the
durable ledger rows and the derived pipeline state via a second connection, so
transactions that never committed fail the test instead of passing it.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row
from redis import Redis
from rq import Queue
from rq.worker import SimpleWorker

from app.core.config import get_settings
from app.processor.downloader import S3Downloader
from app.queue import build_default_queue_service
from app.queue.jobs import reset_service

FIXTURES = Path(__file__).parent / "fixtures"

skip_guard = False
try:
    settings = get_settings()
    connection = psycopg.connect(settings.database_url)
    connection.autocommit = True
    connection.row_factory = dict_row
    connection.execute("SELECT 1")
    verify_conn = psycopg.connect(settings.database_url)
    verify_conn.autocommit = True
    verify_conn.row_factory = dict_row
    downloader = S3Downloader(settings)
    if not downloader.reachable():
        raise RuntimeError("MinIO unreachable")
    redis_conn = Redis.from_url(settings.redis_url)
    redis_conn.ping()
except Exception as exc:  # noqa: BLE001 - guard, not a test
    skip_guard = f"live stack unavailable: {type(exc).__name__}: {exc}"

live = pytest.mark.skipif(bool(skip_guard), reason=str(skip_guard) or "live stack missing")

THE_ORG = str(uuid.uuid4())
THE_USER = str(uuid.uuid4())


def seed_document(conn, source_type: str, storage_key: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (id, name, slug) VALUES (%s, %s, %s) "
            "ON CONFLICT (slug) DO NOTHING",
            (THE_ORG, "queue-it org", THE_ORG),
        )
        cur.execute(
            "INSERT INTO users (id, organization_id, email, password_hash) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (organization_id, email) DO NOTHING",
            (THE_USER, THE_ORG, f"{THE_USER}@example.com", "$2b$12$seed-seed-seed-seed-seed"),
        )
        cur.execute(
            "INSERT INTO documents (organization_id, uploaded_by, title, filename, mime_type, size, source_type, storage_key, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'queued') RETURNING id",
            (THE_ORG, THE_USER, "doc", "doc.pdf", "application/pdf", 1024, source_type, storage_key),
        )
        return cur.fetchone()["id"]


def cleanup_document(conn, document_id: str, storage_key: str):
    try:
        downloader.delete(storage_key)
    except Exception:  # noqa: BLE001 - best-effort cleanup
        pass
    with conn.cursor() as cur:
        cur.execute("DELETE FROM documents WHERE id = %s", (document_id,))
        cur.execute("DELETE FROM organizations WHERE id = %s", (THE_ORG,))


def drain_queue(redis_conn, queue_name: str):
    queue = Queue(queue_name, connection=redis_conn)
    worker = SimpleWorker(queues=[queue], connection=redis_conn)
    worker.work(burst=True)  # executes until the queue (incl. chained jobs) is empty


@live
def test_queued_upload_is_processed_and_embedded_end_to_end():
    reset_service()
    redis_conn.flushall()
    storage_key = f"{THE_ORG}/{uuid.uuid4()}.pdf"
    document_id = seed_document(connection, "pdf", storage_key)
    downloader.put(storage_key, (FIXTURES / "chapter.pdf").read_bytes())

    try:
        service = build_default_queue_service(settings)

        # enqueue path (what the backend calls after an upload)
        job, statuses = service.enqueue_process(document_id)
        assert job is not None and job.status == "queued"
        assert statuses == ("queued", "none")
        assert service.status(document_id).state == "UPLOADED"

        # worker path
        drain_queue(redis_conn, settings.queue_name)

        row = verify_conn.execute(
            "SELECT status, embedding_status, error FROM documents WHERE id = %s",
            (document_id,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["embedding_status"] == "ready"
        assert row["error"] is None

        chunks = verify_conn.execute(
            "SELECT count(*) AS n FROM chunks WHERE document_id = %s", (document_id,)
        ).fetchone()["n"]
        assert chunks > 0

        embeddings = verify_conn.execute(
            "SELECT count(*) AS n, min(dimensions) AS dims, min(model) AS model "
            "FROM embeddings WHERE chunk_id IN "
            "(SELECT id FROM chunks WHERE document_id = %s)",
            (document_id,),
        ).fetchone()
        assert embeddings["n"] == chunks
        assert embeddings["dims"] == 384
        assert embeddings["model"] == "knowflow-hash-384"

        jobs = verify_conn.execute(
            "SELECT kind, status, attempts, finished_at FROM ingestion_jobs "
            "WHERE document_id = %s ORDER BY kind",
            (document_id,),
        ).fetchall()
        assert {j["kind"]: j for j in jobs}["process"]["status"] == "succeeded"
        assert {j["kind"]: j for j in jobs}["embed"]["status"] == "succeeded"
        assert all(j["attempts"] == 1 for j in jobs)
        assert all(j["finished_at"] is not None for j in jobs)

        assert service.status(document_id).state == "READY"

        # re-enqueue is idempotent: row stays as-is, state unchanged
        again, _ = service.enqueue_process(document_id)
        assert again.status == "succeeded"
        assert service.status(document_id).state == "READY"
        assert service.run_process(document_id).status == "ignored"
    finally:
        reset_service()
        cleanup_document(connection, document_id, storage_key)
        redis_conn.flushall()


@live
def test_unsupported_document_never_leaves_a_stuck_state():
    reset_service()
    redis_conn.flushall()
    storage_key = f"{THE_ORG}/{uuid.uuid4()}.pdf"
    document_id = seed_document(connection, "md", storage_key)

    try:
        service = build_default_queue_service(settings)
        job, _ = service.enqueue_process(document_id)
        assert job is not None
        drain_queue(redis_conn, settings.queue_name)

        job_row = verify_conn.execute(
            "SELECT status, error FROM ingestion_jobs WHERE document_id = %s AND kind = 'process'",
            (document_id,),
        ).fetchone()
        assert job_row["status"] == "failed"
        assert "no parser" in job_row["error"]

        doc = verify_conn.execute(
            "SELECT status FROM documents WHERE id = %s", (document_id,)
        ).fetchone()
        assert doc["status"] == "queued"  # not wedged, not failed: awaiting a parser
        assert service.status(document_id).state == "FAILED"  # job-level failure surfaces
    finally:
        reset_service()
        cleanup_document(connection, document_id, storage_key)
        redis_conn.flushall()