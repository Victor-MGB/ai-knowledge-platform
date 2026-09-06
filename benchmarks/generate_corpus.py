#!/usr/bin/env python3
"""Deterministic PDF corpus generator for the Day-33 benchmark.

Every document is a realistic multi-section "business manual" on a distinct
topic, built from fact templates so later retrieval has real signal to find.
`--pages` controls size (default ~6, matching the corpus used during
development). Outputs one PDF per document plus `queries.txt` (one golden
query per fact) so search/RAG benchmarks aim at knowable answers.
"""

import argparse
import random
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

TOPICS = {
    "multi-region-refund-policy": {
        "title": "Multi-Region Refund Policy",
        "facts": [
            "Refunds in the EU region are processed within 14 days of the written request.",
            "Customers in North America receive store credit instead of cash refunds after 30 days.",
            "The Asia-Pacific region requires a return authorization code printed on the original invoice.",
            "Refund requests older than 90 days are escalated to the finance committee automatically.",
            "Cross-border refunds are credited in the currency of the original purchase transaction.",
            "Digital subscription refunds must be requested within the first seven days of billing.",
        ],
    },
    "employee-onboarding-handbook": {
        "title": "Employee Onboarding Handbook",
        "facts": [
            "New hires complete security awareness training within their first two weeks.",
            "Laptop provisioning is handled by the IT desk through the service portal ticket WORK-1001.",
            "The buddy program pairs every new employee with a tenured teammate for ninety days.",
            "Access badges grant floor entry only between 6:00 AM and 9:00 PM on business days.",
            "Payroll requires bank details to be submitted by the fifth of the hiring month.",
            "The mentorship review happens at day thirty with the direct manager and the peer.",
        ],
    },
    "network-operations-runbook": {
        "title": "Network Operations Runbook",
        "facts": [
            "Core switch failover targets a recovery time under thirty seconds.",
            "The nightly config backup job runs at 01:00 UTC and retains thirty snapshots.",
            "Border routers announce routes using BGP with a 5-minute keepalive hold time.",
            "The NOC escalates any packet-loss rate above two percent to the network team.",
            "Maintenance windows are reserved on the last Saturday of each month from 02:00 to 06:00 UTC.",
            "Ansible inventory must be refreshed before any configuration change is applied.",
        ],
    },
    "data-retention-policy": {
        "title": "Data Retention Policy",
        "facts": [
            "Transactional records are retained for seven years before archival.",
            "Marketing campaign logs expire after twelve months unless a legal hold applies.",
            "Customer chat transcripts are stored for 180 days in the hot tier.",
            "Backup tapes are destroyed ninety days after the retention window closes.",
            "Personnel files are purged two years after an employee departs the company.",
            "Vendor contracts move to cold storage after the agreement term plus one year.",
        ],
    },
    "customer-success-playbook": {
        "title": "Customer Success Playbook",
        "facts": [
            "Renewal outreach begins at day ninety before the subscription end date.",
            "Accounts at risk are flagged when weekly active usage drops below fifty percent.",
            "The quarterly business review covers adoption, expansion, and advocacy metrics.",
            "Churn-risk accounts receive an executive sponsor within five business days.",
            "Net promoter score surveys are sent forty-eight hours after onboarding closes.",
            "Success plans are reviewed by the account team on the first Monday of each month.",
        ],
    },
    "security-incident-response": {
        "title": "Security Incident Response Plan",
        "facts": [
            "Severity-one incidents require the on-call engineer to respond within ten minutes.",
            "Proof of compromise must be preserved on write-once media before containment.",
            "The communication template is approved by the legal team before release.",
            "Forensic images are hashed and the hashes are recorded in the incident log.",
            "Public disclosure is coordinated with the affected customers within twenty-four hours.",
            "Post-incident reviews are scheduled no later than five working days after closure.",
        ],
    },
    "api-developer-guide": {
        "title": "REST API Developer Guide",
        "facts": [
            "All endpoints require an OAuth bearer token issued by the identity service.",
            "List endpoints support offset and limit pagination with a default page size of fifty.",
            "Idempotency keys must be unique per client and retained for twenty-four hours.",
            "Webhook deliveries are retried with exponential backoff up to five attempts.",
            "The rate limit is one hundred requests per minute per API key.",
            "Deprecated fields are announced three releases before removal.",
        ],
    },
    "warehouse-safety-manual": {
        "title": "Warehouse Safety Manual",
        "facts": [
            "Forklift operators must complete recertification every twelve months.",
            "Pallet stacks taller than six units require a second worker for retrieval.",
            "Eyewash stations are inspected on the first day of each quarter.",
            "Loose conveyor belts are reported to maintenance within the same shift.",
            "Loading docks keep the exterior door sealed except during active load cycles.",
            "Hazardous materials are stored on secondary containment trays in zone D.",
        ],
    },
    "marketing-brand-guide": {
        "title": "Brand and Marketing Guide",
        "facts": [
            "The primary logo is used on dark backgrounds with a minimum clearance of one inch.",
            "Headline type must be set in the display family at weights above six hundred.",
            "Social posts require an accessibility text alternative for every image.",
            "Product screenshots are allowed only on the approved device shells.",
            "Email headers reserve a five hundred pixel safe zone for the main asset.",
            "Campaign naming follows the pattern CAMPAIGN-SEASON-CHANNEL-TARGET.",
        ],
    },
    "financial-reporting-calendar": {
        "title": "Financial Reporting Calendar",
        "facts": [
            "Monthly close reports are due by the third business day of the following month.",
            "Quarterly earnings are published within forty-five days of the quarter end.",
            "The annual audit window opens on January fifteenth every year.",
            "Expense audits are drawn randomly at a rate of five percent of submissions.",
            "Deferred revenue schedules reconcile weekly against the billing system.",
            "Board packs are distributed to directors seventy-two hours before the meeting.",
        ],
    },
}

FILLER = [
    "This section is maintained by the owning team and reviewed on the standard cycle.",
    "Exceptions require approval from the accountable owner and are logged centrally.",
    "All references in this document correspond to the current approved version.",
    "Questions about interpretation should be raised through the official support channel.",
    "The operational calendar supersedes any conflicting schedule in regional playbooks.",
    "Measurements in this policy follow the corporate standard units of record.",
    "Owners receive automated reminders when a review is due for this area.",
]


def build_document(seed: int, topic: str, target_pages: int, styles) -> list:
    rng = random.Random(f"knowflow-{topic}-{seed}")
    data = TOPICS[topic]

    def sentence(fact: str) -> str:
        starts = [
            f"As documented in this chapter, {fact}",
            f"The standing rule states that {fact}",
            f"Operationally, {fact}",
        ]
        return rng.choice(starts)

    paras: list[str] = []
    for fact in data["facts"]:
        paras.append(",".join([sentence(fact).strip(), " " + fact]) + ".")
    paras.append(
        f"This policy applies to all units operating in regions where {topic.replace('-', ' ')} is enforced."
    )

    # pad with filler paragraphs until we exceed the page target (≈16 paras/page
    # at 10pt with letter margins, measured)
    fill_pool = list(FILLER)
    rng.shuffle(fill_pool)
    i = 0
    while len(paras) < target_pages * 34:
        paras.append(fill_pool[i % len(fill_pool)])
        i += 1

    flow: list = []
    flow.append(Paragraph(data["title"], styles["Title"]))
    flow.extend(
        Paragraph(p, styles["BodyText"]) for n, p in enumerate(paras, 1)
    )
    return flow


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=10, help="number of documents (default 10)")
    ap.add_argument("--pages", type=int, default=6, help="target pages per document")
    ap.add_argument("--out", type=str, default="corpus", help="output directory")
    ap.add_argument("--large", type=int, default=0, help="also write a single large PDF of N pages")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    topics = list(TOPICS.keys())
    queries: list[str] = []
    qseen: set[str] = set()

    for t in topics:
        for fact in TOPICS[t]["facts"]:
            q = f"What is the rule about {fact}"
            if q not in qseen:
                qseen.add(q)
                queries.append(q)

    styles = getSampleStyleSheet()

    for i in range(args.count):
        topic = topics[i % len(topics)]
        file = out / f"kf_doc_{i:05d}.pdf"
        doc = SimpleDocTemplate(
            str(file),
            pagesize=letter,
            topMargin=0.9 * inch,
            bottomMargin=0.9 * inch,
            leftMargin=1.0 * inch,
            rightMargin=1.0 * inch,
        )
        doc.build(build_document(i, topic, args.pages, styles))

    if args.large:
        file = out / "large_doc.pdf"
        big_topic = "network-operations-runbook"
        doc = SimpleDocTemplate(
            str(file),
            pagesize=letter,
            topMargin=0.9 * inch,
            bottomMargin=0.9 * inch,
            leftMargin=1.0 * inch,
            rightMargin=1.0 * inch,
        )
        doc.build(build_document(9999, big_topic, args.large, styles))
        print(f"large: {file}")

    qfile = out / "queries.txt"
    qfile.write_text("\n".join(queries) + "\n")

    total = sum(1 for p in out.glob("kf_doc_*.pdf")) + (1 if (out / "large_doc.pdf").exists() else 0)
    print(f"wrote {total} PDFs to {out}/")
    print(f"wrote {len(queries)} golden queries to {qfile}")


if __name__ == "__main__":
    main()