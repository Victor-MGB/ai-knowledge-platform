"""Deterministic evaluation fixtures for the Day-16 retrieval-quality study.

Generates four PDFs under tests/fixtures/eval/ with real outline bookmarks so
the processor's extractor records `metadata.section` per page (the same signal
the product uses for citation). chapter.pdf from tests/fixtures is the fifth
corpus document and intentionally has NO outline -- it measures retrieval that
must stand without section metadata.

Layout rules the study depends on:
- every section starts on its own page, so the extractor's page-granular
  outline resolution assigns the right section label (same convention as the
  three_pages.pdf fixture);
- the first sentence of the FIRST paragraph of each section is the golden
  answer used by retrieval_quality.py, so the gold must stay verbatim here.

Run from the repo root:  .venv/bin/python evaluation/build_corpus.py
"""

from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

_OUT_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = _OUT_DIR / "ai-service" / "tests" / "fixtures" / "eval"

# (document_title, [(section_title, [paragraphs...]), ...])  -- the first
# sentence of each section's first paragraph is its golden answer.
DOCUMENTS: dict[str, list[tuple[str, list[str]]]] = {
    "retailer policies": [
        (
            "Return Window",
            [
                "Items purchased from the retail store may be returned within 45 days of the purchase date for a full refund.",
                "The 45 day window starts on the day the order ships, not the day it arrives.",
                "Orders placed in the final week of a seasonal sale extend the window to 60 days for loyalty members.",
                "A return after the window closes is reviewed case by case and most often declined unless the fault is ours.",
            ],
        ),
        (
            "Return Eligibility",
            [
                "Products must be unopened, unused, and in the original packaging to qualify for a standard return.",
                "Personally engraved items, opened software, and consumed perishable goods are final sale and cannot be returned.",
                "A product that arrives damaged is always returnable regardless of packaging, and photos are not required first.",
                "Mattresses and box springs are eligible only if the protective bag was never removed.",
                "Counterfeit claims require the original receipt and are forwarded to the brand partner before any refund.",
            ],
        ),
        (
            "Return Shipping",
            [
                "Return shipping labels are provided free of charge for damaged, defective, or incorrectly shipped items.",
                "A regular customer pays a flat six dollar label fee that is refunded when the returned item passes inspection.",
                "Items returned in person at any of our regional stores never incur a label fee.",
                "Bulk pallet returns for furniture should use the logistics portal instead of the consumer label.",
            ],
        ),
        (
            "Refund Processing",
            [
                "Refunds are issued to the original form of payment within five business days once the returned package is received at the warehouse.",
                "Store credit is issued immediately on receipt and never expires.",
                "Payments made with a gift card are refunded back to a new gift card rather than cash.",
                "Split payments are refunded proportionally to each original method in the same proportions they were charged.",
                "Electronically initiated refunds appear on the statement within two extra banking days depending on the issuer.",
            ],
        ),
        (
            "Restocking Fee",
            [
                "A twelve percent restocking fee applies to opened electronics, furniture, and large appliances.",
                "The fee is calculated on the original purchase price before tax and is deducted from the refund amount.",
                "Unopened items, even in these categories, avoid the restocking fee entirely.",
                "The fee never applies to items returned because they arrived damaged, defective, or substituted.",
            ],
        ),
        (
            "Gift Receipts and Exchanges",
            [
                "Items purchased with a gift receipt are eligible for an exchange or a store credit, never a cash refund.",
                "An exchange is honored within the same 45 day window using the current selling price.",
                "Gift receipts never expose the purchase price, so the clerk must use the item price lookup when exchanging.",
                "Seasonal gift cards distributed at checkout are electronic and loadable instantly.",
            ],
        ),
        (
            "International Orders",
            [
                "International orders are not eligible for prepaid return labels and duties are non-refundable.",
                "Cross-border returns must ship back with the customer arranging their own carrier and must clear customs marked as a return.",
                "Any duties or taxes paid at checkout are handled by the buyer's local authority and are outside this policy.",
                "Exchanges for international orders ship only after the original item is scanned in by the freight partner.",
            ],
        ),
    ],
    "platform notes": [
        (
            "Vector Indexing",
            [
                "The vector store keeps a dedicated HNSW graph per embedding model so a cosine scan never crosses model boundaries.",
                "Each graph stores points by approximate nearest neighbour links and answers a top-K query in a small beacon scan.",
                "The index applies to a fixed vector dimension because the graph geometry is built for that dimension at build time.",
                "A second embedding model therefore builds its own graph rather than sharing the existing one.",
            ],
        ),
        (
            "Queue Processing",
            [
                "Jobs reserve a lease that must be renewed before the visibility timeout, otherwise the job is offered back to the queue.",
                "A lease identifies a single worker so two workers never process the same job at the same time.",
                "Failed attempts retry with exponential backoff up to a configured ceiling before the job moves to a dead state.",
                "Successful jobs release the lease and record the outcome so the orchestrator can emit metrics.",
            ],
        ),
        (
            "Token Budgets",
            [
                "A token budget of 512 tokens keeps most semantic units intact while bounding a single embedding request.",
                "Paragraphs that exceed the budget are hard-split at word boundaries so no single unit is ever dropped.",
                "The budget is measured with a character based estimate that stays within a few percent of a real tokenizer on prose.",
                "Smaller budgets multiply the number of units and raise the embedding cost for the same corpus.",
            ],
        ),
        (
            "Multi-Tenancy",
            [
                "Every retrieval query is scoped to the requesting tenant at the SQL layer, and a leak across tenants is a release-blocking defect.",
                "Chunks and embeddings carry the organization id on every row so filtering never depends on application memory.",
                "The tenant scoping applies at documents, chunks, embeddings, and ingestion jobs alike.",
                "Cross tenant queries return an empty result set rather than exposing another tenant's rows.",
            ],
        ),
        (
            "Retrieval Latency",
            [
                "Index lookups complete in tens of milliseconds at small scale because the top-K scan touches only the best-matched nodes.",
                "Latency grows with the number of embedding models in play, since each model scans its own graph.",
                "Worst-case behaviour is a sequential scan over every embedding of every model, which the system avoids by index choice.",
                "The latency budget for a search request is kept under one second end to end at the p99 percentile.",
            ],
        ),
        (
            "Overlap Strategy",
            [
                "Consecutive sliding windows share a tail of overlap tokens so a claim split across a boundary still matches a single query.",
                "The overlap tail is much smaller than the window itself; typical settings share 40 to 80 tokens.",
                "Overlap raises storage only slightly while it meaningfully improves boundary recall for long passages.",
                "The tail duplicates the shared text across both windows, so the embedding of each window stays contiguous.",
            ],
        ),
    ],
    "team handbook": [
        (
            "Onboarding",
            [
                "New employees receive their laptop, access badge, and platform accounts within the first two working days.",
                "The first week covers security training, source control access, and a short project walkthrough with the team lead.",
                "Probation lasts 90 days and includes a midpoint check-in with the manager.",
            ],
        ),
        (
            "Time Off",
            [
                "Employees accrue 20 days of paid time off each calendar year, usable after the first 30 days of employment.",
                "Sick leave of up to ten days is separate from the vacation balance and does not require a doctor's note.",
                "Unused vacation rolls over up to five days into the next calendar year.",
                "Requests of two weeks or more must be filed at least 45 days ahead so project staffing can be arranged.",
            ],
        ),
        (
            "Remote Work",
            [
                "Remote work is allowed for any role whose duties do not require a physical presence at an office.",
                "A remote employee must be reachable during core hours from 10am to 4pm in their local timezone.",
                "Equipment is shipped by the company and remains company property for the duration of employment.",
            ],
        ),
        (
            "Expenses",
            [
                "Business expenses must be submitted within 30 days of the purchase date to be reimbursed.",
                "Receipts are mandatory for every amount above 25 dollars and can be attached as a photo in the tool.",
                "Travel bookings made through the company portal are billed directly and do not need a receipt.",
                "Alcohol and personal items are never reimbursable, and meals are capped at 60 dollars per person.",
            ],
        ),
        (
            "Conduct",
            [
                "Employees must treat customers, partners, and each other with respect, and harassment of any kind is a terminating offence.",
                "Confidential information belongs to the company both during and after employment.",
                "Secondary employment requires written approval and must not compete with the company's business.",
            ],
        ),
        (
            "Payroll",
            [
                "Salaries are paid monthly on the last working day of the month into the employee's nominated bank account.",
                "Payslips are available on the HR portal three working days before payday.",
                "Overtime is compensated at 1.5 times the hourly rate for non-exempt staff and approved in advance by the manager.",
            ],
        ),
    ],
    "service plans": [
        (
            "Plans and Pricing",
            [
                "The starter plan covers three projects and 10 gigabytes of storage at 29 dollars per month.",
                "The business plan covers unlimited projects and 1 terabyte at 149 dollars per month with priority support.",
                "Annual billing grants two months free on every plan.",
            ],
        ),
        (
            "Uptime Guarantee",
            [
                "The service commits to 99.9 percent monthly availability across the business plan and above.",
                "Downtime beyond the commitment earns a service credit worth 10 percent of the monthly fee per hour.",
                "Planned maintenance windows are published seven days in advance and never count against the guarantee.",
            ],
        ),
        (
            "Support Channels",
            [
                "Business plans include a dedicated support channel with a response target of four business hours.",
                "All plans get self-service help centre articles and a community forum monitored by the engineering team.",
                "Phone support is reserved for enterprise contracts only.",
            ],
        ),
        (
            "Data Retention",
            [
                "Customer data is retained for the duration of the active subscription and deleted within 30 days of cancellation.",
                "Backups are kept for 14 days using encrypted storage and are restorable on request.",
                "Logs and metrics are stored up to 90 days and are not shared with any third party.",
            ],
        ),
        (
            "Security Compliance",
            [
                "All customer data is encrypted in transit and at rest using industry standard TLS and AES-256.",
                "The platform undergoes an independent penetration test twice a year and a SOC 2 audit annually.",
                "Subprocessors list is published on the trust page and customers are notified 30 days before a change.",
            ],
        ),
        (
            "Migration Onboarding",
            [
                "Migration to the service includes a free import of up to 50 gigabytes of existing data.",
                "A dedicated onboarding engineer supports the first deployment for business plans and above.",
                "Migration happens at a scheduled window and always keeps the old environment available as a rollback.",
            ],
        ),
    ],
}

# Verbatim golden answers (first sentence of each section's first paragraph),
# consumed by retrieval_quality.py. Kept here so gold and content cannot drift.
GOLD_SENTENCES: dict[str, dict[str, str]] = {}
for doc, sections in DOCUMENTS.items():
    GOLD_SENTENCES[doc] = {name: paragraphs[0] for name, paragraphs in sections}


def _styles():
    body = ParagraphStyle(
        name="Body",
        fontName="Helvetica",
        fontSize=10,
        leading=13,
        alignment=TA_JUSTIFY,
        spaceAfter=6,
    )
    heading = ParagraphStyle(
        name="Heading1",
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=16,
        spaceBefore=10,
        spaceAfter=8,
    )
    title = ParagraphStyle(
        name="Title",
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=20,
        spaceAfter=12,
    )
    return title, heading, body


class _OutlineDoc(SimpleDocTemplate):
    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name == "Heading1":
            key = flowable.getPlainText()
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(key, key, level=0, closed=False)


def _reset_outline(filename: str, headings: list[str]) -> None:
    """Rebuild /Outlines with correct page numbers.

    reportlab captures `bookmarkPage` during its LAYOUT pass, when the canvas
    is still on page one, so every entry it writes points at the first page.
    The extraction layer (Day 9) trusts outline->page to assign `section`, so
    a wrong page means every page inherits the LAST section. Rebuild the tree
    from pypdf instead: locate each heading's page by text search and emit an
    outline item bound to that page object (0-based page number).
    """
    out_path = OUT_DIR / filename
    reader = PdfReader(str(out_path))
    pages = [(i, page.extract_text() or "") for i, page in enumerate(reader.pages)]
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    for title in headings:
        page_number = next(
            (i for i, text in pages if f"\n{title}\n" in f"\n{text}\n"),
            None,
        )
        if page_number is None:
            raise RuntimeError(f"heading not found verbatim in {filename}: {title!r}")
        writer.add_outline_item(title, writer.pages[page_number])
    with open(out_path, "wb") as fh:
        writer.write(fh)


DOC_STEM = {
    "retailer policies": "policies.pdf",
    "platform notes": "architecture_notes.pdf",
    "team handbook": "hr_handbook.pdf",
    "service plans": "service_guide.pdf",
}


def _doc(title, sections, filename):
    doc = _OutlineDoc(
        str(OUT_DIR / filename),
        pagesize=letter,
        leftMargin=inch,
        rightMargin=inch,
        topMargin=0.8 * inch,
        bottomMargin=0.8 * inch,
        title=title,
    )
    title_style, heading, body = _styles()
    story = [Paragraph(title, title_style), Spacer(1, 6)]
    for position, (name, paragraphs) in enumerate(sections):
        if position > 0:
            story.append(PageBreak())
        story.append(Paragraph(name, heading))
        for paragraph in paragraphs:
            story.append(Paragraph(paragraph, body))
    doc.build(story)
    _reset_outline(filename, [name for name, _ in sections])


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for doc, filename in DOC_STEM.items():
        _doc(doc.title(), DOCUMENTS[doc], filename)
        print("wrote", OUT_DIR / filename)


if __name__ == "__main__":
    main()