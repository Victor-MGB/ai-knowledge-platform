"""Day-21 golden test dataset for end-to-end RAG evaluation.

Every row is `(question, expected_answer, expected_document, expected_page)` —
the four required evidence fields. `expected_answer` is the verbatim reference
sentence from the corpus that correctly answers the question (authored sentence-
by-sentence against the real fixture text, and guaranteed to be present in the
extracted output, so answer correctness is measured against actual content, not a
paraphrase a human invented). `expected_page` is resolved from the *actual
extraction output* at load time — the page whose text contains the answer
sentence. Resolving it from the corpus keeps the labels honest: the eval measures
against the text the deployed extractor really emits and refuses to run (assert)
if a golden sentence has drifted out of extraction.

`expected_document` is the corpus document id (`policies`, `arch`, `team`,
`service`, `chapter`), matching retrieval_quality.py's CORPUS.

The questions are the established Day-16 golden queries (natural, answerable
from the corpus, with a couple of cross-document distractors) — the Day-21 eval
extends them from retrieval-only coverage to full RAG answer + citation scoring.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Golden:
    question: str
    expected_answer: str
    expected_document: str
    expected_page: int


# (question, expected_answer, expected_document) — page filled in by `goldens`.
def _resolve_rows() -> list[tuple[str, str, str]]:
    return [
        # ---- retailer policies ------------------------------------------------
        ("how long is the return window after my order ships",
         "The 45 day window starts on the day the order ships, not the day it arrives.",
         "policies"),
        ("how many days do i have to send a purchased item back",
         "Items purchased from the retail store may be returned within 45 days of the purchase date for a full refund.",
         "policies"),
        ("which products may never be returned at all",
         "Personally engraved items, opened software, and consumed perishable goods are final sale and cannot be returned.",
         "policies"),
        ("who pays the label fee when a parcel arrives damaged",
         "Return shipping labels are provided free of charge for damaged, defective, or incorrectly shipped items.",
         "policies"),
        ("when is the refund issued after the warehouse receives the package",
         "Refunds are issued to the original form of payment within five business days once the returned package is received at the warehouse.",
         "policies"),
        ("when does my money come back after i mail a package back",
         "Refunds are issued to the original form of payment within five business days once the returned package is received at the warehouse.",
         "policies"),
        ("how much is the restocking fee for opened electronics and furniture",
         "A twelve percent restocking fee applies to opened electronics, furniture, and large appliances.",
         "policies"),
        ("can a gift receipt purchase be refunded in cash",
         "Items purchased with a gift receipt are eligible for an exchange or a store credit, never a cash refund.",
         "policies"),
        ("are duties refundable on international orders",
         "International orders are not eligible for prepaid return labels and duties are non-refundable.",
         "policies"),
        # ---- platform notes ---------------------------------------------------
        ("which index holds one graph per model so a cosine scan never crosses boundaries",
         "The vector store keeps a dedicated HNSW graph per embedding model so a cosine scan never crosses model boundaries.",
         "arch"),
        ("what happens to a job when its lease is not renewed before the timeout",
         "Jobs reserve a lease that must be renewed before the visibility timeout, otherwise the job is offered back to the queue.",
         "arch"),
        ("what token budget bounds a single embedding request",
         "A token budget of 512 tokens keeps most semantic units intact while bounding a single embedding request.",
         "arch"),
        ("where is the tenant scope enforced at the sql layer",
         "Every retrieval query is scoped to the requesting tenant at the SQL layer, and a leak across tenants is a release-blocking defect.",
         "arch"),
        ("does the system remember who owns what data",
         "Chunks and embeddings carry the organization id on every row so filtering never depends on application memory.",
         "arch"),
        ("how fast does an index lookup complete at small scale",
         "Index lookups complete in tens of milliseconds at small scale because the top-K scan touches only the best-matched nodes.",
         "arch"),
        ("how does an overlap tail help a claim split on a boundary",
         "Consecutive sliding windows share a tail of overlap tokens so a claim split across a boundary still matches a single query.",
         "arch"),
        # ---- team handbook ----------------------------------------------------
        ("when do new employees get their laptop and platform accounts",
         "New employees receive their laptop, access badge, and platform accounts within the first two working days.",
         "team"),
        ("how many paid days off does an employee accrue each calendar year",
         "Employees accrue 20 days of paid time off each calendar year, usable after the first 30 days of employment.",
         "team"),
        ("is remote work permitted for every role",
         "Remote work is allowed for any role whose duties do not require a physical presence at an office.",
         "team"),
        ("by when must an expense be submitted to be reimbursed",
         "Business expenses must be submitted within 30 days of the purchase date to be reimbursed.",
         "team"),
        ("how is harassment of customers or colleagues treated",
         "Employees must treat customers, partners, and each other with respect, and harassment of any kind is a terminating offence.",
         "team"),
        ("what is the overtime pay rate for non exempt staff",
         "Overtime is compensated at 1.5 times the hourly rate for non-exempt staff and approved in advance by the manager.",
         "team"),
        # ---- service plans ----------------------------------------------------
        ("how much does the starter plan cost per month",
         "The starter plan covers three projects and 10 gigabytes of storage at 29 dollars per month.",
         "service"),
        ("what availability is guaranteed on the business plan",
         "The service commits to 99.9 percent monthly availability across the business plan and above.",
         "service"),
        ("what response target applies to business plan support",
         "Business plans include a dedicated support channel with a response target of four business hours.",
         "service"),
        ("how long are backups kept and restorable",
         "Backups are kept for 14 days using encrypted storage and are restorable on request.",
         "service"),
        ("how often does the platform run an independent penetration test",
         "The platform undergoes an independent penetration test twice a year and a SOC 2 audit annually.",
         "service"),
        ("how much existing data can be imported without charge",
         "Migration to the service includes a free import of up to 50 gigabytes of existing data.",
         "service"),
        # ---- chapter fixture (no section metadata) ----------------------------
        ("what does knowflow turn uploaded documents into",
         "KnowFlow is a multi-tenant knowledge platform that turns uploaded documents into answerable, citable ground truth.",
         "chapter"),
        ("what is every retrieval query scoped to",
         "Every tenant owns its data outright; retrieval scopes every query to the requesting organization, never across it.",
         "chapter"),
        ("when does a document become useful to knowflow",
         "A document becomes useful only when it can be located, read, and cited, which is what this handbook describes.",
         "chapter"),
        # ---- cross-document distractors ---------------------------------------
        ("is a slower month made up to the customer with a credit",
         "Downtime beyond the commitment earns a service credit worth 10 percent of the monthly fee per hour.",
         "service"),
        ("what is the meal allowance cap when travelling for the company",
         "Alcohol and personal items are never reimbursable, and meals are capped at 60 dollars per person.",
         "team"),
    ]


def goldens(pages_by_doc: dict[str, list]) -> list[Golden]:
    """Assemble the dataset, resolving each golden's page from the extracted text.
    `pages_by_doc` maps a doc id to its extracted Page objects. A golden whose
    answer sentence is missing from extraction raises — stale labels must be
    caught, not silently skipped."""
    import re

    result: list[Golden] = []
    for question, answer, doc in _resolve_rows():
        norm = lambda s: re.sub(r"\s+", " ", s).strip()
        needle = norm(answer)
        pages = pages_by_doc[doc]
        assert any(needle in norm(p.content) for p in pages), (
            f"golden answer drifted from extractor output [{doc}] {needle!r}"
        )
        page = next(i + 1 for i, p in enumerate(pages) if needle in norm(p.content))
        result.append(Golden(question, answer, doc, page))
    return result
