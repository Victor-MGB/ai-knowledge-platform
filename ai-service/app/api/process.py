from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, Field

from ..core.config import get_settings
from ..processor import build_processor_service
from ..processor.chunkers import ChunkingStrategy

router = APIRouter(prefix="/process", tags=["process"])


class ProcessRequest(BaseModel):
    """Optional controls for one processing run; safe defaults otherwise.

    `chunk_size` is the budget the chosen strategy respects: characters for
    `fixed`, estimated tokens for `token` / `overlap` / `paragraph`.
    `overlap_tokens` applies to the `overlap` strategy only.
    """

    chunk_strategy: ChunkingStrategy = ChunkingStrategy.PARAGRAPH
    chunk_size: int | None = Field(default=None, ge=16, le=4096)
    overlap_tokens: int | None = Field(default=None, ge=0, le=2047)


def _processor(request: Request):
    """Lazily resolve the processor: tests inject a fake, production builds one."""
    service = getattr(request.app.state, "processor_service", None)
    if service is None:
        service = build_processor_service(get_settings())
        request.app.state.processor_service = service
    return service


@router.post("/documents/{document_id}")
def process_document(
    document_id: str,
    request: Request,
    body: Annotated[ProcessRequest | None, Body()] = None,
):
    """Run the Day 9-10 pipeline (extract + chunk) for one queued PDF document.

    Idempotent claim: a document that is not `queued` (already processing,
    ready or failed) is refused with 409 rather than re-processed.
    """
    try:
        result = _processor(request).process(
            document_id,
            body.chunk_strategy if body else None,
            chunk_tokens=body.chunk_size if body else None,
            chunk_chars=body.chunk_size if body else None,
            overlap_tokens=body.overlap_tokens if body else None,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"document_id": document_id, "status": "invalid_params", "reason": str(exc)},
        ) from exc

    mapping = {
        "ready": 200,
        "not_found": 404,
        "not_queued": 409,
        "unsupported_type": 422,
        "failed": 500,
    }
    if result.status == "failed":
        # stored failure details for the audit trail; the client sees the
        # same reason via the document's error field
        raise HTTPException(
            status_code=500,
            detail={
                "document_id": result.document_id,
                "status": result.status,
                "reason": result.detail,
                "pages": result.pages,
                "chunks": result.chunks,
            },
        )
    if result.status != "ready":
        raise HTTPException(
            status_code=mapping[result.status],
            detail={
                "document_id": result.document_id,
                "status": result.status,
                "reason": result.detail,
            },
        )
    return result.model_dump()