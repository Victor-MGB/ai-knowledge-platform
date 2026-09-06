"""RAG prompting primitives — leaf module (no app deps beyond schemas).

The pieces the generator and providers share: the canonical "I don't know"
answer, the default system prompt, the numbered context formatter fed to LLM
prompts, and the deterministic extractive helpers (content tokens, sentence
splitting, normalized question-overlap) that the default provider uses to
answer verbatim and to decide when the evidence simply does not address the
question.
"""

import re

from .schemas import ChatHistoryMessage, Citation, ContextItem

DONT_KNOW = "I don't know."

DEFAULT_SYSTEM_PROMPT = (
    "You are KnowFlow, a retrieval-augmented assistant. Answer the question "
    "using ONLY the context passages provided below. Every factual claim in "
    'your answer must be supported by that context. If the context does not '
    'contain enough information to answer the question, reply exactly: "I '
    "don't know.\" Do not use outside knowledge and do not invent sources."
    " The context passages are numbered [1], [2], ... — after the claim that "
    "relies on a passage, cite its number inline as [n] (for example: "
    "\"annual leave is 20 working days [1]\")."
)

# verbatim sentence text is the extractive baseline's answer unit
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_TOKEN = re.compile(r"[0-9a-z][0-9a-z'-]*")

# deliberately small; the goal is to keep stopwords from faking a match, not
# to be a full NLP tokenizer
_STOPWORDS = frozenset(
    """
    a an and are as at be been but by can could did do does for from had has
    have he her here hers him his how i if in into is it its me my no not of
    on or our ours out over she so than that the their theirs them then there
    these they this those to too up us was we were what when where which who
    why will with would you your yours yes
    """.split()
)


def content_tokens(text: str) -> set[str]:
    """Lowercased alphanumeric word-stems (len >= 3) minus stopwords."""
    return {
        token
        for token in _TOKEN.findall(text.lower())
        if len(token) >= 3 and token not in _STOPWORDS
    }


def sentences(text: str) -> list[str]:
    """Baseline sentence splitter (period/exclamation/question + whitespace).

    Bounded on purpose: this feeds the extractive baseline, and the only harm
    of a wrong split is a longer or shorter quoted sentence, never a fabrica-
    tion. Multi-sentence context yields the single most relevant sentence.
    """
    parts = [part.strip() for part in _SENTENCE_BOUNDARY.split(text.replace("\n", " "))]
    return [part for part in parts if part]


def overlap_ratio(question: str, candidate: str) -> float:
    """Fraction of the question's content tokens present in a candidate."""
    question_tokens = content_tokens(question)
    if not question_tokens:
        return 0.0
    candidate_tokens = content_tokens(candidate)
    if not candidate_tokens:
        return 0.0
    return len(question_tokens & candidate_tokens) / len(question_tokens)


def format_context(items: list[ContextItem]) -> str:
    """Human-labeled context block for LLM prompts ([n] (page, section) text)."""
    blocks = []
    for index, item in enumerate(items, start=1):
        head = f"[{index}]"
        location = []
        if item.page is not None:
            location.append(f"page {item.page}")
        if item.section:
            location.append(f'section "{item.section}"')
        if location:
            head += " (" + ", ".join(location) + ")"
        blocks.append(f"{head} {item.text}")
    return "\n\n".join(blocks)


# Day 20 — conversation memory. Words that mark a turn as *referential*: it is
# not self-contained, it points back at the earlier exchange ("what about
# international customers?", "and the free tier?", "is that refundable?"), so
# it must be resolved against the prior thread before it can retrieve or match
# any evidence.
_REFERENTIAL_RE = re.compile(
    r"\b(what about|how about|whats with|what's with|and then|and the|and why|"
    r"what is that|is that|are those|these|those|them|they|it this|it that|"
    r"same thing|same rule|same policy|and|about that|on that)\b",
    re.IGNORECASE,
)
_PRONOUN_RE = re.compile(r"\bit\b|\bthis\b|\bthat\b|\bthose\b|\bthem\b|\bthey\b|\bthese\b", re.IGNORECASE)


def referential(question: str) -> bool:
    """True when a question points back at the conversation instead of standing
    alone: it carries the referring pronouns or anaphoric connector, or is so
    short it cannot be self-sufficient."""
    return bool(_REFERENTIAL_RE.search(question)) or bool(_PRONOUN_RE.search(question))


def format_history(history: list[ChatHistoryMessage]) -> str:
    """Render the prior conversation for an LLM as a labeled transcript. The
    roles are relabeled to User:/Assistant: so the tokens are clear regardless
    of the chat-format the provider wraps them in."""
    lines = []
    for message in history:
        label = "User" if message.role == "user" else "Assistant"
        lines.append(f"{label}: {message.content}")
    return "\n".join(lines)


def expand_referential_question(
    history: list[ChatHistoryMessage], question: str
) -> str:
    """Resolve a referential follow-up against the conversation thread.

    Returns an expanded, self-contained question that drives the same retrieval
    AND exact-overlap matching the original would, but with the missing
    referent supplied from the prior turns. Non-referential questions pass
    through unchanged.

    Strategy, deterministic and cheap: drop any trailing pronouns/connectors is
    NOT needed — instead take the most recent prior *user* question as the
    thread anchor, append the tail of the most recent *assistant* answer as the
    factual referent, then splice the current question in. The last assistant
    turn usually carries the entity the follow-up is really about ("the refund
    period is 30 days" -> "international customers" ~ the refund policy).
    """
    if not referential(question) or not history:
        return question

    last_user: str | None = None
    last_assistant: str | None = None
    for message in history:
        if message.role == "user":
            last_user = message.content
        else:
            last_assistant = message.content

    thread: list[str] = []
    if last_user:
        thread.append(last_user)
    if last_assistant:
        # keep the factual heart of the reply, not a citation marker tail
        clipped = _clip_sentence_tail(last_assistant)
        if clipped:
            thread.append(clipped)

    prefix = " — ".join(thread)
    if not prefix:
        return question
    return f"{prefix} — {question}"


def _clip_sentence_tail(text: str, max_chars: int = 400) -> str:
    """Keep the first sentence(s) of an assistant reply, dropping the trailing
    [n] citation markers that carry no retrieval meaning (and would otherwise
    pollute the rewrite)."""
    cleaned = re.sub(r"\s*\[\d+\]\s*$", "", text.strip())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rstrip(" ,;")




def is_refusal(text: str) -> bool:
    """True when a provider's reply is the instructed "I don't know" refusal."""
    normalized = " ".join(text.strip().lower().split())
    return (
        normalized == DONT_KNOW.lower()
        or normalized.startswith("i don't know")
        or normalized.startswith("i do not know")
    )


# Day 18 — citation markers. The number inside [n] is the source number the
# context formatter already assigns ([1]..[n] per item), so the same number
# maps an inline answer marker to its source chunk and page reference.
_CITATION_RE = re.compile(r"\[(\d+)\]")


def citation_ids(text: str) -> list[int]:
    """Distinct, ascending [n] source references found in an answer, in the
    order they first appear."""
    seen: list[int] = []
    for raw in _CITATION_RE.findall(text):
        number = int(raw)
        if number >= 1 and number not in seen:
            seen.append(number)
    return seen


def mark_citations(answer: str, source_ids: list[int]) -> str:
    """Return an answer carrying inline `[n]` markers for each source it is
    grounded on. Mirrors the portfolio shape: the verbatim claim followed by
    its source references (e.g. '... 20 working days. [1]')."""
    if not source_ids:
        return answer
    marker = "".join(f"[{number}]" for number in source_ids)
    return f"{answer} {marker}"


def build_citations(items: list[ContextItem], source_ids: list[int]) -> list[Citation]:
    """Map inline `[n]` markers back to their source chunks.

    `items` are the context items in the exact order the formatter numbered
    them, so `[n]` refers to items[n-1]. A source reference with no matching
    item is silently skipped — a model quoting an off-by-one number must not
    corrupt the citation list.
    """
    citations: list[Citation] = []
    for number in source_ids:
        item = items[number - 1] if 0 < number <= len(items) else None
        if item is None:
            continue
        citations.append(
            Citation(
                id=number,
                title=item.document_title,
                section=item.section,
                page=item.page,
                chunk_id=item.chunk_id,
                document_id=item.document_id,
                similarity=item.similarity,
            )
        )
    return citations