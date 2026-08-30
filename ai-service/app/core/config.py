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

    openai_base_url: str = "https://api.openai.com"
    openai_api_key: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()