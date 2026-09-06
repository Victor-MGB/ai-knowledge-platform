from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.embedding.schemas import EmbeddingPipelineResult


class FakePipeline:
    """Drop-in EmbeddingPipeline: maps document id -> a scripted outcome."""

    def __init__(self, outcomes: dict[str, EmbeddingPipelineResult]):
        self.outcomes = outcomes
        self.calls: list[str] = []

    def embed_document(self, document_id: str) -> EmbeddingPipelineResult:
        self.calls.append(document_id)
        return self.outcomes[document_id]


@pytest.fixture()
def embed_client(client: TestClient) -> TestClient:
    fake = FakePipeline(
        {
            "embedded-1": EmbeddingPipelineResult(
                document_id="embedded-1",
                status="embedded",
                chunks=3,
                vectors=3,
                model="knowflow-hash-384",
                dimensions=384,
                detail="embedded 3 chunk(s) with knowflow-hash-384",
            ),
            "ghost": EmbeddingPipelineResult(document_id="ghost", status="not_found"),
            "notready": EmbeddingPipelineResult(
                document_id="notready",
                status="not_embeddable",
                detail="document status is queued, not ready",
            ),
            "done": EmbeddingPipelineResult(
                document_id="done",
                status="not_embeddable",
                detail="document already embedded (embedding_status=ready)",
            ),
            "broken": EmbeddingPipelineResult(
                document_id="broken",
                status="failed",
                detail="embedding batch failed (transient failure, retries exhausted): ConnectError: down",
            ),
        }
    )
    client.app.state.embedding_pipeline = fake
    return client


def test_embedded_returns_the_result(embed_client: TestClient):
    response = embed_client.post("/v1/embed/documents/embedded-1")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "embedded"
    assert body["chunks"] == 3
    assert body["vectors"] == 3
    assert body["model"] == "knowflow-hash-384"
    assert body["dimensions"] == 384
    assert embed_client.app.state.embedding_pipeline.calls == ["embedded-1"]


def test_not_found_maps_to_404(embed_client: TestClient):
    response = embed_client.post("/v1/embed/documents/ghost")
    assert response.status_code == 404
    assert response.json()["detail"]["status"] == "not_found"


def test_not_embeddable_maps_to_409_with_reason(embed_client: TestClient):
    response = embed_client.post("/v1/embed/documents/notready")
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "document status is queued, not ready"

    already = embed_client.post("/v1/embed/documents/done")
    assert already.status_code == 409
    assert "already embedded" in already.json()["detail"]["reason"]


def test_failed_maps_to_500_and_keeps_the_reason(embed_client: TestClient):
    response = embed_client.post("/v1/embed/documents/broken")
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "retries exhausted" in detail["reason"]
    assert detail["vectors"] == 0