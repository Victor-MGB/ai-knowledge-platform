"""Day-21 RAG evaluation metrics — pure, dependency-free scoring functions.

These are the arithmetic behind the end-to-end evaluation in `rag_evaluation.py`.
They are deliberately free of I/O so the metric definitions are unit-testable in
isolation and the *meaning* of every number is pinned by tests, not by the run.

Metric vocabulary (the task's requested axes, defined concretely):
  * Retrieval accuracy  — did the expected evidence surface in the retrieved set
                          (document-level hit@k and page-level hit@k).
  * Recall@K           — fraction of ALL relevant chunks (of the expected
                          document/page) that the top-K context carries.
  * Precision@K        — fraction of the top-K that are relevant.
  * Answer correctness — does the answer state the expected evidence (normalized
                          token containment of the reference answer).
  * Citation correctness— does the answer's cited source resolve to the expected
                          document AND page.
  * Hallucination rate — fraction of non-refused answers whose content carries
                          NO retrieved evidence (claims made with nothing to
                          ground them).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RetrievedItem:
    """One retrieved chunk in rank order (index 0 is the strongest)."""
    rank: int  # 0-based position in the top-K ordering
    document: str
    page: int  # representative/starting page of the chunk
    pages: tuple[int, ...] = ()  # full page range the chunk spans, when known
    text: str = ""


@dataclass
class QueryResult:
    """Per-query retrieval + answer scoring. Metrics that cannot be computed for
    a given query (e.g. recall when there are no relevant chunks) are left None
    and excluded from the aggregate so an empty set can't corrupt a mean."""

    question: str
    truth_document: str
    truth_page: int

    # retrieval, measured at each K independently
    document_hit: dict[int, bool] = field(default_factory=dict)
    page_hit: dict[int, bool] = field(default_factory=dict)
    recall: dict[int, float | None] = field(default_factory=dict)
    precision: dict[int, float | None] = field(default_factory=dict)

    # answer quality
    answer_correct: bool | None = None
    citation_correct: bool | None = None
    citation_document_correct: bool | None = None
    citation_page_exact: bool | None = None
    citation_page_covers: bool | None = None
    hallucinated: bool | None = None
    refused: bool | None = None


def is_relevant(item: RetrievedItem, truth_document: str, truth_page: int) -> bool:
    """A chunk is relevant when it belongs to the expected document and covers
    the expected page (via its page span, falling back to its single page)."""
    if item.document != truth_document:
        return False
    if item.pages:
        return truth_page in item.pages
    return item.page == truth_page


def retrieval_metrics(
    items: list[RetrievedItem],
    truth_document: str,
    truth_page: int,
    all_relevant: int,
    ks: tuple[int, ...],
) -> dict[str, object]:
    """Compute document_hit/page_hit/recall/precision for every K over an ordered
    retrieved list. `all_relevant` is the total number of relevant chunks in the
    corpus (the recall denominator); it is caller-computed because it needs the
    whole corpus, not just the retrieved slice."""
    out: dict[str, object] = {}
    relevant_seen = 0
    for k in ks:
        topk = items[:k]
        rel_in_topk = sum(1 for it in topk if is_relevant(it, truth_document, truth_page))
        out.setdefault("document_hit", {})[k] = any(
            it.document == truth_document for it in topk
        )
        out.setdefault("page_hit", {})[k] = any(
            is_relevant(it, truth_document, truth_page) for it in topk
        )
        out.setdefault("precision", {})[k] = rel_in_topk / k if k else None
        out.setdefault("recall", {})[k] = (
            rel_in_topk / all_relevant if all_relevant else None
        )
        # track cumulative relevant for exact-recall consistency under any k
        if k == max(ks):
            relevant_seen = rel_in_topk
    return out


def answer_correct(expected: str, answer: str, threshold: float = 0.5) -> bool:
    """The answer is correct when it carries at least `threshold` of the expected
    evidence's content tokens. The reference is a single sentence; a faithful
    answer (verbatim quote, or an LLM paraphrase that names the same facts) must
    retain most of its meaning-bearing words."""
    expected_toks = _content_tokens(expected)
    if not expected_toks:
        return answer is not None and bool(answer.strip())
    answer_toks = _content_tokens(answer)
    if not answer_toks:
        return False
    overlap = len(expected_toks & answer_toks)
    return (overlap / len(expected_toks)) >= threshold


def citation_correct(citation_doc: str, truth_document: str, citation_page: int, truth_page: int) -> bool:
    """A citation is correct when it points at the expected document AND page.
    When the citation carries no location (page unknown) we fall back to
    document-only, but the ideal is a fully-resolved page reference."""
    if citation_doc != truth_document:
        return False
    if citation_page is not None and truth_page is not None:
        return citation_page == truth_page
    return True


def hallucinated(answer: str, context_texts: list[str]) -> bool:
    """True when a non-refused answer makes a claim with NO retrieved evidence:
    none of the context chunks it was supposed to ground on share a content
    token with the answer. A refusal is not a hallucination."""
    answer_toks = _content_tokens(answer)
    if not answer_toks:
        return True  # an empty non-refused answer is itself a failure
    for context in context_texts:
        if answer_toks & _content_tokens(context):
            return False
    return True


def _strip_citations(text: str) -> str:
    """Drop inline [n] citation markers before content comparison — they carry no
    factual meaning and would otherwise pollute token overlap."""
    import re

    return re.sub(r"\[\d+\]", "", text)


def _content_tokens(text: str) -> set[str]:
    import re

    stop = frozenset(
        """
        a an and are as at be been but by can could did do does for from had has
        have he her hers him his how i if in into is it its me my no not of on or
        our ours out over she so than that the their theirs them then there these
        they this those to too up us was we were what when where which who why
        will with would you your yours
        """.split()
    )
    toks = re.findall(r"[0-9a-z][0-9a-z'-]*", _strip_citations(text).lower())
    return {t for t in toks if len(t) >= 3 and t not in stop}


def aggregate(results: list[QueryResult], ks: tuple[int, ...]) -> dict[str, object]:
    """Roll per-query results into the report: a mean per K for each retrieval
    metric and a rate for each binary answer metric (None-excluded so a query
    where a metric is undefined does not drag the mean)."""
    n = len(results)
    report: dict[str, object] = {"queries": n}

    def rate(key: str) -> float:
        vals = [getattr(r, key) for r in results]
        present = [v for v in vals if v is not None]
        return (sum(1 for v in present if v) / len(present)) if present else 0.0

    for key in ("document_hit", "page_hit", "recall", "precision"):
        report[key] = {}
        for k in ks:
            items = [getattr(r, key).get(k) for r in results if k in getattr(r, key)]
            items = [v for v in items if v is not None]
            report[key][k] = (sum(items) / len(items)) if items else 0.0

    for key in ("answer_correct", "citation_correct", "hallucinated"):
        report[key] = rate(key)

    for key in ("citation_document_correct", "citation_page_exact", "citation_page_covers"):
        report[key] = rate(key)

    report["refused_rate"] = rate("refused")
    return report
