from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.processor.schemas import ProcessingResult


class FakeProcessor:
    """Drop-in ProcessorService: maps document id -> a scripted outcome."""

    def __init__(self, outcomes: dict[str, ProcessingResult]):
        self.outcomes = outcomes
        self.calls: list[tuple[str, object, dict]] = []

    def process(self, document_id: str, strategy=None, **kwargs) -> ProcessingResult:
        self.calls.append((document_id, strategy, kwargs))
        return self.outcomes[document_id]


@pytest.fixture()
def processor_client(client: TestClient) -> TestClient:
    fake = FakeProcessor(
        {
            "ready-1": ProcessingResult(
                document_id="ready-1", status="ready", pages=3, total_tokens=12
            ),
            "ghost": ProcessingResult(document_id="ghost", status="not_found"),
            "busy": ProcessingResult(
                document_id="busy", status="not_queued", detail="current status is processing"
            ),
            "docx": ProcessingResult(
                document_id="docx", status="unsupported_type", detail="no parser for source_type=docx yet"
            ),
            "broken": ProcessingResult(
                document_id="broken", status="failed", detail="pdf extraction failed: unreadable PDF"
            ),
        }
    )
    client.app.state.processor_service = fake
    return client


def test_ready_returns_the_processing_result(processor_client: TestClient):
    response = processor_client.post("/v1/process/documents/ready-1")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["pages"] == 3
    assert body["document_id"] == "ready-1"
    document_id, strategy, kwargs = processor_client.app.state.processor_service.calls[-1]
    assert document_id == "ready-1"
    assert strategy is None  # no body -> production default inside the service
    assert kwargs == {"chunk_tokens": None, "chunk_chars": None, "overlap_tokens": None}


def test_request_body_forwards_strategy_and_budgets(processor_client: TestClient):
    response = processor_client.post(
        "/v1/process/documents/ready-1",
        json={"chunk_strategy": "overlap", "chunk_size": 384, "overlap_tokens": 96},
    )

    assert response.status_code == 200
    _, strategy, kwargs = processor_client.app.state.processor_service.calls[-1]
    assert strategy == "overlap"
    assert kwargs == {
        "chunk_tokens": 384,
        "chunk_chars": 384,
        "overlap_tokens": 96,
    }


def test_unknown_strategy_is_rejected_by_validation(processor_client: TestClient):
    response = processor_client.post(
        "/v1/process/documents/ready-1",
        json={"chunk_strategy": "kaboom"},
    )

    assert response.status_code == 422
    assert processor_client.app.state.processor_service.calls == []


def test_implausible_chunk_size_is_rejected_by_validation(processor_client: TestClient):
    response = processor_client.post(
        "/v1/process/documents/ready-1",
        json={"chunk_strategy": "token", "chunk_size": 4},
    )

    assert response.status_code == 422
    assert processor_client.app.state.processor_service.calls == []


def test_not_found_maps_to_404(processor_client: TestClient):
    response = processor_client.post("/v1/process/documents/ghost")
    assert response.status_code == 404
    assert response.json()["detail"]["status"] == "not_found"


def test_not_queued_maps_to_409(processor_client: TestClient):
    response = processor_client.post("/v1/process/documents/busy")
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "current status is processing"


def test_unsupported_type_maps_to_422(processor_client: TestClient):
    response = processor_client.post("/v1/process/documents/docx")
    assert response.status_code == 422
    assert response.json()["detail"]["status"] == "unsupported_type"


def test_failed_maps_to_500_and_keeps_the_reason(processor_client: TestClient):
    response = processor_client.post("/v1/process/documents/broken")
    assert response.status_code == 500
    assert "unreadable PDF" in response.json()["detail"]["reason"]