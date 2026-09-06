from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from app.core.config import get_settings
from app.processor.downloader import S3Downloader
from app.processor.pdf import PdfExtractor
from app.processor.processor import ProcessorService
from app.processor.repository import PostgresDocumentRepository

FIXTURES = Path(__file__).parent / "fixtures"

skip_guard = False
try:
    settings = get_settings()
    connection = psycopg.connect(settings.database_url)
    connection.autocommit = True  # each seed/cleanup statement stands alone
    connection.row_factory = dict_row
    connection.execute("SELECT 1")
    # a SECOND connection verifies committed state, so a write that only the
    # writer connection can see (e.g. an uncommitted implicit transaction)
    # fails these tests instead of passing them
    verify_conn = psycopg.connect(settings.database_url)
    verify_conn.autocommit = True
    verify_conn.row_factory = dict_row
    downloader = S3Downloader(settings)
    if not downloader.reachable():
        raise RuntimeError("MinIO unreachable")
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
            (THE_ORG, "process-it org", THE_ORG),
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
        # cascade removes the seeded user + any left-over pages
        cur.execute("DELETE FROM organizations WHERE id = %s", (THE_ORG,))


@live
def test_full_processing_of_a_planted_pdf():
    storage_key = f"{THE_ORG}/{uuid.uuid4()}.pdf"
    document_id = seed_document(connection, "pdf", storage_key)
    downloader.put(storage_key, (FIXTURES / "three_pages.pdf").read_bytes())

    try:
        service = ProcessorService(
            repository=PostgresDocumentRepository(connection),
            downloader=downloader,
            extractor=PdfExtractor(),
            bucket=settings.s3_bucket,
        )
        result = service.process(document_id)

        assert result.status == "ready"
        assert result.pages == 3

        rows = verify_conn.execute(
            "SELECT status, error FROM documents WHERE id = %s", (document_id,)
        ).fetchone()
        assert rows["status"] == "ready"
        assert rows["error"] is None

        pages = verify_conn.execute(
            "SELECT page_number, content, section, source, token_count, organization_id "
            "FROM document_pages WHERE document_id = %s ORDER BY page_number",
            (document_id,),
        ).fetchall()
        assert [p["page_number"] for p in pages] == [1, 2, 3]
        assert [p["section"] for p in pages] == [
            "Introduction",
            "Architecture",
            "Retrieval",
        ]
        assert all(p["content"] for p in pages)
        assert all(p["source"] == f"s3://{settings.s3_bucket}/{storage_key}" for p in pages)
        assert all(str(p["organization_id"]) == THE_ORG for p in pages)
        assert all(p["token_count"] > 0 for p in pages)

        chunks = verify_conn.execute(
            "SELECT chunk_index, page_number, content, token_count, metadata, organization_id "
            "FROM chunks WHERE document_id = %s ORDER BY chunk_index",
            (document_id,),
        ).fetchall()
        assert chunks, "processing must leave chunks behind"
        assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))
        assert all(c["content"] for c in chunks)
        assert all(c["token_count"] > 0 for c in chunks)
        assert all(str(c["organization_id"]) == THE_ORG for c in chunks)
        # paragraph (production default) merges the three short pages into one
        assert len(chunks) == 1
        assert chunks[0]["page_number"] == 1
        assert chunks[0]["metadata"]["strategy"] == "paragraph"
        assert chunks[0]["metadata"]["section"] == "Introduction"
        assert chunks[0]["metadata"]["page_range"] == [1, 3]
        assert all("Introduction to KnowFlow" in c["content"] for c in chunks)
    finally:
        cleanup_document(connection, document_id, storage_key)


@live
def test_corrupt_pdf_marks_document_failed_with_reason():
    storage_key = f"{THE_ORG}/{uuid.uuid4()}.pdf"
    document_id = seed_document(connection, "pdf", storage_key)
    downloader.put(storage_key, (FIXTURES / "corrupt.pdf").read_bytes())

    try:
        service = ProcessorService(
            repository=PostgresDocumentRepository(connection),
            downloader=downloader,
            extractor=PdfExtractor(),
            bucket=settings.s3_bucket,
        )
        result = service.process(document_id)

        assert result.status == "failed"
        assert "pdf extraction failed" in result.detail
        row = verify_conn.execute(
            "SELECT status, error FROM documents WHERE id = %s", (document_id,)
        ).fetchone()
        assert row["status"] == "failed"
        assert "unreadable PDF" in row["error"]

        count = verify_conn.execute(
            "SELECT count(*) AS n FROM document_pages WHERE document_id = %s", (document_id,)
        ).fetchone()["n"]
        assert count == 0
        chunk_count = verify_conn.execute(
            "SELECT count(*) AS n FROM chunks WHERE document_id = %s", (document_id,)
        ).fetchone()["n"]
        assert chunk_count == 0
    finally:
        cleanup_document(connection, document_id, storage_key)


@live
def test_process_is_idempotent_on_an_already_ready_document():
    storage_key = f"{THE_ORG}/{uuid.uuid4()}.pdf"
    document_id = seed_document(connection, "pdf", storage_key)
    downloader.put(storage_key, (FIXTURES / "three_pages.pdf").read_bytes())

    try:
        service = ProcessorService(
            repository=PostgresDocumentRepository(connection),
            downloader=downloader,
            extractor=PdfExtractor(),
            bucket=settings.s3_bucket,
        )
        first = service.process(document_id)
        assert first.status == "ready"
        rows_after_first = verify_conn.execute(
            "SELECT (SELECT count(*) FROM document_pages WHERE document_id = %s) AS pages, "
            "(SELECT count(*) FROM chunks WHERE document_id = %s) AS chunks",
            (document_id, document_id),
        ).fetchone()
        second = service.process(document_id)
        assert second.status == "not_queued"
        rows_after_second = verify_conn.execute(
            "SELECT (SELECT count(*) FROM document_pages WHERE document_id = %s) AS pages, "
            "(SELECT count(*) FROM chunks WHERE document_id = %s) AS chunks",
            (document_id, document_id),
        ).fetchone()
        assert rows_after_second == rows_after_first  # no duplicate pages/chunks
    finally:
        cleanup_document(connection, document_id, storage_key)


@live
def test_full_processing_of_a_chapter_pdf_respects_paragraph_strategy():
    storage_key = f"{THE_ORG}/{uuid.uuid4()}.pdf"
    document_id = seed_document(connection, "pdf", storage_key)
    downloader.put(storage_key, (FIXTURES / "chapter.pdf").read_bytes())

    try:
        service = ProcessorService(
            repository=PostgresDocumentRepository(connection),
            downloader=downloader,
            extractor=PdfExtractor(),
            bucket=settings.s3_bucket,
        )
        result = service.process(document_id)
        assert result.status == "ready"
        assert result.pages >= 5

        chunks = verify_conn.execute(
            "SELECT content, token_count, metadata "
            "FROM chunks WHERE document_id = %s ORDER BY chunk_index",
            (document_id,),
        ).fetchall()
        assert all(c["metadata"]["strategy"] == "paragraph" for c in chunks)
        assert all(c["token_count"] > 0 for c in chunks)
        # paragraph detection keeps an author's sentence together
        assert any(
            "Paragraph chunks are the production choice because they keep each "
            "retrieval unit semantically coherent." in c["content"]
            for c in chunks
        )
    finally:
        cleanup_document(connection, document_id, storage_key)