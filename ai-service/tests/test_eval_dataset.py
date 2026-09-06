"""Day 29 — evaluation-dataset integrity.

The golden dataset (evaluation/rag_eval_dataset.py) is the contract the Day-21
RAG evaluation measures against. This test keeps that contract honest inside
the suite: every golden resolves against the *actual* extractor output on the
real fixture corpus, so a stale label or a drifted fixture breaks here (fast)
before the eval script discovers it downstream.

The full end-to-end scoring run stays a script (evaluation/rag_evaluation.py,
self-contained, prints the retrieval/answer/citation tables); this test only
pins the dataset layer that script consumes.
"""

import re
from pathlib import Path

from app.processor.pdf import PdfExtractor

from evaluation.rag_eval_dataset import goldens

FIXTURES = Path(__file__).resolve().parent / "fixtures"

CORPUS = [
    ("policies", FIXTURES / "eval" / "policies.pdf"),
    ("arch", FIXTURES / "eval" / "architecture_notes.pdf"),
    ("team", FIXTURES / "eval" / "hr_handbook.pdf"),
    ("service", FIXTURES / "eval" / "service_guide.pdf"),
    ("chapter", FIXTURES / "chapter.pdf"),
]

_KNOWN_DOCS = {doc_id for doc_id, _ in CORPUS}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _extract_corpus() -> dict[str, list]:
    extractor = PdfExtractor()
    pages_by_doc: dict[str, list] = {}
    for doc_id, path in CORPUS:
        assert path.exists(), f"missing fixture {path}"
        result = extractor.extract(
            path.read_bytes(),
            document_id=doc_id,
            organization_id="org-eval",
            source=f"s3://eval/{doc_id}.pdf",
        )
        pages_by_doc[doc_id] = result.pages
    return pages_by_doc


def test_goldens_resolve_page_labels_against_real_extraction():
    rows = goldens(_extract_corpus())

    assert len(rows) >= 30
    # every golden has a resolved page in its expected document
    for row in rows:
        assert row.expected_document in _KNOWN_DOCS
        assert row.expected_page >= 1
        assert row.question.strip()
        assert _norm(row.expected_answer)

    # questions are unique — a duplicate would silently double-count scoring
    assert len({row.question for row in rows}) == len(rows)

    # every corpus document is actually exercised by the dataset
    assert {row.expected_document for row in rows} == _KNOWN_DOCS


def test_every_golden_answer_is_a_verbatim_sentence_from_its_document():
    pages_by_doc = _extract_corpus()
    rows = goldens(pages_by_doc)

    for row in rows:
        haystack = " ".join(page.content for page in pages_by_doc[row.expected_document])
        # `goldens` already asserts the sentence is present on ONE page; also
        # pin that the label is verbatim corpus text, not a human paraphrase.
        assert _norm(row.expected_answer) in _norm(haystack), (
            f"golden answer is not verbatim corpus text [{row.expected_document}]"
        )