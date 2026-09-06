from fastapi import APIRouter, HTTPException, Request

from ..core.config import get_settings
from ..embedding import build_embedding_pipeline

router = APIRouter(prefix="/embed", tags=["embed"])

_MAPPING = {
    "embedded": 200,
    "not_found": 404,
    "not_embeddable": 409,
    "failed": 500,
}


def _pipeline(request: Request):
    """Lazily resolve the pipeline: tests inject a fake, production builds one."""
    service = getattr(request.app.state, "embedding_pipeline", None)
    if service is None:
        service = build_embedding_pipeline(get_settings())
        request.app.state.embedding_pipeline = service
    return service


@router.post("/documents/{document_id}")
def embed_document(document_id: str, request: Request):
    """Run the Day-11 embedding phase for one ready document.

    Idempotent claim: a document is embeddable only while embedding_status is
    none or failed; an already-embedded or in-flight document is refused with
    409 rather than double-embedded. Provider choice comes from configuration
    (EMBEDDING_PROVIDER), not the caller.
    """
    result = _pipeline(request).embed_document(document_id)

    if result.status == "failed":
        # the stored failure is the audit trail; the client sees the same
        # reason via the document's embedding_error field
        raise HTTPException(
            status_code=500,
            detail={
                "document_id": result.document_id,
                "status": result.status,
                "reason": result.detail,
                "chunks": result.chunks,
                "vectors": result.vectors,
            },
        )
    if result.status != "embedded":
        raise HTTPException(
            status_code=_MAPPING[result.status],
            detail={
                "document_id": result.document_id,
                "status": result.status,
                "reason": result.detail,
            },
        )
    return result.model_dump()