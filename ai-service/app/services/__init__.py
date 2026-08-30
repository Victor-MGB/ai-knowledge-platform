"""Service container - builds providers from settings.

Raise at startup (not at request time) if configuration is impossible, so a
misconfigured container fails fast and loudly instead of 500ing once live.
"""

from ..core.config import Settings
from .embedding import (
    EmbeddingService,
    HashingEmbeddingProvider,
    OpenAIEmbeddingProvider,
)
from .llm import LLMService, MockLLMProvider, OpenAILLMProvider


def build_embedding_service(settings: Settings) -> EmbeddingService:
    if settings.embedding_provider == "hash":
        return EmbeddingService(
            HashingEmbeddingProvider(settings.embedding_model, settings.embedding_dimensions)
        )
    if settings.embedding_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("embedding_provider=openai requires OPENAI_API_KEY")
        return EmbeddingService(
            OpenAIEmbeddingProvider(
                settings.openai_api_key, settings.openai_base_url, settings.openai_embedding_model
            )
        )
    raise RuntimeError(f"unknown embedding_provider: {settings.embedding_provider!r}")


def build_llm_service(settings: Settings) -> LLMService:
    if settings.llm_provider == "mock":
        return LLMService(MockLLMProvider(settings.llm_model))
    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("llm_provider=openai requires OPENAI_API_KEY")
        return LLMService(
            OpenAILLMProvider(
                settings.openai_api_key, settings.openai_base_url, settings.openai_llm_model
            )
        )
    raise RuntimeError(f"unknown llm_provider: {settings.llm_provider!r}")