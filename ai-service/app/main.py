from fastapi import FastAPI, Request
from fastapi.responses import Response

from . import middleware
from .api import chat, embed, embeddings, health, process, queue, rag
from .core.config import get_settings
from .core.logging import configure_logging
from .core.metrics import render_metrics
from .services import build_embedding_service, build_llm_service, build_rag_service

DESCRIPTION = (
    "KnowFlow AI service - embedding generation and chat completions behind "
    "provider interfaces, so local mock/hash providers and real OpenAI-compatible "
    "endpoints are interchangeable by configuration alone, plus the Day 9-10 "
    "document processor (/v1/process) that turns queued PDF uploads into "
    "per-page extracted text and retrieval-ready chunks, the Day-11 "
    "embedding phase (/v1/embed) that vectorizes those chunks into `embeddings` "
    "with batched, retried, validated provider calls, and the Day-12 "
    "background queue (/v1/queue) that hands both phases to an RQ worker pool "
    "with a durable job ledger, retries and a derived processing state "
    "(UPLOADED -> PROCESSING -> EMBEDDING -> READY), and the Day-17 RAG "
    "generation endpoint (/v1/rag/generate) that turns a question plus "
    "retrieved context into a grounded answer with bounded context and an "
    "honest \"I don't know\" guardrail."
)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.debug, json_format=settings.environment == "production")

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=DESCRIPTION,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    middleware.install(app)

    app.state.settings = settings
    app.state.embedding_service = build_embedding_service(settings)
    app.state.llm_service = build_llm_service(settings)
    app.state.rag_service = build_rag_service(settings)
    # Day 9: the processor is built lazily on first /v1/process request (tests
    # inject a fake into app.state.processor_service instead)
    app.state.processor_service = None
    # Day 11: the embedding pipeline holds its own DB connection, so it too is
    # built lazily on first /v1/embed request (tests inject a fake instead)
    app.state.embedding_pipeline = None
    # Day 12: the queue facade opens its own DB connection, so it is built
    # lazily on first /v1/queue request (tests inject a fake instead)
    app.state.queue_service = None

    app.include_router(health.router)
    app.include_router(embeddings.router, prefix=settings.api_prefix)
    app.include_router(chat.router, prefix=settings.api_prefix)
    app.include_router(rag.router, prefix=settings.api_prefix)
    app.include_router(process.router, prefix=settings.api_prefix)
    app.include_router(embed.router, prefix=settings.api_prefix)
    app.include_router(queue.router, prefix=settings.api_prefix)

    # Day 30 — Prometheus scrape endpoint (text format, standard port layout:
    # the compose Prometheus scrapes ai-service:8000/metrics).
    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(
            content=render_metrics(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

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