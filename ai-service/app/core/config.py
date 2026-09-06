from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, loaded from environment variables + .env.

    Environment wins over .env (pydantic-settings precedence). The default
    provider set (hash embeddings + mock LLM) runs fully offline so the
    service is testable with zero keys; flip a provider to "openai" and add
    OPENAI_API_KEY to switch to a real model without touching code.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "KnowFlow AI Service"
    app_version: str = "0.1.0"
    environment: str = "development"
    debug: bool = False
    api_prefix: str = "/v1"

    embedding_provider: str = "hash"  # hash | openai
    embedding_model: str = "knowflow-hash-384"
    embedding_dimensions: int = 384
    openai_embedding_model: str = "text-embedding-3-small"

    llm_provider: str = "mock"  # mock | openai
    llm_model: str = "knowflow-mock-1"
    openai_llm_model: str = "gpt-4o-mini"

    # Day 17: RAG generation — grounded answers from retrieved context, produced
    # by the same provider-swap philosophy as embeddings/LLMs. `extract` (the
    # default) is a deterministic faithful-by-construction baseline that quotes
    # evidence verbatim and refuses honestly; `openai` prompt-packages the same
    # context into an OpenAI-compatible chat completion.
    rag_provider: str = "extract"  # extract | openai
    rag_model: str = "knowflow-extract-1"
    rag_default_system_prompt: str | None = None  # None -> built-in prompt
    rag_max_context_tokens: int = 1200  # defensive budget for /v1/rag/generate

    openai_base_url: str = "https://api.openai.com"
    openai_api_key: str | None = None

    # document processor (Day 9) — Postgres + S3/MinIO from the other services
    database_url: str = "postgres://knowflow:knowflow@localhost:5434/knowflow"
    s3_endpoint: str = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_bucket: str = "knowflow"
    s3_access_key: str = "knowflow"
    s3_secret_key: str = "knowflow123"
    s3_force_path_style: bool = True
    pdf_page_limit: int = 1000  # hostile-upload guard for extraction

    # embedding pipeline (Day 11) — batching + retry for document vectors
    embedding_batch_size: int = 64  # texts per provider request slice
    embedding_max_retries: int = 3  # retries per batch for transient failures
    embedding_retry_backoff: float = 0.5  # seconds, exponentiated per attempt

    # background processing (Day 12) — Redis + RQ worker pool
    redis_url: str = "redis://127.0.0.1:6375/0"
    queue_name: str = "knowflow"
    queue_max_attempts: int = 3  # retry budget per ingestion job
    queue_retry_backoff: float = 1.0  # seconds, exponentiated per attempt
    queue_worker_timeout: int = 600  # seconds an RQ worker may run one task

    # observability (Day 30) — Prometheus metrics on the API + worker processes
    metrics_enabled: bool = True
    metrics_port: int = 8001  # worker process scrape endpoint (api uses :8000/metrics)


@lru_cache
def get_settings() -> Settings:
    return Settings()