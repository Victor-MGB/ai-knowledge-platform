"""RAG generation facade: enforce the contract, hand off to the provider.

Owns the guardrails that apply to every provider:
  * context budget — drops the lowest-similarity items so the payload stays
    inside `max_context_tokens` (soft: the strongest item always survives),
    ordering kept best-first so `evidence[0]` is the strongest;
  * empty-context refusal — no evidence means the only honest answer is the
    canonical "I don't know.", decided here so every provider behaves alike;
  * similarity floor — `min_score` (optional) refuses when even the
    strongest surviving evidence sits below the caller's bar.

The provider then decides ground-level sufficiency (the extractive baseline
also refuses when no sentence shares a content token with the question).
"""

from typing import Generator

from ..processor.chunkers import estimate_tokens
from .prompting import DEFAULT_SYSTEM_PROMPT, DONT_KNOW
from .provider import RagProvider, StreamEvent
from .schemas import (
    RagGenerationRequest,
    RagGenerationResponse,
)


def _budget_context(items: list, budget: int) -> list:
    """Best-first context that fits the budget. `items` is sorted by
    similarity descending, stable for ties; the strongest item always
    survives even if it alone would exceed the budget (an answer needs at
    least one evidence unit)."""
    ordered = sorted(
        items,
        key=lambda item: item.similarity if item.similarity is not None else -1.0,
        reverse=True,
    )
    kept: list = []
    total = 0
    for index, item in enumerate(ordered):
        tokens = estimate_tokens(item.text)
        if index > 0 and total + tokens > budget:
            continue
        kept.append(item)
        total += tokens
    return kept


class RAGGenerator:
    def __init__(
        self,
        provider: RagProvider,
        *,
        default_system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_context_tokens: int = 1200,
    ):
        self.provider = provider
        self.default_system_prompt = default_system_prompt
        self.max_context_tokens = max_context_tokens

    @property
    def provider_name(self) -> str:
        return self.provider.name

    @property
    def model(self) -> str:
        return self.provider.model

    def generate(self, request: RagGenerationRequest) -> RagGenerationResponse:
        budget = request.max_context_tokens or self.max_context_tokens
        context = _budget_context(request.context, budget)

        # guardrails shared by every provider:
        if not context:
            return self._refuse()
        if request.min_score is not None:
            best = max(
                (item.similarity if item.similarity is not None else 0.0)
                for item in context
            )
            if best < request.min_score:
                return self._refuse()

        result = self.provider.generate(
            request.question,
            context,
            history=request.history,
            system_prompt=request.system_prompt or self.default_system_prompt,
            temperature=request.temperature,
        )
        return RagGenerationResponse(
            answer=result.answer,
            refused=result.refused,
            provider=result.provider,
            model=result.model,
            evidence=result.evidence,
            citations=result.citations,
            usage=result.usage,
        )

    def generate_stream(
        self, request: RagGenerationRequest
    ) -> Generator["RagGenerationResponse | str", None, None]:
        """Yield answer tokens as they arrive, ending with the final response
        object (carrying citations/evidence/refused). Refuses stream nothing
        and immediately yields the empty-citation response."""
        budget = request.max_context_tokens or self.max_context_tokens
        context = _budget_context(request.context, budget)

        # guardrails — refuse before streaming anything
        if not context:
            yield self._refuse()
            return
        if request.min_score is not None:
            best = max(
                (item.similarity if item.similarity is not None else 0.0)
                for item in context
            )
            if best < request.min_score:
                yield self._refuse()
                return

        for event, value in self.provider.generate_stream(
            request.question,
            context,
            history=request.history,
            system_prompt=request.system_prompt or self.default_system_prompt,
            temperature=request.temperature,
        ):
            if event == "token":
                yield value  # a str token
            else:
                result = value
                yield RagGenerationResponse(
                    answer=result.answer,
                    refused=result.refused,
                    provider=result.provider,
                    model=result.model,
                    evidence=result.evidence,
                    citations=result.citations,
                    usage=result.usage,
                )

    def _refuse(self) -> RagGenerationResponse:
        return RagGenerationResponse(
            answer=DONT_KNOW,
            refused=True,
            provider=self.provider.name,
            model=self.provider.model,
            evidence=[],
            citations=[],
        )