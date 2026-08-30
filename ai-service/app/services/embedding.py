"""Embedding providers and the service facade.

The rest of the app only talks to EmbeddingService. Providers implement the
same EmbeddingProvider protocol, so swapping the local hashing vectorizer for
a real model (OpenAI-compatible /v1/embeddings) is configuration, not code.

The local provider stands on Day 2's lesson: text -> fixed-length vector,
L2-normalized so cosine distance behaves as expected. It is deterministic and
offline - a testable stand-in for all-MiniLM-L6-v2 / text-embedding-3-small.
"""

import math
import re
from hashlib import sha256
from typing import Protocol

import httpx

from ..core.config import Settings
from ..models.results import EmbeddingResult

TOKEN_RE = re.compile(r"[a-z0-9']+")


class EmbeddingProvider(Protocol):
    name: str
    model: str
    dimensions: int | None

    def embed(self, text: str) -> EmbeddingResult: ...


class HashingEmbeddingProvider:
    """Feature-hashing vectorizer: each token maps to one of `dimensions`
    slots (signed by its hash) so any text embeds deterministically. This is
    the same geometry real models use, minus learned semantics."""

    name = "hash"

    def __init__(self, model: str, dimensions: int = 384):
        self.model = model
        self.dimensions = dimensions

    def embed(self, text: str) -> EmbeddingResult:
        vector = [0.0] * self.dimensions
        for token in TOKEN_RE.findall(text.lower()):
            digest = sha256(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        vector = [round(v / norm, 6) for v in vector]
        return EmbeddingResult(
            vector=vector,
            dimensions=self.dimensions,
            model=self.model,
            provider=self.name,
        )


class OpenAIEmbeddingProvider:
    """Calls any OpenAI-compatible /v1/embeddings endpoint over HTTP."""

    name = "openai"
    dimensions: int | None = None  # known only after the first remote call

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def embed(self, text: str) -> EmbeddingResult:
        response = httpx.post(
            f"{self.base_url}/v1/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": text},
            timeout=30,
        )
        response.raise_for_status()
        vector = response.json()["data"][0]["embedding"]
        return EmbeddingResult(
            vector=vector,
            dimensions=len(vector),
            model=self.model,
            provider=self.name,
        )


class EmbeddingService:
    def __init__(self, provider: EmbeddingProvider):
        self.provider = provider

    @property
    def provider_name(self) -> str:
        return self.provider.name

    @property
    def model(self) -> str:
        return self.provider.model

    @property
    def dimensions(self) -> int | None:
        return self.provider.dimensions

    def embed(self, text: str) -> EmbeddingResult:
        return self.provider.embed(text)