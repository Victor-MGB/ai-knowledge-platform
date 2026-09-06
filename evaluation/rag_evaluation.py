"""Day-21 RAG evaluation — end-to-end scoring of the deployed RAG pipeline.

This is the step that separates the project from a typical beginner RAG
portfolio: it does not stop at "does retrieval return a related chunk" — it runs
the full RAG path (retrieval → context budget → generation → citations) and
grades the ANSWER against a golden test dataset.

Unlike the Day-16 retrieval study (which scored raw embedding arithmetic against
gold *sentences*), this evaluator drives the SAME production components the
deployed stack uses, so the numbers mean what the product would do:

  * extraction   — app.processor.pdf.PdfExtractor        (Day 9)
  * chunking     — app.processor.chunkers.chunk_pages     (Day 10, prod default)
  * embedding    — app.services.embedding.HashingEmbeddingProvider (the model
                   of record, Day 11)
  * retrieval    — cosine ranking over the corpus (the pgvector semantics, Day 15)
  * context      — best-first token budget (RagService.buildContext, Day 17)
  * generation   — app.rag.generator.RAGGenerator + ExtractiveRagProvider
                   (the deterministic faithful baseline, Day 17)
  * citations    — the stored `citations` provenance (Day 18)

The only layers deliberately NOT re-run here are the pgvector SQL index and the
HTTP transport — those are pinned by Day 15/16 and the backend integration tests;
the RAG *evaluation* contract lives here, self-contained and deterministic.

Metrics (see rag_metrics.py for definitions):
  document hit@K   retrieval accuracy: expected doc in the top-K
  page hit@K       ...and the expected page surface too
  Recall@K         fraction of all relevant chunks retrieved
  Precision@K      how much of the top-K is relevant
  answer correct   does the answer carry the expected evidence
  citation correct does the cited source resolve to the expected doc + page
  hallucination    non-refused answers claiming facts with no retrieved support

Run from the repo root:  .venv/bin/python evaluation/rag_evaluation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if __package__ is None:  # running as a plain script, not `python -m`
    sys.path.insert(0, str(_ROOT / "ai-service"))

from app.processor.chunkers import chunk_pages  # noqa: E402
from app.processor.pdf import PdfExtractor  # noqa: E402
from app.rag.generator import RAGGenerator  # noqa: E402
from app.rag.provider import ExtractiveRagProvider  # noqa: E402
from app.rag.schemas import ContextItem, RagGenerationRequest  # noqa: E402
from app.services.embedding import HashingEmbeddingProvider  # noqa: E402

from rag_eval_dataset import goldens  # noqa: E402
from rag_metrics import (  # noqa: E402
    RetrievedItem,
    QueryResult,
    aggregate,
    answer_correct,
    hallucinated,
    retrieval_metrics,
)

FIXTURES = _ROOT / "ai-service" / "tests" / "fixtures"

# (eval doc id, pdf path, human title shown in citations)
CORPUS = [
    ("policies", FIXTURES / "eval" / "policies.pdf", "Retailer Policies"),
    ("arch", FIXTURES / "eval" / "architecture_notes.pdf", "Platform Notes"),
    ("team", FIXTURES / "eval" / "hr_handbook.pdf", "Team Handbook"),
    ("service", FIXTURES / "eval" / "service_guide.pdf", "Service Plans"),
    ("chapter", FIXTURES / "chapter.pdf", "KnowFlow Handbook"),
]

PROVIDER = HashingEmbeddingProvider("knowflow-hash-384", dimensions=384)

TOP_KS = (1, 3, 5)
GENERATION_K = 5          # retrieve top-5 chunks for the answer
MAX_CONTEXT_TOKENS = 2000  # isolate retrieval+generation from budget trim


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def token_count(text: str) -> int:
    # mirror the app's estimate well enough for the budget
    return max(1, (len(text) + 3) // 4)


def main() -> None:
    extractor = PdfExtractor()
    pages_by_doc: dict[str, list] = {}
    chunks: list[dict] = []  # {doc, title, page_start, page_end, section, text, vector}
    for doc_id, path, title in CORPUS:
        pages = extractor.extract(
            path.read_bytes(),
            document_id=doc_id,
            organization_id="org-eval",
            source=f"s3://eval/{doc_id}.pdf",
        ).pages
        pages_by_doc[doc_id] = pages
        for record in chunk_pages(pages, "paragraph", chunk_tokens=512):
            meta = record.metadata or {}
            page_range = meta.get("page_range") or [record.page_number, record.page_number]
            chunks.append(
                {
                    "doc": doc_id,
                    "title": title,
                    "page_start": int(page_range[0]),
                    "page_end": int(page_range[-1]),
                    "section": meta.get("section"),
                    "text": record.content,
                    "vector": PROVIDER.embed(record.content).vector,
                }
            )

    dataset = goldens(pages_by_doc)
    print(f"corpus: {sum(len(p) for p in pages_by_doc.values())} pages across "
          f"{len(pages_by_doc)} documents -> {len(chunks)} chunks; "
          f"golden set: {len(dataset)} questions\n")

    generator = RAGGenerator(ExtractiveRagProvider("knowflow-extract-1"))
    results: list[QueryResult] = []

    for golden in dataset:
        qv = PROVIDER.embed(golden.question).vector
        sims = [dot(qv, c["vector"]) for c in chunks]
        order = sorted(range(len(chunks)), key=lambda i: -sims[i])

        # all relevant chunks in the whole corpus (recall denominator)
        all_relevant = sum(
            1
            for c in chunks
            if c["doc"] == golden.expected_document
            and golden.expected_page in range(c["page_start"], c["page_end"] + 1)
        )

        items = [
            RetrievedItem(
                rank=i,
                document=chunks[idx]["doc"],
                page=chunks[idx]["page_start"],
                pages=tuple(range(chunks[idx]["page_start"], chunks[idx]["page_end"] + 1)),
                text=chunks[idx]["text"],
            )
            for i, idx in enumerate(order)
        ]

        res = QueryResult(golden.question, golden.expected_document, golden.expected_page)
        res.__dict__.update(
            retrieval_metrics(items, golden.expected_document, golden.expected_page, all_relevant, TOP_KS)
        )

        # ---- RAG generation on the top-K -------------------------------------------------
        context: list[ContextItem] = []
        context_chunk: list[dict] = []  # parallel to context: originating chunk
        total = 0
        for idx in order[:GENERATION_K]:
            c = chunks[idx]
            toks = token_count(c["text"])
            if total > 0 and total + toks > MAX_CONTEXT_TOKENS:
                continue
            context.append(
                ContextItem(
                    text=c["text"],
                    similarity=min(1.0, max(0.0, sims[idx])),
                    section=c["section"],
                    page=c["page_start"],
                    chunk_id=f"{c['doc']}#{idx}",
                    document_id=c["doc"],
                    document_title=c["title"],
                )
            )
            context_chunk.append(c)
            total += toks

        response = generator.generate(
            RagGenerationRequest(
                question=golden.question,
                context=context,
                max_context_tokens=MAX_CONTEXT_TOKENS,
            )
        )
        res.refused = response.refused
        if response.refused:
            # a refusal is honest, never a hallucination and never a correct answer
            res.answer_correct = False
            res.hallucinated = False
            res.citation_correct = False
            res.citation_document_correct = False
            res.citation_page_exact = False
            res.citation_page_covers = False
        else:
            res.answer_correct = answer_correct(golden.expected_answer, response.answer)
            res.hallucinated = hallucinated(
                response.answer, [c.text for c in context]
            )
            # the extractive provider cites exactly the source it quoted from; its
            # `id` is the 1-based context index, so map back to the full chunk span.
            cited = response.citations[0] if response.citations else None
            cited_chunk = (
                context_chunk[cited.id - 1] if cited and 1 <= cited.id <= len(context_chunk) else None
            )
            if cited_chunk is not None:
                doc_ok = cited_chunk["doc"] == golden.expected_document
                span = (cited_chunk["page_start"], cited_chunk["page_end"])
                page_covers = span[0] <= golden.expected_page <= span[1]
                page_exact = cited_chunk["page_start"] == golden.expected_page
                res.citation_document_correct = doc_ok
                res.citation_page_exact = page_exact
                res.citation_page_covers = page_covers
                res.citation_correct = doc_ok and page_covers
                if not doc_ok:
                    print(
                        f"  DBG [{golden.expected_document}/p{golden.expected_page}] "
                        f"cited {cited_chunk['doc']}/p{span[0]}-{span[1]} "
                        f"answ={' '.join(response.answer.split())[:70]!r}\n"
                        f"       context(n={len(context_chunk)}): "
                        + ", ".join(f"{cc['doc']}/p{cc['page_start']}" for cc in context_chunk)
                    )
            else:
                res.citation_correct = False
                res.citation_document_correct = False
                res.citation_page_exact = False
                res.citation_page_covers = False
        results.append(res)

    # ---- report ----------------------------------------------------------------------------
    agg = aggregate(results, TOP_KS)
    print("RETRIEVAL  (graded against the expected document + expected page)\n")
    print(f"{'k':>3} | {'document hit@k':>14} | {'page hit@k':>11} | {'recall@k':>9} | {'precision@k':>11}")
    for k in TOP_KS:
        print(
            f"{k:>3} | {agg['document_hit'][k]:>14.3f} | {agg['page_hit'][k]:>11.3f} "
            f"| {agg['recall'][k]:>9.3f} | {agg['precision'][k]:>11.3f}"
        )
    print()
    print("ANSWER QUALITY  (full RAG path: retrieval -> budget -> generate -> cite)\n")
    print(f"  answer correct                  : {agg['answer_correct']:.3f}")
    print(f"  citation -> document correct    : {agg['citation_document_correct']:.3f}")
    print(f"  citation -> page covers answer  : {agg['citation_page_covers']:.3f}")
    print(f"  citation correct (doc + page)   : {agg['citation_correct']:.3f}")
    print(f"  citation -> EXACT page match    : {agg['citation_page_exact']:.3f}")
    print(f"  hallucination rate              : {agg['hallucinated']:.3f}")
    print(f"  refused (honest 'I don't know.')     : {agg['refused_rate']:.3f}")
    print()

    print("PER-QUERY FAILURES  (document hit@5 == False, or citation doc wrong)\n")
    for r in results:
        if (not r.document_hit[5]) or (r.citation_document_correct is False):
            print(
                f"  [{r.truth_document}/p{r.truth_page}] doc_hit@5="
                f"{bool(r.document_hit[5])} cite_doc={r.citation_document_correct} "
                f"q={r.question!r}"
            )
    print()


if __name__ == "__main__":
    main()
