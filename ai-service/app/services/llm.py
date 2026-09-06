"""LLM providers and the service facade.

Mirror image of the embedding layer: LLMService talks to an LLMProvider chosen
by configuration. The mock provider keeps the whole service functional and
testable offline; the OpenAI-compatible provider switches in a real completion
endpoint (including OPENAI_BASE_URL for local/bridge servers like llama.cpp).

Day 27: ``complete_stream`` yields tokens one-by-one for SSE streaming.
"""

from typing import Generator, Protocol

import httpx

from ..core.config import Settings
from ..models.results import CompletionResult, Usage

Message = dict  # {role: "system"|"user"|"assistant", content: str}


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, messages: list[Message], temperature: float) -> CompletionResult: ...

    def complete_stream(self, messages: list[Message], temperature: float) -> Generator[str, None, None]: ...


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

    def complete_stream(self, messages: list[Message], temperature: float) -> Generator[str, None, None]:
        result = self.complete(messages, temperature)
        yield result.reply


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

    def complete_stream(self, messages: list[Message], temperature: float) -> Generator[str, None, None]:
        """Stream tokens from the OpenAI-compatible API via SSE."""
        import json as _json

        with httpx.stream(
            "POST",
            f"{self.base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "stream": True,
            },
            timeout=120,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload.strip() == "[DONE]":
                    break
                try:
                    chunk = _json.loads(payload)
                except _json.JSONDecodeError:
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content")
                if content:
                    yield content


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

    def complete_stream(self, messages: list[Message], temperature: float = 0.0) -> Generator[str, None, None]:
        yield from self.provider.complete_stream(messages, temperature)