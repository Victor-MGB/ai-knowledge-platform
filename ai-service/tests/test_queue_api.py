"""Day 12 (API): the /v1/queue endpoints with a fake QueueService injected.

Covers: enqueueing triggers the facade, unknown documents map to 404, Redis
being down maps to 503 (upload path stays alive), and GET reports the derived
state. Endpoint -> service behavior is trivial; the state machine itself is
exercised in test_queue.py.
"""

import pytest
from starlette.testclient import TestClient

from app.main import create_app
from app.queue.schemas import JobRecord, QueueStatus


class FakeJobLedger:
    def __init__(self, known: set[str]):
        self.known = known
        self.submits: list[tuple[str, str]] = []
        self.redis_down = False

    def enqueue_process(self, document_id):
        if self.redis_down:
            raise ConnectionError("redis connection refused")
        if document_id not in self.known:
            return None, None
        self.submits.append(("process", document_id))
        return _job(document_id, "process"), ("queued", "none")

    def enqueue_embed(self, document_id):
        if self.redis_down:
            raise ConnectionError("redis connection refused")
        if document_id not in self.known:
            return None, None
        self.submits.append(("embed", document_id))
        return _job(document_id, "embed"), ("ready", "none")

    def status(self, document_id):
        if document_id not in self.known:
            return None
        if document_id == "ready-doc":
            return QueueStatus(
                document_id=document_id,
                state="READY",
                process_job=_job(document_id, "process", status="succeeded"),
                embed_job=_job(document_id, "embed", status="succeeded"),
            )
        return QueueStatus(
            document_id=document_id, state="UPLOADED", process_job=_job(document_id, "process")
        )


def _job(document_id, kind, status="queued"):
    return JobRecord(
        job_id=f"{document_id}-{kind}-job",
        organization_id="org-1",
        document_id=document_id,
        kind=kind,
        status=status,
        attempts=0,
        max_attempts=3,
    )


@pytest.fixture()
def client_with_queue():
    with TestClient(create_app()) as client:
        client.app.state.queue_service = FakeJobLedger({"doc-1", "ready-doc"})
        yield client, client.app.state.queue_service


def test_enqueue_process_returns_status_and_submits_work(client_with_queue):
    client, ledger = client_with_queue
    res = client.post("/v1/queue/documents/doc-1/process")
    assert res.status_code == 200
    body = res.json()
    assert body["state"] == "UPLOADED"
    assert body["process_job"]["kind"] == "process"
    assert ledger.submits == [("process", "doc-1")]


def test_enqueue_embed_returns_embeddable_state(client_with_queue):
    client, ledger = client_with_queue
    res = client.post("/v1/queue/documents/doc-1/embed")
    assert res.status_code == 200
    assert res.json()["state"] == "UPLOADED"
    assert ledger.submits == [("embed", "doc-1")]


def test_unknown_document_is_404(client_with_queue):
    client, _ = client_with_queue
    assert client.post("/v1/queue/documents/nope/process").status_code == 404
    assert client.post("/v1/queue/documents/nope/embed").status_code == 404
    assert client.get("/v1/queue/documents/nope").status_code == 404


def test_redis_down_is_503_not_fatal(client_with_queue):
    client, ledger = client_with_queue
    ledger.redis_down = True
    res = client.post("/v1/queue/documents/doc-1/process")
    assert res.status_code == 503
    assert res.json()["detail"]["status"] == "queue_unavailable"


def test_status_reports_ready(client_with_queue):
    client, _ = client_with_queue
    res = client.get("/v1/queue/documents/ready-doc")
    assert res.status_code == 200
    body = res.json()
    assert body["state"] == "READY"
    assert body["embed_job"]["status"] == "succeeded"