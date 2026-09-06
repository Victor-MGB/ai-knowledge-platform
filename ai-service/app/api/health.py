from fastapi import APIRouter, Request

from ..schemas import HealthResponse, ServiceStatus

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    settings = request.app.state.settings
    embedding = request.app.state.embedding_service
    llm = request.app.state.llm_service
    rag = request.app.state.rag_service
    return HealthResponse(
        status="ok",
        version=settings.app_version,
        environment=settings.environment,
        services={
            "embedding": ServiceStatus(
                provider=embedding.provider_name,
                model=embedding.model,
                dimensions=embedding.dimensions,
            ),
            "llm": ServiceStatus(provider=llm.provider_name, model=llm.model),
            "rag": ServiceStatus(provider=rag.provider_name, model=rag.model),
        },
    )