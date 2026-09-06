from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from app.core.config import get_settings
from app.embedding.repository import PostgresEmbeddingRepository
from app.embedding.service import EmbeddingPipeline
from app.processor.downloader import S3Downloader
from app.processor.pdf import PdfExtractor
from app.processor.processor import ProcessorService
from app.processor.repository import PostgresDocumentRepository
from app.services import build_embedding_service

FIXTURES = Path(__file__).parent / "fixtures"

skip_guard = False
try:
    settings = get_settings()
    connection = psycopg.connect(settings.database_url)
    connection.autocommit = True  # each seed/cleanup statement stands alone
    connection.row_factory = dict_row
    connection.execute("SELECT 1")
    # a SECOND connection verifies committed state (Day-9 lesson): writes
    # visible only to the writer connection fail these tests on purpose
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

MODEL = settings.embedding_model          # knowflow-hash-384
DIMS = settings.embedding_dimensions      # 384


def seed_document(conn, source_type: str, storage_key: str, *, status: str = "queued") -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (id, name, slug) VALUES (%s, %s, %s) "
            "ON CONFLICT (slug) DO NOTHING",
            (THE_ORG, "embed-it org", THE_ORG),
        )
        cur.execute(
            "INSERT INTO users (id, organization_id, email, password_hash) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (organization_id, email) DO NOTHING",
            (THE_USER, THE_ORG, f"{THE_USER}@example.com", "$2b$12$seed-seed-seed-seed-seed"),
        )
        cur.execute(
            "INSERT INTO documents (organization_id, uploaded_by, title, filename, mime_type, size, source_type, storage_key, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (THE_ORG, THE_USER, "doc", "doc.pdf", "application/pdf", 1024, source_type, storage_key, status),
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


def build_pipeline() -> EmbeddingPipeline:
    return EmbeddingPipeline(
        repository=PostgresEmbeddingRepository(connection),
        vectorizer=build_embedding_service(settings),
        batch_size=settings.embedding_batch_size,
        max_retries=settings.embedding_max_retries,
        retry_backoff=settings.embedding_retry_backoff,
    )


def build_processor() -> ProcessorService:
    return ProcessorService(
        repository=PostgresDocumentRepository(connection),
        downloader=downloader,
        extractor=PdfExtractor(),
        bucket=settings.s3_bucket,
    )


@live
def test_full_embed_after_processing_a_planted_pdf():
    storage_key = f"{THE_ORG}/{uuid.uuid4()}.pdf"
    document_id = seed_document(connection, "pdf", storage_key)
    downloader.put(storage_key, (FIXTURES / "three_pages.pdf").read_bytes())

    try:
        processed = build_processor().process(document_id)
        assert processed.status == "ready"

        result = build_pipeline().embed_document(document_id)
        assert result.status == "embedded"
        assert result.chunks == 1
        assert result.vectors == 1
        assert result.model == MODEL
        assert result.dimensions == DIMS

        rows = verify_conn.execute(
            "SELECT chunk_id, model, dimensions, vector_dims(embedding) AS dims, organization_id "
            "FROM embeddings WHERE chunk_id IN "
            "(SELECT id FROM chunks WHERE document_id = %s)",
            (document_id,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["model"] == MODEL
        assert rows[0]["dimensions"] == DIMS
        assert rows[0]["dims"] == DIMS
        assert str(rows[0]["organization_id"]) == THE_ORG

        state = verify_conn.execute(
            "SELECT status, embedding_status, embedding_error "
            "FROM documents WHERE id = %s",
            (document_id,),
        ).fetchone()
        assert state["status"] == "ready"
        assert state["embedding_status"] == "ready"
        assert state["embedding_error"] is None
    finally:
        cleanup_document(connection, document_id, storage_key)


@live
def test_chapter_pdf_embeds_one_vector_per_chunk():
    storage_key = f"{THE_ORG}/{uuid.uuid4()}.pdf"
    document_id = seed_document(connection, "pdf", storage_key)
    downloader.put(storage_key, (FIXTURES / "chapter.pdf").read_bytes())

    try:
        processed = build_processor().process(document_id)
        assert processed.status == "ready"
        assert processed.chunks >= 2

        result = build_pipeline().embed_document(document_id)
        assert result.status == "embedded"
        assert result.vectors == processed.chunks

        rows = verify_conn.execute(
            "SELECT count(*) AS n, count(DISTINCT model) AS models, "
            "count(DISTINCT vector_dims(embedding)) AS dims_set "
            "FROM embeddings "
            "WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id = %s)",
            (document_id,),
        ).fetchone()
        assert rows["n"] == processed.chunks
        assert rows["models"] == 1
        assert rows["dims_set"] == 1
    finally:
        cleanup_document(connection, document_id, storage_key)


@live
def test_already_embedded_document_is_refused_and_leaves_rows_alone():
    storage_key = f"{THE_ORG}/{uuid.uuid4()}.pdf"
    document_id = seed_document(connection, "pdf", storage_key)
    downloader.put(storage_key, (FIXTURES / "three_pages.pdf").read_bytes())

    try:
        build_processor().process(document_id)
        assert build_pipeline().embed_document(document_id).status == "embedded"

        second = build_pipeline().embed_document(document_id)
        assert second.status == "not_embeddable"
        assert "already embedded" in second.detail

        count = verify_conn.execute(
            "SELECT count(*) AS n FROM embeddings "
            "WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id = %s)",
            (document_id,),
        ).fetchone()["n"]
        assert count == 1  # refused second run left no duplicate vectors
    finally:
        cleanup_document(connection, document_id, storage_key)


@live
def test_failed_document_is_never_embeddable():
    storage_key = f"{THE_ORG}/{uuid.uuid4()}.pdf"
    document_id = seed_document(connection, "pdf", storage_key)
    downloader.put(storage_key, (FIXTURES / "corrupt.pdf").read_bytes())

    try:
        processed = build_processor().process(document_id)
        assert processed.status == "failed"

        result = build_pipeline().embed_document(document_id)
        assert result.status == "not_embeddable"
        assert "not ready" in result.detail

        state = verify_conn.execute(
            "SELECT embedding_status FROM documents WHERE id = %s", (document_id,)
        ).fetchone()
        assert state["embedding_status"] == "none"
    finally:
        cleanup_document(connection, document_id, storage_key)


@live
def test_ready_document_without_chunks_is_refused_instead_of_faking_ready():
    document_id = seed_document(
        connection, "pdf", None, status="ready"
    )

    try:
        result = build_pipeline().embed_document(document_id)
        assert result.status == "not_embeddable"
        assert "no chunks" in result.detail

        state = verify_conn.execute(
            "SELECT embedding_status FROM documents WHERE id = %s", (document_id,)
        ).fetchone()
        assert state["embedding_status"] == "none"
    finally:
        cleanup_document(connection, document_id, None)