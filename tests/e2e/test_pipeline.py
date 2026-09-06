"""Day 14 — full-pipeline integration test (Milestone 1).

Drives the ENTIRE pipeline through public HTTP — no manual database seeding,
no manual /v1/process /v1/embed calls, no CLI:

    register org -> upload PDF -> storage -> queue -> RQ worker
      -> extraction -> chunking -> embedding -> pgvector -> READY

and asserts every committed outcome through a SECOND database connection (the
repo's "same-connection reads can always see uncommitted self-writes" rule).

Prereqs: knowflow-pg / knownflow-minio / knowflow-redis containers up, backend
on :3000, ai-service on :8001, and ONE RQ worker running (the ingestion the
upload triggers is consumed by it). Run everything via e2e/run.sh — that
boots the servers + worker, runs this test, and tears down.

Nothing here calls the /v1/process or /v1/embed endpoints, and no rows are
inserted outside the API path (the only direct SQL is read-only verification,
plus end-of-test cleanup of the throwaway org).
"""

from __future__ import annotations

import math
import os
import time
import uuid
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

BACKEND_URL = os.environ.get("E2E_BACKEND_URL", "http://127.0.0.1:3000")
AI_SERVICE_URL = os.environ.get("E2E_AI_SERVICE_URL", "http://127.0.0.1:8001")
DATABASE_URL = os.environ.get(
    "E2E_DATABASE_URL", "postgres://knowflow:knowflow@localhost:5434/knowflow"
)
FIXTURE = (
    Path(__file__).parent.parent / "ai-service" / "tests" / "fixtures" / "chapter.pdf"
)
POLL_INTERVAL_S = 1.5
POLL_TIMEOUT_S = 180.0


def _reachable(url: str, timeout: float = 3.0) -> bool:
    try:
        return httpx.get(url, timeout=timeout).status_code < 500
    except httpx.HTTPError:
        return False


_guard = None
try:
    conn = psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)
    assert FIXTURE.exists(), f"missing fixture {FIXTURE}"
    assert _reachable(f"{BACKEND_URL}/health"), "backend not reachable on :3000"
    assert _reachable(f"{AI_SERVICE_URL}/health"), "ai-service not reachable on :8001"
    conn.execute("SELECT 1")
except Exception as exc:  # noqa: BLE001 - guard, not a test
    _guard = f"full stack unavailable: {type(exc).__name__}: {exc}"

live = pytest.mark.skipif(
    bool(_guard),
    reason=str(_guard) or "full stack missing (run e2e/run.sh to boot it)",
)


def register_org(client: httpx.Client) -> tuple[str, str]:
    """Public API only: creates a tenant + owner and returns (access_token, org_id)."""
    email = f"e2e-{uuid.uuid4()}@example.com"
    response = client.post(
        f"{BACKEND_URL}/auth/register",
        json={
            "email": email,
            "password": "Sup3rSecret!1",
            "organization": f"e2e-{uuid.uuid4().hex[:10]}",
            "name": "e2e runner",
        },
    )
    assert response.status_code == 201, f"register failed: {response.text}"
    body = response.json()
    return body["tokens"]["accessToken"], body["organization"]["id"]


def upload_pdf(client: httpx.Client, token: str) -> tuple[str, str]:
    """Public API only: upload the fixture PDF (any working directory)."""
    with FIXTURE.open("rb") as fh:
        response = client.post(
            f"{BACKEND_URL}/api/v1/documents",
            headers={"authorization": f"Bearer {token}"},
            files={"file": ("chapter.pdf", fh, "application/pdf")},
        )
    assert response.status_code == 201, f"upload failed: {response.text}"
    body = response.json()
    assert body["status"] == "queued", body
    assert body["storageKey"] is not None, body
    return body["id"], body["storageKey"]


def wait_for_ready(
    client: httpx.Client, token: str, document_id: str
) -> dict:
    """Poll the document status API through the worker's stage transitions."""
    deadline = time.monotonic() + POLL_TIMEOUT_S
    last_stage = None
    while time.monotonic() < deadline:
        response = client.get(
            f"{BACKEND_URL}/api/v1/documents/{document_id}/status",
            headers={"authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, f"status endpoint failed: {response.text}"
        body = response.json()
        stage = body["stage"]
        if stage != last_stage:
            print(f"      stage: {stage.upper()}  @ {_stamp()}  "
                  f"(pages={body['counts']['pages']} chunks={body['counts']['chunks']} "
                  f"embeddings={body['counts']['embeddings']})")
            last_stage = stage
        if xpath := body.get("error") or body.get("embeddingError"):
            raise AssertionError(f"pipeline failed: {xpath}")
        if stage in ("ready", "failed"):
            return body
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        "document never reached READY. The stack answers on :3000/:8001 but the "
        "upload produced no worker progress — is an RQ worker running "
        "(ai-service/.venv/bin/python -m app.queue.worker)? Run this via e2e/run.sh."
    )


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


@live
def test_upload_to_ready_through_public_api_only():
    with httpx.Client(timeout=30.0) as client:
        token, org_id = register_org(client)
        document_id, storage_key = upload_pdf(client, token)

        try:
            # ---- the no-manual-steps section: after the upload, the backend
            # enqueues and the worker drives extract -> chunk -> embed by itself
            print()
            final = wait_for_ready(client, token, document_id)
            assert final["stage"] == "ready", final
            assert final["embeddingStatus"] == "ready", final
            assert final["status"] == "ready", final
            assert final["error"] is None and final["embeddingError"] is None, final
            counts = final["counts"]
            assert counts["pages"] > 0, "extraction produced no pages"
            assert counts["chunks"] > 0, "chunking produced no chunks"
            assert counts["embeddings"] > 0, "embedding produced no vectors"
            print(f"      READY  @ {_stamp()}  front-desk confirms: {counts}")

            # ---- cross-check the AI service's own derived state (separate
            # projection, same five states)
            qs = client.get(f"{AI_SERVICE_URL}/v1/queue/documents/{document_id}")
            assert qs.status_code == 200, qs.text
            queue_state = qs.json()
            assert queue_state["state"] == "READY", queue_state
            assert queue_state["process_job"]["status"] == "succeeded", queue_state
            assert queue_state["embed_job"]["status"] == "succeeded", queue_state
            print("      queue API confirms READY with both jobs succeeded  "
                  f"@ {_stamp()}")

            # ---- committed state, read through a SECOND connection
            verify_second_connection(document_id, storage_key, org_id, counts)

            # ---- pgvector is genuinely queryable: re-embed a chunk and ask
            # the store for its distance (deterministic hash -> distance 0)
            verify_pgvector_queryable(client, document_id)

            # ---- Day 15: semantic retrieval over the pipeline's own corpus
            hits = client.post(
                f"{BACKEND_URL}/api/v1/search",
                headers={"authorization": f"Bearer {token}"},
                json={
                    "query": "how do chunks and retrieval preserve citation",
                    "limit": 5,
                },
            )
            assert hits.status_code == 200, hits.text
            response = hits.json()
            assert response["model"] == "knowflow-hash-384", response
            assert response["results"], "search found nothing in its own corpus"
            for result in response["results"]:
                assert result["chunk"]["content"]
                assert 0.0 <= result["similarity"] <= 1.0, result
                assert result["document"]["id"] == document_id, result
                assert result["page"] >= 1, result
                assert result["metadata"].get("strategy") in (
                    "paragraph",
                    "token",
                    "fixed",
                    "overlap",
                ), result
            similarities = [r["similarity"] for r in response["results"]]
            assert similarities == sorted(similarities, reverse=True), "not ordered best-first"
            print("      semantic search returns top-K over its own corpus  "
                  f"@ {_stamp()}  top: {similarities[0]:.3f}")

            # ---- Day 17: RAG generation — retrieve context, then generate
            # an answer with "I don't know" guardrails + evidence tracking
            rag = client.post(
                f"{BACKEND_URL}/api/v1/rag/generate",
                headers={"authorization": f"Bearer {token}"},
                json={
                    "question": "how do chunks and retrieval preserve citation",
                    "limit": 5,
                },
            )
            assert rag.status_code == 200, rag.text
            rag_body = rag.json()
            assert "answer" in rag_body, rag_body
            assert "refused" in rag_body, rag_body
            assert "evidence" in rag_body, rag_body
            assert "provider" in rag_body, rag_body
            assert "model" in rag_body, rag_body
            retrieval = rag_body["retrieval"]
            assert retrieval["model"] == "knowflow-hash-384", retrieval
            assert retrieval["retrieved"] >= 1, retrieval
            print(f"      RAG generate: refused={rag_body['refused']}  "
                  f"provider={rag_body['provider']}  retrieved={retrieval['retrieved']}  "
                  f"@ {_stamp()}")

            # the corpus is about document processing so extractive should
            # find at least one answer (not refuse)
            assert rag_body["refused"] is False, (
                f"expected a grounded answer, got refused: {rag_body['answer']}"
            )
            assert len(rag_body["evidence"]) >= 1, "no evidence returned"

            # a question completely outside the corpus must be refused (not hallucinated)
            rag_unk = client.post(
                f"{BACKEND_URL}/api/v1/rag/generate",
                headers={"authorization": f"Bearer {token}"},
                json={"question": "what is the quantum entanglement protocol"},
            )
            assert rag_unk.status_code == 200, rag_unk.text
            unk_body = rag_unk.json()
            assert unk_body["refused"] is True, (
                f"expected refusal for out-of-scope question, got: {unk_body['answer']}"
            )
            assert unk_body["retrieval"]["retrieved"] >= 0, unk_body
            print(f"      RAG refusal guardrail works for out-of-scope question  "
                  f"@ {_stamp()}")

            # ---- cleanup through the public API (delete), then drop the
            # throwaway org directly and prove nothing leaked
            deleted = client.delete(
                f"{BACKEND_URL}/api/v1/documents/{document_id}",
                headers={"authorization": f"Bearer {token}"},
            )
            assert deleted.status_code == 204, deleted.text
            gone = client.get(
                f"{BACKEND_URL}/api/v1/documents/{document_id}",
                headers={"authorization": f"Bearer {token}"},
            )
            assert gone.status_code == 404, gone.text
            print("      deleted via the API (204) — retrieval afterwards is 404  "
                  f"@ {_stamp()}")
        finally:
            # cleanup is allowed direct SQL (it is not "the pipeline"); it proves
            # the throwaway org + user + documents all cascade away
            with psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row) as c:
                c.execute("DELETE FROM organizations WHERE id = %s", (org_id,))

        assert_cleanup(document_id, org_id)


def verify_second_connection(
    document_id: str, storage_key: str, org_id: str, api_counts: dict
) -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row) as verify:
        doc = verify.execute(
            "SELECT status, embedding_status, error, embedding_error, storage_key "
            "FROM documents WHERE id = %s",
            (document_id,),
        ).fetchone()
        assert doc is not None, "document row missing (inserted outside a commit?)"
        assert doc["status"] == "ready" and doc["embedding_status"] == "ready", doc
        assert doc["error"] is None and doc["embedding_error"] is None, doc
        assert doc["storage_key"] == storage_key, doc

        pages = verify.execute(
            "SELECT count(*) AS n FROM document_pages WHERE document_id = %s",
            (document_id,),
        ).fetchone()["n"]
        chunks = verify.execute(
            "SELECT count(*) AS n FROM chunks WHERE document_id = %s",
            (document_id,),
        ).fetchone()["n"]
        embeddings = verify.execute(
            "SELECT count(*) AS n, min(dimensions) AS dims, min(model) AS model "
            "FROM embeddings WHERE chunk_id IN "
            "(SELECT id FROM chunks WHERE document_id = %s)",
            (document_id,),
        ).fetchone()
        assert pages == api_counts["pages"], (pages, api_counts)
        assert chunks == api_counts["chunks"], (chunks, api_counts)
        assert embeddings["n"] == chunks, embeddings
        assert embeddings["dims"] == 384, embeddings
        assert embeddings["model"] == "knowflow-hash-384", embeddings

        jobs = verify.execute(
            "SELECT kind, status, attempts, finished_at FROM ingestion_jobs "
            "WHERE document_id = %s ORDER BY kind",
            (document_id,),
        ).fetchall()
        assert {j["kind"]: j for j in jobs}["process"]["status"] == "succeeded", jobs
        assert {j["kind"]: j for j in jobs}["embed"]["status"] == "succeeded", jobs
        assert all(j["attempts"] == 1 and j["finished_at"] is not None for j in jobs)
    print("      second connection confirms documents/pages/chunks/embeddings/jobs  "
          f"@ {_stamp()}")


def verify_pgvector_queryable(client: httpx.Client, document_id: str) -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row) as verify:
        sample = verify.execute(
            "SELECT c.id, c.content, e.embedding, e.dimensions "
            "FROM chunks c JOIN embeddings e ON e.chunk_id = c.id "
            "WHERE c.document_id = %s ORDER BY c.chunk_index LIMIT 1",
            (document_id,),
        ).fetchone()
        assert sample is not None, "no chunk+embedding pair to probe"
        assert sample["dimensions"] == 384, sample

        # finite, non-zero 384-D vectors (a broken provider stores zeros/NaN)
        vecs = verify.execute(
            "SELECT e.embedding::text AS vec "
            "FROM chunks c JOIN embeddings e ON e.chunk_id = c.id "
            "WHERE c.document_id = %s",
            (document_id,),
        ).fetchall()
        assert vecs, "no stored vectors to sanity-check"
        for v in vecs:
            components = [float(x) for x in v["vec"].strip("[]").split(",")]
            assert len(components) == 384, v["vec"]
            assert all(math.isfinite(x) for x in components), v["vec"]
            assert any(x != 0.0 for x in components), "zero vector stored"

        # distinct chunks must not collapse to the same vector
        pair = verify.execute(
            "SELECT a.embedding <-> b.embedding AS dist "
            "FROM (SELECT embedding FROM chunks c JOIN embeddings e ON e.chunk_id = c.id "
            "      WHERE c.document_id = %s ORDER BY c.chunk_index LIMIT 2) a, "
            "     (SELECT embedding FROM chunks c JOIN embeddings e ON e.chunk_id = c.id "
            "      WHERE c.document_id = %s ORDER BY c.chunk_index LIMIT 2) b "
            "WHERE a.embedding <-> b.embedding > 0 LIMIT 1",
            (document_id, document_id),
        ).fetchone()
        assert pair is not None, "two chunks produced indistinguishable vectors"

        # re-embed the exact chunk text through the public embeddings endpoint
        # (hash provider: deterministic) and ask pgvector for the distance
        reembedded = client.post(
            f"{AI_SERVICE_URL}/v1/embeddings", json={"text": sample["content"]}
        )
        assert reembedded.status_code == 200, reembedded.text
        vector = reembedded.json()["data"][0]["embedding"]
        literal = "[" + ",".join(f"{v:.6f}" for v in vector) + "]"

        row = verify.execute(
            "SELECT e.embedding <-> %s::vector AS dist "
            "FROM chunks c JOIN embeddings e ON e.chunk_id = c.id "
            "WHERE c.document_id = %s AND c.id = %s",
            (literal, document_id, sample["id"]),
        ).fetchone()
        assert row is not None and abs(row["dist"]) < 1e-6, (
            "pgvector could not return the stored vector for the chunk's own text "
            f"(dist={row and row['dist']})"
        )
    print("      pgvector queryable: re-embedded chunk text finds its stored "
          f"vector at distance ~0  @ {_stamp()}")


def assert_cleanup(document_id: str, org_id: str) -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row) as verify:
        assert verify.execute(
            "SELECT count(*) AS n FROM organizations WHERE id = %s", (org_id,)
        ).fetchone()["n"] == 0
        assert verify.execute(
            "SELECT count(*) AS n FROM documents WHERE id = %s", (document_id,)
        ).fetchone()["n"] == 0
        for table in ("document_pages", "chunks", "ingestion_jobs"):
            n = verify.execute(
                f"SELECT count(*) AS n FROM {table} WHERE document_id = %s",
                (document_id,),
            ).fetchone()["n"]
            assert n == 0, f"{table}: {n} leaked rows for deleted document"
        leaked_vectors = verify.execute(
            "SELECT count(*) AS n FROM embeddings e "
            "JOIN chunks c ON c.id = e.chunk_id WHERE c.document_id = %s",
            (document_id,),
        ).fetchone()["n"]
        assert leaked_vectors == 0, f"embeddings: {leaked_vectors} leaked rows"
    print("      cleanup verified: org + document + pipeline rows fully removed  "
          f"@ {_stamp()}")