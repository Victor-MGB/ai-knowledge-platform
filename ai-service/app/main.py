from fastapi import APIRouter, FastAPI, Request

from .api import chat, embeddings, health
from .core.config import get_settings
from .core.logging import configure_logging
from .services import build_embedding_service, build_llm_service

DESCRIPTION = (
    "KnowFlow AI service - embedding generation and chat completions behind "
    "provider interfaces, so local mock/hash providers and real OpenAI-compatible "
    "endpoints are interchangeable by configuration alone."
)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.debug)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=DESCRIPTION,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.state.settings = settings
    app.state.embedding_service = build_embedding_service(settings)
    app.state.llm_service = build_llm_service(settings)

    app.include_router(health.router)
    app.include_router(embeddings.router, prefix=settings.api_prefix)
    app.include_router(chat.router, prefix=settings.api_prefix)

    @app.get("/", include_in_schema=False)
    def root(request: Request) -> dict:
        return {
            "app": settings.app_name,
            "version": settings.app_version,
            "environment": settings.environment,
            "docs": request.app.docs_url,
        }

    return app


app = create_app()