"""RAG generation providers.

Mirror image of the embedding layer's providers. The default `extract`
provider is deterministic and faithful **by construction**: it quotes a
verbatim sentence from the strongest evidence and refuses with a canonical
"I don't know." when nothing in the context addresses the question (no
content-token overlap). The `openai` provider prompt-packages the same
context into an OpenAI-compatible chat completion (same endpoint/key/format
as the embedding + LLM layers) and is instructed — and post-checked — for the
same honest-refusal behavior. `RAG_PROVIDER=openai` swaps it in without code
changes; the extract-output contract (answer/refused/evidence) is identical,
so a real model can power the same endpoint and Day-20 faithfulness eval.
"""

from dataclasses import dataclass, field

from typing import Generator, Protocol, Union

from ..models.results import CompletionResult, Usage
from ..processor.chunkers import estimate_tokens
from .prompting import (
    DEFAULT_SYSTEM_PROMPT,
    DONT_KNOW,
    build_citations,
    citation_ids,
    content_tokens,
    expand_referential_question,
    format_context,
    format_history,
    is_refusal,
    mark_citations,
    overlap_ratio,
    sentences,
)
from .schemas import ChatHistoryMessage, Citation, ContextItem, Evidence

Message = dict  # {role, content}, the LLMService wire shape

# A stream event is either ("token", text_chunk) or ("done", RagResult).
# Forward-referenced via string so the alias can live before RagResult.
StreamEvent = tuple[str, Union[str, "RagResult"]]


class ChatCompleter(Protocol):
    """The slice of LLMService/OpenAILLMProvider the openai RAG provider needs.
    Kept as a Protocol so app/rag never imports app.services (which would
    make a direct app.rag.* import trigger the services container)."""

    def complete(
        self, messages: list[Message], temperature: float
    ) -> CompletionResult: ...

    def complete_stream(
        self, messages: list[Message], temperature: float
    ) -> Generator[str, None, None]: ...


@dataclass
class RagResult:
    answer: str
    refused: bool
    provider: str
    model: str
    evidence: list[Evidence] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


class RagProvider:
    name: str = ""
    model: str = ""

    def generate(
        self,
        question: str,
        context: list[ContextItem],
        *,
        history: list[ChatHistoryMessage] | None = None,
        system_prompt: str,
        temperature: float,
    ) -> RagResult:
        raise NotImplementedError

    def generate_stream(
        self,
        question: str,
        context: list[ContextItem],
        *,
        history: list[ChatHistoryMessage] | None = None,
        system_prompt: str,
        temperature: float,
    ) -> Generator[StreamEvent, None, None]:
        """Default: non-streaming fallback — yields the full answer as one
        token event, then the done event. Subclasses override for real
        streaming."""
        result = self.generate(
            question, context, history=history,
            system_prompt=system_prompt, temperature=temperature,
        )
        yield ("token", result.answer)
        yield ("done", result)


def _token_usage(text: str) -> int:
    return estimate_tokens(text)


def _evidence_from(index: int, item: ContextItem) -> Evidence:
    return Evidence(
        index=index,
        chunk_id=item.chunk_id,
        document_id=item.document_id,
        document_title=item.document_title,
        section=item.section,
        page=item.page,
        similarity=item.similarity,
    )


class ExtractiveRagProvider(RagProvider):
    """Deterministic baseline: answer = the context sentence most relevant to
    the question (highest normalized content-token overlap; ties by evidence
    strength), quoted verbatim. Refuses when the context is empty or when NO
    sentence shares a single content token with the question — an ungrounded
    answer is impossible by construction."""

    name = "extract"

    def __init__(self, model: str):
        self.model = model

    def generate(
        self,
        question: str,
        context: list[ContextItem],
        *,
        history: list[ChatHistoryMessage] | None = None,
        system_prompt: str,
        temperature: float,
    ) -> RagResult:
        if not context or not any(content_tokens(item.text) for item in context):
            return self._refuse()
        # Day 20: a referential follow-up ("what about international
        # customers?") is resolved against the thread before matching, so the
        # extractive baseline understands the conversation too — not just the
        # openai path.
        query = expand_referential_question(history or [], question)
        best: tuple[float, str, ContextItem] | None = None
        for item in context:
            for candidate in sentences(item.text):
                ratio = overlap_ratio(query, candidate)
                if ratio > 0 and (best is None or ratio > best[0]):
                    best = (ratio, candidate, item)
        if best is None:
            return self._refuse()
        _, answer, item = best
        index = next(i for i, candidate in enumerate(context) if candidate is item)
        # Day 18: this answer is grounded on exactly one source chunk, the one
        # it quotes verbatim from. Cite it as [n] where n = its position in the
        # context (matching format_context's numbering), deterministically.
        source_id = index + 1
        return RagResult(
            answer=mark_citations(answer, [source_id]),
            refused=False,
            provider=self.name,
            model=self.model,
            evidence=[_evidence_from(index, item)],
            citations=build_citations(context, [source_id]),
            usage=Usage(
                prompt_tokens=_token_usage(
                    format_context(context) + query + system_prompt
                ),
                completion_tokens=_token_usage(answer),
                total_tokens=max(1, _token_usage(query) + _token_usage(answer)),
            ),
        )

    def _refuse(self) -> RagResult:
        return RagResult(
            answer=DONT_KNOW,
            refused=True,
            provider=self.name,
            model=self.model,
            evidence=[],
            citations=[],
        )


class OpenAiRagProvider(RagProvider):
    """The "real LLM" mode: packages question + formatted context into a
    system/user chat completion (the same OpenAI-compatible endpoint the rest
    of the service uses) with the default system prompt's honesty contract.
    `refused` honors the instructed refusal; an empty context forces refusal —
    an answer generated with NO evidence is never surfaced as grounded."""

    name = "openai"

    def __init__(self, llm: ChatCompleter, model: str):
        self.llm = llm
        self.model = model

    def generate(
        self,
        question: str,
        context: list[ContextItem],
        *,
        history: list[ChatHistoryMessage] | None = None,
        system_prompt: str,
        temperature: float,
    ) -> RagResult:
        user_prompt = (
            f"Context:\n{format_context(context)}\n\n"
            f"Question: {question}\n\n"
            f"Answer:"
        )
        if history:
            # Day 20 — the conversation so far, before the current question, so
            # the model can resolve pronouns and follow-up references.
            user_prompt = (
                f"Previous conversation:\n{format_history(history)}\n\n{user_prompt}"
            )
        result = self.llm.complete(
            [
                {"role": "system", "content": system_prompt or DEFAULT_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature,
        )
        refused = is_refusal(result.reply) or not context
        # Day 18 — cite sources. The context formatter numbered each source
        # [1]..[n]; honor any the model already references, and deterministically
        # fall back to the strongest (index 0) when the reply names none, so a
        # grounded answer is never left uncited.
        references = citation_ids(result.reply) or ([1] if not refused else [])
        answer = result.reply if not refused else DONT_KNOW
        if not refused:
            answer = mark_citations(answer, references)
        return RagResult(
            answer=answer,
            refused=refused,
            provider=self.name,
            model=result.model,
            evidence=[_evidence_from(i, item) for i, item in enumerate(context)],
            citations=build_citations(context, references),
            usage=result.usage,
        )

    def generate_stream(
        self,
        question: str,
        context: list[ContextItem],
        *,
        history: list[ChatHistoryMessage] | None = None,
        system_prompt: str,
        temperature: float,
    ) -> Generator[StreamEvent, None, None]:
        """Stream tokens from the LLM, then yield the final RagResult."""
        user_prompt = (
            f"Context:\n{format_context(context)}\n\n"
            f"Question: {question}\n\n"
            f"Answer:"
        )
        if history:
            user_prompt = (
                f"Previous conversation:\n{format_history(history)}\n\n{user_prompt}"
            )
        messages = [
            {"role": "system", "content": system_prompt or DEFAULT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        # Stream tokens from the LLM
        full_reply = ""
        for token in self.llm.complete_stream(messages, temperature):
            full_reply += token
            yield ("token", token)

        # Post-process: refusal check + citation enforcement
        refused = is_refusal(full_reply) or not context
        references = citation_ids(full_reply) or ([1] if not refused else [])
        answer = full_reply if not refused else DONT_KNOW
        if not refused:
            answer = mark_citations(answer, references)

        yield ("done", RagResult(
            answer=answer,
            refused=refused,
            provider=self.name,
            model=self.model,
            evidence=[_evidence_from(i, item) for i, item in enumerate(context)],
            citations=build_citations(context, references),
            usage=Usage(),
        ))