"""Day 17 — RAG helper + provider unit tests (imports, no HTTP).

Covers the pieces the API tests exercise indirectly: sentence splitting,
content-token math, context formatting, the budget function, and the two
providers' refusal behavior at the unit boundary.
"""

from app.models.results import CompletionResult, Usage
from app.rag.generator import RAGGenerator, _budget_context
from app.rag.prompting import (
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
    referential,
    sentences,
)
from app.rag.provider import ExtractiveRagProvider, OpenAiRagProvider
from app.rag.schemas import ChatHistoryMessage, ContextItem, RagGenerationRequest


def test_content_tokens_are_lowercased_stopword_free_and_length_limited():
    assert content_tokens("The Return Window seen so") == {"return", "window", "seen"}
    assert "so" not in content_tokens("so what")  # len < 3 dropped


def test_sentences_split_on_periods_but_not_decimal_points():
    assert sentences("The window is 30 days. Restocking applies!") == [
        "The window is 30 days.",
        "Restocking applies!",
    ]
    assert sentences("Only one sentence") == ["Only one sentence"]


def test_overlap_ratio_is_normalized_by_question_size():
    question = "what is the return window"  # content tokens: {return, window}
    assert overlap_ratio(question, "The return window is thirty days.") == 1.0
    assert overlap_ratio(question, "Return orders ship free.") == 0.5
    assert overlap_ratio(question, "Cheese caves stay cool.") == 0.0


def test_format_context_labels_index_location_section():
    items = [
        ContextItem(text="a sentence.", page=3, section="Return"),
        ContextItem(text="another sentence."),
    ]
    formatted = format_context(items)
    assert "[1] (page 3, section \"Return\") a sentence." in formatted
    assert "[2] another sentence." in formatted


def test_is_refusal_matches_the_canonical_and_common_variants():
    assert is_refusal(DONT_KNOW)
    assert is_refusal("I don't know anything about that.")
    assert is_refusal(" I DON'T KNOW. ")
    assert not is_refusal("The return window is thirty days.")


def test_budget_sorts_best_first_and_drops_what_does_not_fit():
    items = [
        ContextItem(text="short a b", similarity=0.3),
        ContextItem(text="medium sentence here", similarity=0.9),
        ContextItem(text="tiny c d", similarity=0.5),
    ]
    budgeted = _budget_context(items, budget=8)
    # best first, then the next that still fits; the 0.3 one is dropped
    assert [item.similarity for item in budgeted] == [0.9, 0.5]


def test_budget_never_drops_the_strongest_item():
    items = [ContextItem(text="q" * 400, similarity=0.8)]  # ~100 tokens
    assert _budget_context(items, budget=50) == items


def test_extractive_provider_refuses_cleanly():
    provider = ExtractiveRagProvider("knowflow-extract-1")
    result = provider.generate("question x", [], system_prompt="", temperature=0.0)
    assert result.refused is True
    assert result.answer == DONT_KNOW
    assert result.evidence == []


def test_openai_provider_honors_an_instructed_refusal():
    class FakeLLM:
        def complete(self, messages, temperature):
            return CompletionResult(
                reply="I don't know.",
                model="gpt-fake",
                provider="openai",
                usage=Usage(prompt_tokens=10, completion_tokens=3, total_tokens=13),
            )

    provider = OpenAiRagProvider(FakeLLM(), "gpt-fake")
    result = provider.generate(
        "q", [ContextItem(text="unrelated trivia.", similarity=0.4)],
        system_prompt="", temperature=0.0,
    )
    assert result.refused is True
    assert result.answer == DONT_KNOW
    assert len(result.evidence) == 1


def test_openai_provider_answers_with_evidence_and_never_surfaces_empty_context():
    class FakeLLM:
        def complete(self, messages, temperature):
            return CompletionResult(
                reply="thirty days",
                model="gpt-fake",
                provider="openai",
                usage=Usage(prompt_tokens=5, completion_tokens=2, total_tokens=7),
            )

    provider = OpenAiRagProvider(FakeLLM(), "gpt-fake")
    answered = provider.generate(
        "q", [ContextItem(text="the return window is thirty days.", similarity=0.9)],
        system_prompt="", temperature=0.0,
    )
    assert answered.refused is False
    assert answered.answer == "thirty days [1]"
    assert len(answered.citations) == 1
    assert answered.citations[0].id == 1
    assert answered.citations[0].similarity == 0.9

    # empty context: an LLM reply is never surfaced as a grounded answer
    forced = provider.generate("q", [], system_prompt="", temperature=0.0)
    assert forced.refused is True
    assert forced.answer == DONT_KNOW
    assert forced.citations == []


def test_generator_facade_round_trips_a_request():
    generator = RAGGenerator(ExtractiveRagProvider("knowflow-extract-1"))
    request = RagGenerationRequest(
        question="what is the return window",
        context=[ContextItem(text="The return window is thirty days.", similarity=0.9)],
    )
    response = generator.generate(request)
    assert response.refused is False
    assert response.answer == "The return window is thirty days. [1]"
    assert response.provider == "extract"


def test_citation_ids_extract_distinct_ascending_numbers():
    assert citation_ids("20 days [2] and paid [1] and [2] again") == [2, 1]
    assert citation_ids("no markers here") == []
    assert citation_ids("zero [0] ignored") == []


def test_mark_citations_appends_markers_to_the_claim():
    assert mark_citations("annual leave is 20 working days.", [1]) == (
        "annual leave is 20 working days. [1]"
    )
    assert mark_citations("annual leave is 20 working days.", []) == (
        "annual leave is 20 working days."
    )
    assert mark_citations("a claim.", [1, 3]) == "a claim. [1][3]"


def test_build_citations_maps_markers_back_to_source_metadata():
    items = [
        ContextItem(
            text="Annual leave is 20 working days.",
            similarity=0.9,
            document_id="doc-1",
            chunk_id="chunk-1",
            document_title="Employee Handbook",
            section="Leave",
            page=14,
        ),
        ContextItem(
            text="Sick leave is 10 days.",
            similarity=0.5,
            document_id="doc-2",
            chunk_id="chunk-2",
            document_title="Employee Handbook",
            page=20,
        ),
    ]
    citations = build_citations(items, [2, 1])
    assert [c.id for c in citations] == [2, 1]
    assert citations[0].title == "Employee Handbook"
    assert citations[0].page == 20
    assert citations[0].document_id == "doc-2"
    # an out-of-range marker is dropped, never corrupting the list
    assert build_citations(items, [1, 99])[0].id == 1
    assert len(build_citations(items, [99])) == 0


def test_extractive_provider_cites_the_quoted_source():
    provider = ExtractiveRagProvider("knowflow-extract-1")
    result = provider.generate(
        "how much annual leave",
        [
            ContextItem(
                text="Unrelated filler here.",
                similarity=0.2,
                document_title="Payroll Handbook",
                page=3,
            ),
            ContextItem(
                text="Annual leave is 20 working days.",
                similarity=0.9,
                document_title="Employee Handbook",
                page=14,
            ),
        ],
        system_prompt="",
        temperature=0.0,
    )
    assert result.refused is False
    assert result.answer == "Annual leave is 20 working days. [2]"
    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation.id == 2
    assert citation.title == "Employee Handbook"
    assert citation.page == 14


# ---- Day 20 — conversation memory ----


def test_referential_detects_pronouns_and_anaphoric_connectors():
    assert referential("What about international customers?")
    assert referential("How about the free tier?")
    assert referential("Is that refundable?")
    assert referential("And the return window?")
    assert not referential("What is the refund policy?")
    assert not referential("How many days is the return window on shoes?")


def test_expand_referential_question_splices_the_thread_anchor():
    history = [
        ChatHistoryMessage(role="user", content="What is the refund policy?"),
        ChatHistoryMessage(
            role="assistant", content="The refund period is 30 days. [1]"
        ),
    ]
    expanded = expand_referential_question(
        history, "What about international customers?"
    )
    assert "What is the refund policy?" in expanded
    # the trailing citation marker carries no retrieval meaning and is clipped
    assert "[1]" not in expanded
    assert "The refund period is 30 days." in expanded
    assert "What about international customers?" in expanded


def test_expand_referential_question_passes_standalone_questions_through():
    history = [
        ChatHistoryMessage(role="user", content="What is the refund policy?"),
        ChatHistoryMessage(role="assistant", content="30 days [1]"),
    ]
    standalone = "How do I file a warranty claim?"
    assert expand_referential_question(history, standalone) == standalone


def test_expand_referential_question_without_history_is_a_noop():
    assert (
        expand_referential_question([], "What about international customers?")
        == "What about international customers?"
    )


def test_format_history_labels_roles():
    history = [
        ChatHistoryMessage(role="user", content="What is the refund policy?"),
        ChatHistoryMessage(role="assistant", content="The refund period is 30 days."),
    ]
    formatted = format_history(history)
    assert formatted == (
        "User: What is the refund policy?\n"
        "Assistant: The refund period is 30 days."
    )


def test_extractive_provider_resolves_a_referential_follow_up():
    """The Day-20 story in one unit: after 'refund policy' -> '30 days', asking
    'what about international customers?' must ground on the refund snippet —
    the raw question alone shares no content tokens with that evidence."""
    provider = ExtractiveRagProvider("knowflow-extract-1")
    history = [
        ChatHistoryMessage(role="user", content="What is the refund policy?"),
        ChatHistoryMessage(
            role="assistant", content="The refund period is 30 days. [1]"
        ),
    ]
    result = provider.generate(
        "What about international customers?",
        [
            ContextItem(
                text="The refund period is 30 days from the delivery date.",
                similarity=0.9,
                document_title="Returns Handbook",
                page=4,
            ),
            ContextItem(
                text="International orders ship within 10 business days.",
                similarity=0.7,
                document_title="Shipping Policy",
                page=2,
            ),
        ],
        history=history,
        system_prompt="",
        temperature=0.0,
    )
    assert result.refused is False
    # it grounds on the refund evidence (the referent of the follow-up), not the
    # literal shipping sentence that happens to mention 'international'
    assert "30 days" in result.answer
    assert len(result.citations) == 1
    assert result.citations[0].title == "Returns Handbook"


def test_extractive_provider_without_history_still_refuses_an_ambiguous_follow_up():
    provider = ExtractiveRagProvider("knowflow-extract-1")
    result = provider.generate(
        "What about international customers?",
        [ContextItem(text="The refund period is 30 days.", similarity=0.9)],
        system_prompt="",
        temperature=0.0,
    )
    # no thread to resolve the pronoun against -> honest refusal, never a guess
    assert result.refused is True
    assert result.answer == DONT_KNOW


def test_generator_facade_ships_history_to_the_provider():
    class RecordingProvider(ExtractiveRagProvider):
        def __init__(self):
            super().__init__("knowflow-extract-1")
            self.seen_history = None

        def generate(self, *args, history=None, **kwargs):
            self.seen_history = history
            return super().generate(*args, history=history, **kwargs)

    provider = RecordingProvider()
    generator = RAGGenerator(provider)
    history = [
        ChatHistoryMessage(role="user", content="What is the refund policy?"),
        ChatHistoryMessage(role="assistant", content="30 days [1]"),
    ]
    generator.generate(
        RagGenerationRequest(
            question="What about international customers?",
            context=[
                ContextItem(text="International orders ship in 10 days.", similarity=0.9)
            ],
            history=history,
        )
    )
    assert provider.seen_history == history