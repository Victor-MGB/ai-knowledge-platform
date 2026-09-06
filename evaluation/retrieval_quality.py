"""Day-16 retrieval-quality experiments.

A golden-set evaluation over the real ingest pipeline -- pypdf extraction,
the app's own chunkers, and the app's own hashing embedder -- so the numbers
measure what the deployed stack actually returns, not a hand-rolled mock.

Method
------
1. Build the corpus: chapter.pdf (daily fixture; no outline) plus four
   outline-annotated fixtures generated deterministically by build_corpus.py
   (retailer policies, platform notes, team handbook, service plans).
2. For every (chunker, top-k, similarity-threshold) combination, chunk the
   corpus, embed every chunk and query, and rank by cosine.
3. Score against a hand-authored golden set (33 queries, each with the
   verbatim answer sentence it should surface):
     Coverage@k   fraction of golden ANSWER TOKENS the top-k context actually
                  carries -- the RAG-fidelity metric: can the assistant answer
                  from what retrieval returned?
     Hit@k / MRR  whether the single most answer-dense chunk makes the cut and
                  how early it ranks.
   Both are reported AFTER the similarity threshold prunes the list, so the
   tables show the real precision/recall trade of each knob.
4. Metadata-filter experiment: restricting to metadata.section before ranking.
   Configs that merge pages keep only the FIRST section label, so `reach`
   (does the target section still exist in the pool?) is reported per config:
   metadata filtering is only as good as the section attribution.

Run from the repo root:  .venv/bin/python evaluation/retrieval_quality.py
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if __package__ is None:  # running as a plain script, not `python -m`
    sys.path.insert(0, str(_ROOT / "ai-service"))

from app.processor.chunkers import chunk_pages  # noqa: E402
from app.processor.pdf import PdfExtractor  # noqa: E402
from app.services.embedding import HashingEmbeddingProvider, TOKEN_RE  # noqa: E402

from build_corpus import GOLD_SENTENCES  # noqa: E402

FIXTURES = _ROOT / "ai-service" / "tests" / "fixtures"

CORPUS = [
    ("policies", FIXTURES / "eval" / "policies.pdf"),
    ("arch", FIXTURES / "eval" / "architecture_notes.pdf"),
    ("team", FIXTURES / "eval" / "hr_handbook.pdf"),
    ("service", FIXTURES / "eval" / "service_guide.pdf"),
    ("chapter", FIXTURES / "chapter.pdf"),
]

PROVIDER = HashingEmbeddingProvider("knowflow-hash-384", dimensions=384)

# (doc_id, section_or_None, query, gold sentence verbatim in the extracted text)
GOLDENS = [
    # retailer policies -------------------------------------------------------
    ("policies", "Return Window", "how long is the return window after my order ships",
        GOLD_SENTENCES["retailer policies"]["Return Window"]),
    ("policies", "Return Window", "how many days do i have to send a purchased item back",
        GOLD_SENTENCES["retailer policies"]["Return Window"]),
    ("policies", "Return Eligibility", "which products may never be returned at all",
        GOLD_SENTENCES["retailer policies"]["Return Eligibility"]),
    ("policies", "Return Shipping", "who pays the label fee when a parcel arrives damaged",
        GOLD_SENTENCES["retailer policies"]["Return Shipping"]),
    ("policies", "Refund Processing", "when is the refund issued after the warehouse receives the package",
        GOLD_SENTENCES["retailer policies"]["Refund Processing"]),
    ("policies", "Refund Processing", "when does my money come back after i mail a package back",
        GOLD_SENTENCES["retailer policies"]["Refund Processing"]),
    ("policies", "Restocking Fee", "how much is the restocking fee for opened electronics and furniture",
        GOLD_SENTENCES["retailer policies"]["Restocking Fee"]),
    ("policies", "Gift Receipts and Exchanges", "can a gift receipt purchase be refunded in cash",
        GOLD_SENTENCES["retailer policies"]["Gift Receipts and Exchanges"]),
    ("policies", "International Orders", "are duties refundable on international orders",
        GOLD_SENTENCES["retailer policies"]["International Orders"]),
    # platform notes ----------------------------------------------------------
    ("arch", "Vector Indexing", "which index holds one graph per model so a cosine scan never crosses boundaries",
        GOLD_SENTENCES["platform notes"]["Vector Indexing"]),
    ("arch", "Queue Processing", "what happens to a job when its lease is not renewed before the timeout",
        GOLD_SENTENCES["platform notes"]["Queue Processing"]),
    ("arch", "Token Budgets", "what token budget bounds a single embedding request",
        GOLD_SENTENCES["platform notes"]["Token Budgets"]),
    ("arch", "Multi-Tenancy", "where is the tenant scope enforced at the sql layer",
        GOLD_SENTENCES["platform notes"]["Multi-Tenancy"]),
    ("arch", "Multi-Tenancy", "does the system remember who owns what data",
        GOLD_SENTENCES["platform notes"]["Multi-Tenancy"]),
    ("arch", "Retrieval Latency", "how fast does an index lookup complete at small scale",
        GOLD_SENTENCES["platform notes"]["Retrieval Latency"]),
    ("arch", "Overlap Strategy", "how does an overlap tail help a claim split on a boundary",
        GOLD_SENTENCES["platform notes"]["Overlap Strategy"]),
    # team handbook -----------------------------------------------------------
    ("team", "Onboarding", "when do new employees get their laptop and platform accounts",
        GOLD_SENTENCES["team handbook"]["Onboarding"]),
    ("team", "Time Off", "how many paid days off does an employee accrue each calendar year",
        GOLD_SENTENCES["team handbook"]["Time Off"]),
    ("team", "Remote Work", "is remote work permitted for every role",
        GOLD_SENTENCES["team handbook"]["Remote Work"]),
    ("team", "Expenses", "by when must an expense be submitted to be reimbursed",
        GOLD_SENTENCES["team handbook"]["Expenses"]),
    ("team", "Conduct", "how is harassment of customers or colleagues treated",
        GOLD_SENTENCES["team handbook"]["Conduct"]),
    ("team", "Payroll", "what is the overtime pay rate for non exempt staff",
        GOLD_SENTENCES["team handbook"]["Payroll"]),
    # service plans -----------------------------------------------------------
    ("service", "Plans and Pricing", "how much does the starter plan cost per month",
        GOLD_SENTENCES["service plans"]["Plans and Pricing"]),
    ("service", "Uptime Guarantee", "what availability is guaranteed on the business plan",
        GOLD_SENTENCES["service plans"]["Uptime Guarantee"]),
    ("service", "Support Channels", "what response target applies to business plan support",
        GOLD_SENTENCES["service plans"]["Support Channels"]),
    ("service", "Data Retention", "how long are backups kept and restorable",
        GOLD_SENTENCES["service plans"]["Data Retention"]),
    ("service", "Security Compliance", "how often does the platform run an independent penetration test",
        GOLD_SENTENCES["service plans"]["Security Compliance"]),
    ("service", "Migration Onboarding", "how much existing data can be imported without charge",
        GOLD_SENTENCES["service plans"]["Migration Onboarding"]),
    # chapter (daily fixture, intentionally no section metadata) --------------
    ("chapter", None, "what does knowflow turn uploaded documents into",
        "KnowFlow is a multi-tenant knowledge platform that turns uploaded documents into answerable, citable ground truth."),
    ("chapter", None, "what is every retrieval query scoped to",
        "retrieval scopes every query to the requesting organization, never across it."),
    ("chapter", None, "when does a document become useful to knowflow",
        "A document becomes useful only when it can be located, read, and cited, which is what this handbook describes."),
    # cross-document distractors ----------------------------------------------
    ("service", "Uptime Guarantee", "is a slower month made up to the customer with a credit",
        GOLD_SENTENCES["service plans"]["Uptime Guarantee"]),
    ("team", "Expenses", "what is the meal allowance cap when travelling for the company",
        GOLD_SENTENCES["team handbook"]["Expenses"]),
]

# (label, strategy, kwargs) -- kwargs map to chunk_pages' chunk_tokens /
# chunk_chars / overlap_tokens; chunk_size is the product's name for these.
CHUNK_CONFIGS = [
    ("paragraph 512 (prod default)", "paragraph", {"chunk_tokens": 512}),
    ("paragraph 256", "paragraph", {"chunk_tokens": 256}),
    ("paragraph 128", "paragraph", {"chunk_tokens": 128}),
    ("token 256", "token", {"chunk_tokens": 256}),
    ("token 128", "token", {"chunk_tokens": 128}),
    ("token 64", "token", {"chunk_tokens": 64}),
    ("overlap 256/128", "overlap", {"chunk_tokens": 256, "overlap_tokens": 128}),
    ("overlap 256/32", "overlap", {"chunk_tokens": 256, "overlap_tokens": 32}),
    ("overlap 128/32", "overlap", {"chunk_tokens": 128, "overlap_tokens": 32}),
    ("fixed 512 chars", "fixed", {"chunk_chars": 512}),
    ("fixed 1024 chars", "fixed", {"chunk_chars": 1024}),
]
TOP_KS = (1, 3, 5, 10)
MIN_SIMS = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5)


@dataclass(frozen=True)
class Frame:
    doc: str
    index: int
    section: str | None
    text: str
    vector: list[float]
    span: int = 1


def tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text.lower()))


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def load_corpus() -> dict[str, list]:
    extractor = PdfExtractor()
    pages_by_doc: dict[str, list] = {}
    for doc, path in CORPUS:
        pages = extractor.extract(
            path.read_bytes(),
            document_id=doc,
            organization_id="org-eval",
            source=f"s3://eval/{doc}.pdf",
        ).pages
        pages_by_doc[doc] = pages
    # Guard phrase: golden answers must match what extraction actually emits,
    # so the metric cannot silently chase stale text.
    for doc, section, query, gold in GOLDENS:
        joined = re.sub(r"\s+", " ", " ".join(p.content for p in pages_by_doc[doc]))
        assert gold in joined, f"gold string drifted from extractor output [{doc}] {gold!r}"
    return pages_by_doc


def chunk_config(pages_by_doc, label, strategy, kwargs) -> list[Frame]:
    frames: list[Frame] = []
    for doc, pages in pages_by_doc.items():
        records = chunk_pages(pages, strategy, **kwargs)
        for record in records:
            meta = record.metadata or {}
            page_range = meta.get("page_range") or [record.page_number, record.page_number]
            frames.append(Frame(
                doc=doc,
                index=record.chunk_index,
                section=meta.get("section"),
                text=record.content,
                vector=PROVIDER.embed(record.content).vector,
                span=page_range[-1] - page_range[0] + 1,
            ))
    return frames


def gold_sets(pages_by_doc) -> list[dict]:
    golds = []
    for doc, section, query, gold in GOLDENS:
        gold_toks = tokens(gold)
        joined = " ".join(p.content for p in pages_by_doc[doc])
        # work on normalized text; token set membership is enough for coverage
        golds.append({
            "doc": doc, "section": section, "query": query, "gold": gold,
            "toks": gold_toks,
        })
    return golds


def score(query_vec, pool, gold_toks, k, min_sim):
    """Return (coverage, rank_of_best_or_0, kept_count, pooled_ranks, pool).

    `pool` is the candidate frame list (optionally pre-filtered for the
    metadata experiment). The best (most answer-dense) frame is located
    INSIDE the pool, so portal-filtered runs rank against the filtered
    candidates, not a global index that no longer addresses them. Coverage is
    the gold-token fraction the top-k (post-threshold) context carries.
    """
    best_idx = best_index(pool, gold_toks)
    sims = [dot(query_vec, f.vector) for f in pool]
    order = sorted(range(len(pool)), key=lambda i: -sims[i])
    kept = [i for i in order if sims[i] >= min_sim]
    ranked = kept[:k]
    covered = set()
    for i in ranked:
        covered |= tokens(pool[i].text)
    cov = len(covered & gold_toks) / max(len(gold_toks), 1)
    try:
        rank = kept.index(best_idx) + 1 if best_idx in kept else 0
    except ValueError:
        rank = 0
    return cov, rank, len(kept), ranked, pool


def best_index(frames, best_toks):
    """Global index of the frame that carries the most gold tokens."""
    best_i, best_overlap = -1, -1
    for i, f in enumerate(frames):
        overlap = len(tokens(f.text) & best_toks)
        if overlap > best_overlap:
            best_i, best_overlap = i, overlap
    return best_i


def agg(rows):
    n = len(rows)
    def mean(key):
        return sum(r[key] for r in rows) / n
    return {
        "count": n,
        "cov": mean("cov"),
        "hit": sum(1 for r in rows if r["rank"] and r["rank"] <= r["k"]) / n,
        "mrr": mean("mrr"),
        "kept": mean("kept"),
    }


def main() -> None:
    pages_by_doc = load_corpus()
    golds = gold_sets(pages_by_doc)
    print(f"corpus: {sum(len(p) for p in pages_by_doc.values())} pages across "
          f"{len(pages_by_doc)} documents; {len(golds)} golden queries\n")

    # ---- table A: chunking strategies at k=5, no threshold -------------------
    print("TABLE A  chunking strategies  (top-k=5, min_similarity=0)\n"
          "         `best span` = mean physical page-range of each query's best\n"
          "         chunk; big spans mean answers are bundled into multi-page\n"
          "         units, which inflates coverage but hurts citing + filtering\n")
    print(f"{'chunk config':<26} {'#chunks':>7} {'Cov@5':>7} {'Hit@5':>6} {'MRR':>6} {'best span':>9}")
    per_config = {}
    for label, strategy, kwargs in CHUNK_CONFIGS:
        frames = chunk_config(pages_by_doc, label, strategy, kwargs)
        per_config[label] = frames
        rows = []
        for g in golds:
            qv = PROVIDER.embed(g["query"]).vector
            cov, rank, kept, _, _ = score(qv, frames, g["toks"], 5, 0.0)
            best_idx = best_index(frames, g["toks"])
            rows.append({"cov": cov, "rank": rank, "k": 5, "mrr": 1 / rank if rank else 0.0,
                         "kept": kept, "span": frames[best_idx].span})
        a = agg(rows)
        span = sum(r["span"] for r in rows) / len(rows)
        print(f"{label:<26} {len(frames):>7} {a['cov']:>7.3f} {a['hit']:>6.3f} {a['mrr']:>6.3f} {span:>9.2f}")
    print()

    # ---- table B: top-k effect on the production default ---------------------
    print("TABLE B  top-k effect  (paragraph 512, min_similarity=0)\n")
    frames = per_config["paragraph 512 (prod default)"]
    print(f"{'top-k':>5} {'Cov@k':>7} {'Hit@k':>6} {'MRR':>6} {'Cov/Hit@1':>10}")
    for k in TOP_KS:
        rows = []
        for g in golds:
            qv = PROVIDER.embed(g["query"]).vector
            cov, rank, kept, _, _ = score(qv, frames, g["toks"], k, 0.0)
            rows.append({"cov": cov, "rank": rank, "k": k, "mrr": 1 / rank if rank else 0.0, "kept": kept})
        a = agg(rows)
        cov1 = sum(r["cov"] for r in rows) / len(rows)
        hit1 = sum(1 for r in rows if r["rank"] == 1) / len(rows)
        print(f"{k:>5} {a['cov']:>7.3f} {a['hit']:>6.3f} {a['mrr']:>6.3f} {cov1:>7.3f}/{hit1:>6.3f}")
    print()

    # ---- table C: similarity threshold curve (k=5, prod chunking) ------------
    print("TABLE C  similarity threshold curve  (paragraph 512, top-k=5)\n")
    print(f"{'min_sim':>8} {'Cov@5':>7} {'Hit@5':>6} {'MRR':>6} {'kept':>6}")
    for ms in MIN_SIMS:
        rows = []
        for g in golds:
            qv = PROVIDER.embed(g["query"]).vector
            cov, rank, kept, _, _ = score(qv, frames, g["toks"], 5, ms)
            rows.append({"cov": cov, "rank": rank, "k": 5, "mrr": 1 / rank if rank else 0.0, "kept": kept})
        a = agg(rows)
        print(f"{ms:>8.1f} {a['cov']:>7.3f} {a['hit']:>6.3f} {a['mrr']:>6.3f} {a['kept']:>6.1f}")
    print()

    # ---- table D: metadata section filter, per chunk config -------------------
    print("TABLE D  metadata (section) filter  (top-k=3, no threshold)\n"
          "         measured on the section-bearing queries only. `reach` = how\n"
          "         often the target section still exists in the pool AFTER\n"
          "         chunking (multi-page merges keep only the FIRST section\n"
          "         label, so wide merges erase the sections they cross).\n")
    sec_golds = [g for g in golds if g["section"]]
    for cfg_label in ("paragraph 512 (prod default)", "paragraph 128", "token 64"):
        cfg_frames = per_config[cfg_label]
        rows_plain, rows_gated = [], []
        gates = []
        for g in sec_golds:
            qv = PROVIDER.embed(g["query"]).vector
            gate = lambda f, d=g["doc"], s=g["section"]: f.doc == d and f.section == s
            gated = [f for f in cfg_frames if gate(f)]
            reachable = bool(gated)
            cov, rank, kept, _, _ = score(qv, cfg_frames, g["toks"], 3, 0.0)
            rows_plain.append({"cov": cov, "rank": rank, "k": 3, "mrr": 1 / rank if rank else 0.0, "kept": kept})
            cov2, rank2, kept2, ranked2, _ = score(qv, gated, g["toks"], 3, 0.0)
            rows_gated.append({"cov": cov2, "rank": rank2, "k": 3, "mrr": 1 / rank2 if rank2 else 0.0, "kept": kept2})
            top1 = bool(ranked2) and gated[ranked2[0]].section == g["section"]
            gates.append((reachable, top1))
        a, b = agg(rows_plain), agg(rows_gated)
        reach = sum(1 for r, _ in gates if r) / len(gates)
        top1_ok = sum(1 for _, t in gates if t) / len(gates)
        print(f"  [{cfg_label}]   section queries: {len(sec_golds)}   reach: {reach:.3f}\n"
              f"    {'filter':<10} {'Cov@3':>7} {'Hit@3':>6} {'MRR':>6} {'kept':>6} {'top1 sec-ok':>11}\n"
              f"    {'none':<10} {a['cov']:>7.3f} {a['hit']:>6.3f} {a['mrr']:>6.3f} {a['kept']:>6.1f} {'':>11}\n"
              f"    {'section':<10} {b['cov']:>7.3f} {b['hit']:>6.3f} {b['mrr']:>6.3f} {b['kept']:>6.1f} {top1_ok:>10.3f}\n")
    print()

    # ---- per-query misses at the production default -------------------------
    print("TABLE E  per-query misses  (paragraph 512, top-k=5, min_similarity=0)\n")
    frames = per_config["paragraph 512 (prod default)"]
    for g in golds:
        qv = PROVIDER.embed(g["query"]).vector
        cov, rank, kept, _, _ = score(qv, frames, g["toks"], 5, 0.0)
        if rank == 0 or cov < 0.5:
            print(f"  MISS [{g['doc']}/{g['section']}] rank={rank} cov@5={cov:.2f} query={g['query']!r}")
    print()


if __name__ == "__main__":
    main()