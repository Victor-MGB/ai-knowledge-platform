import time

from fastapi import APIRouter, Request

from ..core.metrics import record_ai_call
from ..models.results import Usage
from ..schemas import EmbeddingData, EmbeddingRequest, EmbeddingResponse

router = APIRouter(tags=["embeddings"])


@router.post("/embeddings", response_model=EmbeddingResponse)
def create_embedding(body: EmbeddingRequest, request: Request) -> EmbeddingResponse:
    service = request.app.state.embedding_service
    started = time.monotonic()
    result = service.embed(body.text)
    elapsed = time.monotonic() - started
    token_count = len(body.text.split())
    record_ai_call(
        "embeddings",
        result.provider,
        elapsed,
        {"prompt_tokens": token_count, "total_tokens": token_count},
    )
    return EmbeddingResponse(
        data=[EmbeddingData(embedding=result.vector)],
        model=result.model,
        provider=result.provider,
        dimensions=result.dimensions,
        usage=Usage(prompt_tokens=token_count, total_tokens=token_count),
    )