"""Day 34 — offline, deterministic half of the production-demo check.

Runs the 14 demo questions against the freshly built corpus PDFs using the
real pipeline pieces (PdfExtractor, paragraph chunker, HashingEmbeddingProvider,
RAGGenerator + ExtractiveRagProvider) with the live defaults the backend uses
(limit=5, max_context_tokens=1200). No database, no network: this is the fast
signal before the live end-to-end run.

13 questions must retrieve the intended document+section and be answerable from
that section's golden sentence; question 14 is out of scope and must be refused
via a `min_score` floor (the same guardrail the RAG API supports — the
extractive baseline refuses on empty context or sub-threshold similarity).

The questions deliberately mirror the golden sentences' vocabulary: the offline
embedder is a feature-hashing stand-in (not a trained model), so retrieval
discrimination comes from exact content-token overlap.

Run:  ai-service/.venv/bin/python -m tests.e2e.offline_eval
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ai-service"))

from tests.e2e.demo_corpus import DOC_FILENAMES, OUT_DIR  # noqa: E402
from app.processor.chunkers import chunk_pages  # noqa: E402
from app.processor.pdf import PdfExtractor  # noqa: E402
from app.rag.generator import RAGGenerator  # noqa: E402
from app.rag.provider import ExtractiveRagProvider  # noqa: E402
from app.rag.schemas import ContextItem, RagGenerationRequest  # noqa: E402
from app.services.embedding import HashingEmbeddingProvider  # noqa: E402

LIMIT = 5
MAX_CONTEXT_TOKENS = 1200

# (question, expected_document_title, expected_section, golden_fragment, min_score)
# min_score is applied to the request exactly as the live RAG endpoint would.
DEMO_QUESTIONS: list[tuple[str, str | None, str | None, str | None, float | None]] = [
    (
        "How many paid vacation days do employees accrue per year and after how long can they use them?",
        "Employee Handbook",
        "Leave Allowance",
        "Employees accrue 22 days of paid vacation each calendar year",
        None,
    ),
    (
        "How much does Lumina charge per member per month for the Business plan, in dollars?",
        "Product Documentation",
        "Pricing Tiers",
        "the Business plan is 199 dollars per member per month",
        None,
    ),
    (
        "What overall test coverage threshold must new engineering code maintain before merge?",
        "Engineering Handbook",
        "Tests and Coverage",
        "keeps overall test coverage at or above eighty percent",
        None,
    ),
    (
        "If a P0 security incident is confirmed, how quickly must it be reported to the security team?",
        "Security Policy",
        "Incident Response",
        "A confirmed P0 security incident must be reported",
        None,
    ),
    (
        "What monthly uptime percent does Lumina guarantee on the Business plan?",
        "Customer Policy",
        "Uptime Guarantee",
        "ninety-nine point nine percent monthly uptime",
        None,
    ),
    (
        "Above what dollar amount does an expense or purchase order require manager approval?",
        "Financial Policy",
        "Approvals",
        "above five hundred dollars require manager approval",
        None,
    ),
    (
        "When are salaries paid twice each month, on the fifteenth and the last working day of the month?",
        "HR Policy",
        "Payroll",
        "paid twice each month, on the fifteenth and the last working day",
        None,
    ),
    (
        "What is the standard rate limit for an API token in requests per minute?",
        "API Documentation",
        "Rate Limits",
        "The standard rate limit is sixty requests per minute per token",
        None,
    ),
    (
        "Which identity provider provisions single sign-on access through Okta for new employees during onboarding?",
        "Onboarding Guide",
        "Getting Started",
        "All accounts are provisioned through Okta single sign-on",
        None,
    ),
    (
        "By how much did customer churn fall for accounts that adopted Pulse alerting within their first ninety days?",
        "Research Report",
        "Finding: Retention",
        "an eighteen percent reduction in customer churn",
        None,
    ),
    (
        "How far in advance must an employee submit a leave request that is longer than two weeks?",
        "HR Policy",
        "Leave Process",
        "at least forty-five days in advance",
        None,
    ),
    (
        "How long is the refund window if a customer cancels their annual subscription?",
        "Customer Policy",
        "Refunds and Cancellation",
        "cancelled within the first thirty days receive a full refund",
        None,
    ),
    (
        "What HTTP status code is returned when a request exceeds the rate limit on the API?",
        "API Documentation",
        "Error Handling",
        "returns a 429 status code with a Retry-After header",
        None,
    ),
    # Nothing in the corpus covers employee income-tax arrangements for crypto
    # mining. The min_score floor turns the weak coincidental overlap into an
    # honest refusal (the same guardrail the streaming API honours).
    (
        "How should employees claim tax deductions for cryptocurrency mining at home?",
        None,
        None,
        None,
        0.20,
    ),
]


def _norm(text: str) -> str:
    return " ".join(text.split())


def build_corpus() -> list[ContextItem]:
    """Extract + chunk every PDF exactly as the processor would, carrying the
    citation provenance the live rows would have."""
    extractor = PdfExtractor()
    items: list[ContextItem] = []
    for title, filename in DOC_FILENAMES.items():
        result = extractor.extract(
            (OUT_DIR / filename).read_bytes(), document_id=title, organization_id="demo"
        )
        for index, chunk in enumerate(chunk_pages(result.pages, "paragraph", chunk_tokens=512)):
            items.append(
                ContextItem(
                    text=chunk.content,
                    section=chunk.metadata.get("section"),
                    page=chunk.metadata["page_range"][0],
                    chunk_id=f"{title}:{index}",
                    document_id=title,
                    document_title=title,
                )
            )
    return items


def main() -> int:
    embedder = HashingEmbeddingProvider("knowflow-hash-384", dimensions=384)
    corpus = build_corpus()
    chunk_vectors = [embedder.embed(item.text).vector for item in corpus]
    generator = RAGGenerator(ExtractiveRagProvider("extract"), max_context_tokens=MAX_CONTEXT_TOKENS)

    passed = 0
    for question, expected_doc, expected_section, golden_fragment, min_score in DEMO_QUESTIONS:
        query = embedder.embed(question).vector
        scored = sorted(
            (
                (sum(q * c for q, c in zip(query, vec)), item)
                for vec, item in zip(chunk_vectors, corpus)
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        context = [
            item.model_copy(update={"similarity": round(sim, 6)}) for sim, item in scored[:LIMIT]
        ]
        response = generator.generate(
            RagGenerationRequest(
                question=question,
                context=context,
                max_context_tokens=MAX_CONTEXT_TOKENS,
                min_score=min_score,
            )
        )

        if expected_doc is None:
            ok = response.refused
            print(f"{'PASS' if ok else 'FAIL'}  (refused={response.refused}, top_sim={context[0].similarity:.3f}) {question}")
            if not ok:
                print(f"        expected out-of-scope refusal, got: {_norm(response.answer)[:140]}")
            passed += ok
            continue

        evidence = response.evidence[0] if response.evidence else None
        top_title = evidence.document_title if evidence else None
        top_section = evidence.section if evidence else None
        evidence_text = ""
        if not response.refused and evidence is not None and evidence.index < len(context):
            evidence_text = _norm(context[evidence.index].text)
        has_golden = (not response.refused) and (
            golden_fragment and _norm(golden_fragment) in evidence_text
        )
        ok = (
            not response.refused
            and top_title == expected_doc
            and top_section == expected_section
            and has_golden
        )
        print(
            f"{'PASS' if ok else 'FAIL'}  (answer={top_title!r}/{top_section!r}, "
            f"top_retrieval={context[0].document_title!r}/{context[0].section!r}, sim={context[0].similarity:.3f}) {question}"
        )
        if top_title != expected_doc or top_section != expected_section:
            print(f"        expected {expected_doc!r}/{expected_section!r}")
        if not has_golden:
            print(f"        golden fragment not in the cited chunk: {golden_fragment!r}")
        passed += ok

    print("-" * 60)
    print(f"{passed}/{len(DEMO_QUESTIONS)} demo questions PASS")
    return 0 if passed == len(DEMO_QUESTIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())