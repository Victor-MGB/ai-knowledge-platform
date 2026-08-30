from fastapi import APIRouter, Request

from ..models.results import Usage
from ..schemas import EmbeddingData, EmbeddingRequest, EmbeddingResponse

router = APIRouter(tags=["embeddings"])


@router.post("/embeddings", response_model=EmbeddingResponse)
def create_embedding(body: EmbeddingRequest, request: Request) -> EmbeddingResponse:
    service = request.app.state.embedding_service
    result = service.embed(body.text)
    return EmbeddingResponse(
        data=[EmbeddingData(embedding=result.vector)],
        model=result.model,
        provider=result.provider,
        dimensions=result.dimensions,
        usage=Usage(prompt_tokens=len(body.text.split()), total_tokens=len(body.text.split())),
    )