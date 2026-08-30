"""LLM providers and the service facade.

Mirror image of the embedding layer: LLMService talks to an LLMProvider chosen
by configuration. The mock provider keeps the whole service functional and
testable offline; the OpenAI-compatible provider switches in a real completion
endpoint (including OPENAI_BASE_URL for local/bridge servers like llama.cpp).
"""

from typing import Protocol

import httpx

from ..core.config import Settings
from ..models.results import CompletionResult, Usage

Message = dict  # {role: "system"|"user"|"assistant", content: str}


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, messages: list[Message], temperature: float) -> CompletionResult: ...


class MockLLMProvider:
    """Deterministic placeholder: echoes the last user message and reports
    token counts. Clearly labeled as a mock in the reply payload."""

    name = "mock"

    def __init__(self, model: str):
        self.model = model

    def complete(self, messages: list[Message], temperature: float) -> CompletionResult:
        last_user = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        reply = (
            f"[mock  ·  no LLM configured] You asked: {last_user!r}. "
            "Set LLM_PROVIDER=openai and OPENAI_API_KEY for a real completion."
        )
        prompt_tokens = sum(len(str(m.get("content", "")).split()) for m in messages)
        completion_tokens = len(reply.split())
        return CompletionResult(
            reply=reply,
            model=self.model,
            provider=self.name,
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )


class OpenAILLMProvider:
    """Calls any OpenAI-compatible /v1/chat/completions endpoint over HTTP."""

    name = "openai"

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def complete(self, messages: list[Message], temperature: float) -> CompletionResult:
        response = httpx.post(
            f"{self.base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": messages, "temperature": temperature},
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()
        choice = body["choices"][0]
        usage = body.get("usage", {})
        return CompletionResult(
            reply=choice["message"]["content"],
            model=body.get("model", self.model),
            provider=self.name,
            usage=Usage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            ),
            finish_reason=choice.get("finish_reason", "stop"),
        )


class LLMService:
    def __init__(self, provider: LLMProvider):
        self.provider = provider

    @property
    def provider_name(self) -> str:
        return self.provider.name

    @property
    def model(self) -> str:
        return self.provider.model

    def complete(self, messages: list[Message], temperature: float = 0.0) -> CompletionResult:
        return self.provider.complete(messages, temperature)