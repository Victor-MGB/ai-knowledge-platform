"""Day 21 — unit tests for the RAG evaluation metric functions (no I/O).

Pins the arithmetic behind the end-to-end evaluation (rag_evaluation.py) so the
published numbers cannot silently drift: what counts as a retrieval hit, how
Recall@K / Precision@K are computed, what makes an answer correct, what makes a
citation correct, what counts as a hallucination, and how per-query results roll
into the aggregate report. The evaluator drives the real pipeline; these tests
guard the meaning of every number it prints.
"""

from evaluation.rag_metrics import (
    QueryResult,
    RetrievedItem,
    aggregate,
    answer_correct,
    citation_correct,
    hallucinated,
    is_relevant,
    retrieval_metrics,
)

KS = (1, 3, 5)


def _item(document: str, page: int, *pages: int) -> RetrievedItem:
    return RetrievedItem(
        rank=0,
        document=document,
        page=page,
        pages=tuple(pages) or (),
        text="",
    )


def test_is_relevant_matches_document_and_page_span():
    assert is_relevant(_item("a", 2, 2, 3, 4), "a", 3)
    assert not is_relevant(_item("a", 2, 2, 3, 4), "a", 5)  # outside span
    assert not is_relevant(_item("a", 2, 2, 3, 4), "b", 3)  # wrong doc
    # no span: falls back to the single page
    assert is_relevant(_item("a", 2), "a", 2)
    assert not is_relevant(_item("a", 2), "a", 9)


def test_retrieval_metrics_hit_recall_precision():
    items = [
        _item("a", 1, 1, 2),
        _item("b", 7),
        _item("a", 2, 2, 3),
        _item("a", 9),
        _item("c", 4),
    ]
    # relevant: doc a covering page 2 -> items[0] (pages 1-2) and items[2] (2-3)
    m = retrieval_metrics(items, "a", 2, all_relevant=3, ks=KS)
    assert m["document_hit"] == {1: True, 3: True, 5: True}
    assert m["page_hit"] == {1: True, 3: True, 5: True}
    assert m["precision"] == {1: 1.0, 3: 2 / 3, 5: 2 / 5}
    assert m["recall"] == {1: 1 / 3, 3: 2 / 3, 5: 2 / 3}


def test_retrieval_metrics_handles_zero_relevant():
    m = retrieval_metrics([_item("x", 1)], "y", 1, all_relevant=0, ks=(5,))
    assert m["recall"][5] is None  # undefined, excluded from aggregate
    assert m["precision"][5] == 0.0


def test_answer_correct_threshold_on_content_tokens():
    ref = "Refunds are issued within five business days once the package reaches the warehouse."
    assert answer_correct(ref, ref)  # verbatim
    # paraphrase keeps the meaning-bearing words
    assert answer_correct(ref, "refunds issued five business days package reaches warehouse")
    # unrelated answer shares almost nothing -> incorrect
    assert not answer_correct(ref, "Employees accrue twenty days of paid time off per year")


def test_citation_correct_requires_document_and_page():
    assert citation_correct("a", "a", 2, 2)
    assert not citation_correct("a", "a", 3, 2)  # wrong page
    assert not citation_correct("a", "b", 2, 2)  # wrong doc


def test_hallucinated_detects_ungrounded_claims():
    context = ["Refunds are issued within five business days."]
    assert not hallucinated("Refunds are issued within five business days.", context)
    # claims content none of the context shares -> hallucination
    assert hallucinated("Employees accrue twenty holidays each year.", context)
    # an empty non-refused answer counts as a hallucination (a failure)
    assert hallucinated("", context)


def test_hallucinated_ignores_citation_markers():
    context = ["Refunds are issued within five business days."]
    assert not hallucinated("Refunds are issued within five business days. [1]", context)


def test_aggregate_rolls_up_rates_and_means_per_k():
    results = []
    r1 = QueryResult("q1", "a", 1)
    r1.__dict__.update(
        retrieval_metrics(
            [_item("a", 1, 1), _item("x", 5), _item("a", 1, 1)],
            "a",
            1,
            all_relevant=2,
            ks=KS,
        )
    )
    r1.answer_correct = True
    r1.citation_correct = True
    r1.hallucinated = False
    r1.refused = False
    r1.citation_document_correct = True
    r1.citation_page_covers = True
    r1.citation_page_exact = False

    r2 = QueryResult("q2", "b", 2)
    r2.__dict__.update(
        retrieval_metrics(
            [_item("b", 2, 2), _item("b", 9), _item("b", 2, 2, 3)],
            "b",
            2,
            all_relevant=2,
            ks=KS,
        )
    )
    r2.answer_correct = False
    r2.citation_correct = False
    r2.citation_document_correct = False
    r2.citation_page_covers = False
    r2.citation_page_exact = False
    r2.hallucinated = True
    r2.refused = False
    results.extend([r1, r2])

    rep = aggregate(results, KS)
    assert rep["queries"] == 2
    assert rep["document_hit"][1] == 1.0
    assert rep["page_hit"][5] == 1.0
    assert rep["answer_correct"] == 0.5
    assert rep["citation_correct"] == 0.5
    assert rep["hallucinated"] == 0.5
    assert rep["citation_document_correct"] == 0.5
    assert rep["citation_page_covers"] == 0.5
    assert rep["citation_page_exact"] == 0.0
    assert rep["refused_rate"] == 0.0


def test_aggregate_excludes_none_metrics():
    r = QueryResult("q", "a", 1)
    r.answer_correct = None  # undefined for this query
    rep = aggregate([r], KS)
    assert rep["answer_correct"] == 0.0  # no present values -> 0.0, not NaN
