from typing import Annotated

from fastapi import APIRouter, HTTPException, Request

from ..core.config import get_settings
from ..queue import QueueService
from ..queue.schemas import QueueStatus
from ..queue.service import build_api_queue_service

router = APIRouter(prefix="/queue", tags=["queue"])

# Day 12: enqueue endpoints hand documents to the RQ worker pool and report
# the derived pipeline state (UPLOADED / PROCESSING / EMBEDDING / READY /
# FAILED). The backend calls /documents/{id}/process right after an upload —
# from then on workers drive process -> embed without further HTTP. Redis
# being down is non-fatal here: the row stays queued, the upload survives,
# and a later sweep can re-submit (see the ingestion_jobs design notes).


def _queue(request: Request) -> QueueService:
    """Lazily resolve the queue facade: tests inject a fake, production builds
    one. Building is deferred because it opens a Postgres connection."""
    service = getattr(request.app.state, "queue_service", None)
    if service is None:
        service = build_api_queue_service(get_settings())
        request.app.state.queue_service = service
    return service


@router.post("/documents/{document_id}/process", response_model=QueueStatus)
def enqueue_process(document_id: str, request: Request):
    """Extract + chunk a queued document on the worker pool (chain -- if the
    process run succeeds the worker enqueues the embed phase itself)."""
    try:
        service = _queue(request)
        job, _ = service.enqueue_process(document_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail={"status": "not_found", "reason": "no such document"},
            )
        return service.status(document_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"status": "queue_unavailable", "reason": f"could not enqueue: {exc}"},
        ) from exc


@router.post("/documents/{document_id}/embed", response_model=QueueStatus)
def enqueue_embed(document_id: str, request: Request):
    """Vectorize a ready document's chunks on the worker pool (diagram path:
    a process run normally chains this automatically; enqueueing manually is
    the re-run/repair path)."""
    try:
        service = _queue(request)
        job, _ = service.enqueue_embed(document_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail={"status": "not_found", "reason": "no such document"},
            )
        return service.status(document_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"status": "queue_unavailable", "reason": f"could not enqueue: {exc}"},
        ) from exc


@router.get("/documents/{document_id}", response_model=QueueStatus)
def get_queue_status(document_id: str, request: Request):
    """Current derived pipeline state plus the process/embed job ledger rows."""
    status = _queue(request).status(document_id)
    if status is None:
        raise HTTPException(
            status_code=404,
            detail={"status": "not_found", "reason": "no such document"},
        )
    return status