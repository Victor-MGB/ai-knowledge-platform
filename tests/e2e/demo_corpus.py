"""Day 34 — production demo corpus: a realistic 10-document company dataset.

Builds ten internally-consistent PDFs for a fictional B2B analytics company,
Lumina Systems (analytics platform + Pulse alerting, ~140 staff). The layout
rules match the Day-16 evaluation corpus so the product's extractor records
`metadata.section` per page:

- every section starts on its own page, so page-granular outline resolution
  assigns the right section label;
- the FIRST sentence of the FIRST paragraph of each section is the answerable
  fact, and the demo questions in tests/e2e/demo.py mirror that sentence's vocabulary
  so the hash embedder can find it;
- each section is auto-padded to ~470-505 extracted tokens (PAD_FLOOR /
  PAD_TARGET below) so the production default (paragraph strategy, 512-token
  budget) emits exactly one chunk per section: the paragraph chunker only
  opens a new chunk when the projected tokens exceed 512, so any section that
  leaves headroom swallows the next section's first paragraphs (and its
  golden sentence) under its own label. Squeezing every section up to the
  budget forces a clean boundary. That is what makes retrieval
  discriminating: a question about payroll ranks the Payroll chunk, not the
  whole HR policy, and the citation resolves to the exact section and page
  instead of a neighbouring section.

Documents (each a self-contained piece of the same company story):
  Employee Handbook       product-level culture, leave, remote work, reviews
  Product Documentation   Lumina Analytics platform, connectors, Pulse, pricing
  Engineering Handbook    code standards, coverage, review, deploy, on-call
  Security Policy         MFA, encryption, incident response, vendor risk
  Customer Policy         support SLAs, uptime, retention, refunds
  Financial Policy        expenses, approvals, cards, travel, budget reviews
  HR Policy               benefits, the leave *process*, payroll, equity
  API Documentation       bearer auth, rate limits, endpoints, webhooks, errors
  Onboarding Guide        day one, Okta, first weeks, buddy, 30-60-90
  Research Report         2025 cohort study: retention, adoption, performance

Employee Handbook vs HR Policy and Customer Policy vs Financial Policy carry
deliberately overlapping topics (leave, refunds) with distinct facts, so a
query that says too little must be disambiguated by retrieval, not by the
question text.

Run:  ai-service/.venv/bin/python -m tests.e2e.demo_corpus
"""

import math
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

OUT_DIR = Path(__file__).resolve().parent / "fixtures" / "demo"

# Extracted-text token target per section (see module docstring and _size_sections).
# The production chunker uses estimate_tokens == ceil(chars * 0.25), and
# extraction keeps a ~1.00 source/extracted ratio, so these are sized from the
# source paragraphs directly. A chunk only closes when the running total would
# exceed the 512 budget, so a short section lets the next page's first unit —
# heading + the next section's golden sentence fused together (~35-45 tokens) —
# be absorbed under the previous section's label. Pinning every section close to
# the budget forces a clean break. Budget is 512; extraction runs ~3 tokens below
# the source estimate, so TARGET 498 leaves ~11 tokens of slack against a split.
PAD_FLOOR = 486
PAD_TARGET = 498

# Short, plausible closing sentences used for the final fine adjustment so a
# section lands within PAD_FLOOR..TARGET without overshooting the 512 budget
# (coarse pool sentences alone overshoot by their full length).
PAD_SHORT: list[str] = [
    "The owning team answers questions about this section.",
    "Revision notes are shared on the relevant internal channel.",
    "This section is reviewed at least once a year.",
    "Edge cases are resolved by the owning team.",
    "A request to change this section goes to the owning team.",
    "Guidance beyond this section links through the internal wiki.",
]

# (document title -> [(section title, [paragraphs...]), ...]). The first
# sentence of each section's first paragraph is its golden fact.
DOCUMENTS: dict[str, list[tuple[str, list[str]]]] = {
    "Employee Handbook": [
        (
            "Working Hours",
            [
                "Regular working hours at Lumina run from Monday to Friday with flexible start times between 8:00 and 10:00 and a fixed core block from 10:00 to 16:00.",
                "The flexibility exists so people can align work with school runs, commutes, and different timezones, but the core block guarantees that colleagues are mutually reachable.",
                "Managers agree individual schedules in Compass, and any regular deviation from the core block needs written approval from the people team.",
                "Total scheduled time stays at 37.5 hours a week, and lunch breaks are not counted inside that total.",
                "Overtime is tracked per project and is compensated or taken as flexible time only when pre-approved by the line manager.",
                "The office is staffed and supported every weekday, and the lights, café, and meeting rooms are available from 7:30 in the morning.",
                "Anyone who works past 18:00 is expected to take a longer break the next morning, because sustained late nights are not a company expectation.",
                "The core block is the official collaboration window: stand-ups, design sessions, and demos are scheduled inside it.",
                "Meetings default to the shared calendar when all participants are inside the core block, so nobody is pulled outside their flexible window.",
                "Time tracking in Compass records the agreed schedule, and the people team reviews schedule outliers each quarter.",
                "The company observes the same working-hours spirit when travelling: flight times and meeting days follow the local core block of the hosting office.",
                "A manager who needs a fixed earlier start for a role records the requirement in the job posting before hiring for it.",
                "Working hours questions are answered by the people team within two working days on the internal handbook channel.",
                "The handbook team refreshes this section annually and publishes the change note on the internal channel.",
            ],
        ),
        (
            "Leave Allowance",
            [
                "Employees accrue 22 days of paid vacation each calendar year, and the balance becomes usable after the first 90 days of employment.",
                "Accrual runs on the calendar year and is visible in the Compass leave page, with the balance updating automatically after each payroll run.",
                "Sick leave of up to ten days is separate from the vacation balance and does not require a doctor's note for the first three days.",
                "Public holidays are additional to the annual accrual and are listed on the company calendar published each January.",
                "Unused vacation rolls over up to five days into the following year, and anything beyond that is lost unless finance approves an exception.",
                "New parents receive sixteen weeks of fully paid leave, and the partner loses none of their own vacation entitlement over that period.",
                "Bereavement leave of five days is available for a close family member, on top of the annual accrual, and compassionate leave for other cases is agreed with the people team.",
                "The vacation balance accrues at a constant rate across the year, so a new hire does not wait until January for a usable entitlement.",
                "An employee who starts in July still accrues the full-time equivalent of 22 days, prorated from their start month.",
                "Compressed weeks and part-time roles accrue pro rata, and the Compass page shows the prorated figure automatically.",
                "A colleague who becomes ill mid-year keeps the vacation days already accrued and can carry them under the rollover rule.",
                "Vacation is not a substitute for notice: a fixed contract end date is agreed before the leave is booked in Compass.",
                "The process for booking vacation spans is described in the HR policy, together with notice periods for long absences.",
                "The leave balance is reconciled with the payroll system each month so the two pages never disagree.",
            ],
        ),
        (
            "Remote Work",
            [
                "Remote work is open to any role that does not require physical presence, and remote employees keep the same core hours from 10:00 to 16:00 in their local timezone.",
                "A remote work arrangement is agreed with the line manager and recorded in Compass before the first remote day is taken.",
                "Roles that must be on-site include hardware lab ownership, physical security duty, and the facilities rota, and those exclusions are documented in the role profile.",
                "Equipment issued for remote work is shipped by the company and remains company property for the duration of employment.",
                "A reliable internet connection and a quiet workspace are the employee's responsibility, and the IT team assists with connectivity troubleshooting.",
                "Quarterly team gatherings in the London office are expected, with travel and accommodation covered by the company.",
                "The remote policy is reviewed once a year against engagement data, and changes are announced at least two months before they take effect.",
                "A remote employee sets their timezone in Compass, and the calendar shows 10:00 to 16:00 in that timezone as the shared window.",
                "Remote onboarding uses the same day-one kit, with a pre-configured laptop shipped so the new hire is online by their first stand-up.",
                "The office-based counterpart of a remote teammate keeps video on in shared meetings, which is a courtesy, not a rule.",
                "Remote approval cannot be declined just because a role was advertised as office-based; the written role profile is the reference.",
                "Any role whose duties include handling customer hardware on-site cannot be remote, and that is stated in the profile before offer.",
                "The internal knowledge base collects remote-work practicalities, from microphone kits to the best-local-time etiquette guide.",
            ],
        ),
        (
            "Conduct and Confidentiality",
            [
                "All employees must protect company confidential information, which remains Lumina property during and after employment.",
                "Confidential information includes customer lists, financial results, source code, pricing, and anything a third party shares under an agreement.",
                "Harassment or discrimination of any kind is a serious breach and leads to disciplinary action up to and including termination.",
                "Secondary employment requires written approval and must not compete with Lumina's business or use Lumina resources.",
                "Company accounts, documents, and equipment are granted for work purposes and must be returned on the last working day.",
                "Social media posts that reveal internal information or speak for the company require prior sign-off from marketing.",
                "A confidentiality obligation survives the end of employment, as described in the joining agreement every employee signs.",
                "Confidential information is stored in company systems, not in personal accounts, and personal devices do not hold work data.",
                "The annual security training includes a short confidentiality quiz with the examples from this handbook.",
                "A suspected confidentiality breach is reported through the DLP report button, and the reporter stays anonymous.",
                "Gifts and hospitality above a modest value are registerable in Compass, and the conduct section of this handbook covers the bar.",
                "An employee who reports a concern in good faith is protected by the whistleblower rule described in the HR policy.",
                "The conduct policy is the same for office and remote employees, and there is no separate rulebook by location.",
                "Contractors and visitors follow the same confidentiality expectations, which are signed at site entry.",
            ],
        ),
        (
            "Performance Reviews",
            [
                "Formal performance reviews happen twice a year, in the last full weeks of June and December.",
                "A review covers the previous half-year against the role's expectations and sets one or two development goals for the next.",
                "Reviews are written by the line manager in Compass, discussed live, and then signed off by both sides within a week.",
                "Peer feedback is collected anonymously from three collaborators before each review and is shared verbatim in the meeting.",
                "Compensation adjustments decided in a review take effect at the start of the following month.",
                "A probation review is held around day 45 and again at day 85, and it uses the same review form as the regular cycle.",
                "A rising star review is a five-way conversation between the employee, the manager, the people team, and a senior sponsor.",
                "Anyone can request an out-of-cycle review when their role or scope changes materially between cycles.",
                "The review form asks for facts and examples, and the manager is expected to reference evidence rather than impressions.",
                "Each review produces a short development plan with a named owner and a review date in the next cycle.",
                "The people team samples completed reviews to keep calibration balanced across teams.",
                "A review that changes compensation is confirmed in writing by the people team within ten working days.",
                "The review calendar and the list of due forms are visible to every employee in the Compass reviews page.",
                "Preparation time of one hour is protected for the employee before the review meeting.",
            ],
        ),
    ],
    "Product Documentation": [
        (
            "Platform Overview",
            [
                "Lumina Analytics is a governed data analytics platform that ingests raw business data, models it into a consistent schema, and serves governed dashboards through a single web application.",
                "The platform is delivered as software as a service in the customer's chosen region, with no customer-managed servers anywhere in the stack.",
                "Workspaces isolate a business unit's data, models, and dashboards from every other workspace in the same organization.",
                "Administrators control who sees which dashboards through roles that are resolved at request time, never baked into cached pages.",
                "Every action, from a model edit to a dashboard share, is written to an immutable audit log that the customer can export.",
                "The ingestion layer applies schema validation before data lands in the warehouse, so bad rows never reach a dashboard.",
                "Row-level security is attached to dimensions, and it is enforced inside the query engine rather than by hiding panels.",
                "The platform exposes the full product through an API, so dashboards and alerts can be scripted by the customer's own engineering team.",
                "Administrators can spin up a development workspace that shares the same connectors but stays isolated from production queries.",
                "The query engine cost-aware planner explains slow dashboards with a visual plan diagram in the workspace.",
                "The governed dictionary is the source of truth for metric names, and dashboards pick from it instead of free text.",
                "All product documentation lives in the help centre, and each help page links to the underlying API reference.",
                "The audit log covers the internal UI and the API alike, so no action path hides a customer action from governance.",
                "A tenant can be handed a full inventory of its data, models, and dashboards from the workspace settings page.",
            ],
        ),
        (
            "Data Connectors",
            [
                "Lumina ships more than forty built-in data connectors covering databases, warehouses, and file sources such as Snowflake, BigQuery, PostgreSQL, and S3.",
                "Each connector has a documented capability matrix that lists which sync types, credential methods, and data types it supports.",
                "Connectors trigger incremental syncs on a configurable schedule, and the sync status of every source is visible on the sources page.",
                "A failed sync retries three times with backoff, then raises a visible alert and stops silently overwriting good data.",
                "File-based sources accept CSV and Excel uploads, while warehouse connectors reuse the customer's own read-only credentials.",
                "Connector credentials are stored in a separate secrets manager and are never visible in logs or the web application.",
                "A custom connector SDK lets customers add private sources that behave exactly like the built-in ones on the sources page.",
                "The connectors team publishes sync performance benchmarks for every source so customers can size their ingestion windows.",
                "A warehouse connector profiles a sample of the tables on first connect to guess types, and the administrator confirms the mapping.",
                "The sources page shows the last synced row counts and a freshness indicator per table, which surfaces stale feeds early.",
                "Connector versions are pinned per source, so a connector upgrade in the product does not silently change an existing sync.",
                "Customers can pause a source for a holiday window and bring it back with a manual catch-up run the same day.",
                "The connector catalogue is searchable by database vendor, cloud provider, and file format from the help centre.",
                "Connector health, in rows synced and last error, is part of the workspace operations page.",
            ],
        ),
        (
            "Dashboards and Reports",
            [
                "Dashboards are composed of drag-and-drop panels and can be scheduled to email a PDF report to any distribution list.",
                "Every panel carries a definition query, and the query editor validates the query against the workspace model before a panel is saved.",
                "Panel queries are versioned, and the version is stamped on the dashboard definition so a revert is always possible.",
                "Dashboards can be embedded in the customer's own application and rendered behind the customer's single sign-on.",
                "Rendering limits keep a single dashboard to sixty panels and a single page request to two hundred rows, computed server-side.",
                "Scheduled reports support a target folder, a send window, and a list of subscribers, and every delivery is recorded.",
                "A dashboard audit view shows who viewed what and when, which is used by administrators for compliance.",
                "The export engine renders reports as PDF, PNG, and CSV, with the CSV export honouring the same row-level permissions the dashboard uses.",
                "Dashboard layout is responsive, and a published view renders readably on a phone for the morning executive report.",
                "A panel can be shared as a private link that expires, which is how teams hand off a metric without granting access.",
                "The dashboard language follows the workspace locale, and scheduled reports observe the subscriber's timezone.",
                "Version history keeps the last fifty states of a dashboard, and any of them can be promoted back to live.",
                "Panel parameters, like a date range or a store filter, are user-facing and safe, so customers can be handed the URL directly.",
                "Report delivery failures retry twice and then notify the subscriber list owner in the workspace.",
            ],
        ),
        (
            "Pulse Alerting",
            [
                "Pulse is the alerting module that watches any measured metric and notifies Slack or email when a threshold is crossed for the configured duration.",
                "Alerts evaluate on a schedule from fifteen seconds to one day, and evaluation is independent of dashboard refreshes.",
                "Alerts support severity levels and can be paused during planned campaigns to avoid noise from knowingly expected changes.",
                "Downtime and notification history is kept on the alert's detail page for thirty days.",
                "A Pulse alert can fan out to Slack, Microsoft Teams, email, or a webhook that the customer points at their own on-call tool.",
                "Thresholds can hold for a minimum duration, so a one-minute spike does not page anyone when the alert requires ten minutes.",
                "Each alert rule is owned by a named contact, and ownership is shown in the alert list and in the digest that goes to administrators.",
                "Alert-based metrics feed back into dashboards, which is how the research team measured the retention effect of early Pulse adoption.",
                "Compound rules combine conditions, like revenue below target while support tickets spike, and evaluate them as a single alert.",
                "A Pulse alert renders its metric sparkline in the notification itself, which helps the on-call engineer triage before opening the app.",
                "Alert silencing by schedule supports the quiet hours that are part of the customer's own service contract.",
                "Delivery failures, like an unreachable webhook, bounce to the alert owner within one hour so a downstream page is not missed.",
                "The Pulse audit trail is part of the workspace audit log, and alert definitions can be exported with the workspace.",
                "An alert can be tested with a dry run that evaluates the current metric without sending a notification.",
            ],
        ),
        (
            "Pricing Tiers",
            [
                "The Team plan is 49 dollars per member per month and the Business plan is 199 dollars per member per month, with a 15 percent discount when billed annually.",
                "A member is an individual account with dashboard access, and the member count is billed on the peak of the month.",
                "The free tier covers one workspace and three dashboards for evaluation, with community support and no alerting.",
                "The Team plan adds unlimited dashboards, PDF reports, and the standard support channel with a one-business-day response.",
                "The Business plan adds Pulse alerting, private embedded dashboards, the higher rate limit, and priority support.",
                "Enterprise pricing is custom and includes dedicated infrastructure, phone support, and a named success engineer.",
                "Downgrades apply at the next billing period, while upgrades apply immediately and are prorated to the renewal date.",
                "Prices exclude VAT and local taxes, which are added at the rate of the customer's registered address where required.",
                "Billing is per workspace, so an organization can mix a free workspace for experiments with a Business workspace for operations.",
                "The annual discount is applied as a line item on the invoice, and the customer sees the monthly equivalent in the plan card.",
                "Professional services, like schema modelling workshops, are quoted separately and appear on their own invoice line.",
                "A workspace can be trialled on Business for fourteen days without a card, and the trial converts with one click.",
                "The pricing page documents every tier in a comparison table, and plan changes are effective within one billing cycle.",
                "Invoice history and plan receipts are downloadable from the billing page at any time.",
            ],
        ),
    ],
    "Engineering Handbook": [
        (
            "Coding Standards",
            [
                "All production code must pass linting, type checking, and the formatting rules configured in the shared engineering repository.",
                "The shared repository holds one toolchain per language, and a new toolchain lands there before any team adopts it.",
                "Sensitive values must never be logged, and secret material must be read from the vault at runtime rather than committed.",
                "Every repository pins its language toolchain so builds are reproducible between the developer machine and CI.",
                "Public identifiers use the naming style the repository linter enforces, and exceptions are approved in a comment.",
                "Features are shipped behind a feature flag with a named owner, and flags are removed within two sprints of full rollout.",
                "Comments explain why a decision was made, and the code itself states what it does, so the two do not repeat each other.",
                "Generated code, vendored dependencies, and contracts are clearly marked so they are never edited by hand.",
                "Error paths return typed errors, and a function that can fail documents its failure modes next to its signature.",
                "The linter runs pre-commit and in CI, and a formatting disagreement is settled by the formatter, not by debate.",
                "Monorepo boundaries keep shared packages versioned, and a change at that layer goes through the owning team's review.",
                "The style guide is a short list of habits, like naming by intention and keeping functions under a screen of height.",
                "Readme files state the run, test, and deploy commands so a new engineer is not blocked on tribal knowledge.",
                "A reviewer can block a merge on style grounds only when the change violates the pinned toolchain rules.",
            ],
        ),
        (
            "Tests and Coverage",
            [
                "New code is merged only when the diff keeps overall test coverage at or above eighty percent and the pull request adds tests for any new behaviour.",
                "Coverage is measured per repository on the merged branch, and the gate runs on the pull request, not on the mainline.",
                "Critical paths that handle money movement, authentication, or data deletion require unit and integration tests regardless of coverage arithmetic.",
                "A unit test exercises one function in isolation, while an integration test boots the real service with its real database.",
                "Flaky tests are treated as a defect and are removed or fixed within the same working week they are observed.",
                "A test that asserts the same thing three times is consolidated, because the coverage gate should not be paid in meaningless rows.",
                "Most repositories aim above the gate, and teams publish their coverage trend on the engineering dashboard each month.",
                "The coverage gate measures line coverage from the same instrumentation the CI pipeline uses for its build report.",
                "Property based tests are encouraged for parsers and serialisers, where table based cases miss boundary conditions.",
                "A pull request that drops coverage must explain the trade-off in the description, and a reviewer can reopen it on that basis.",
                "Tests run against the checked-in fixture data, and nothing in the suite depends on a live third-party endpoint.",
                "The integration suite builds a throwaway schema and tears it down, so a local run never touches a shared database.",
                "A benchmark that degrades more than ten percent blocks the merge alongside the coverage gate.",
                "Mutation testing runs nightly on the most critical packages and reports the escaped mutants to the owning team.",
            ],
        ),
        (
            "Code Review and CI",
            [
                "Every pull request requires at least one approving review from a code owner and a green continuous integration run before it can merge.",
                "Reviews must be addressed in the same pull request, and rebasing is preferred over squash merges to preserve the review history.",
                "CI runs the unit suite, the integration suite, and the deployment artifact build in parallel to keep the feedback loop under twenty minutes.",
                "A pull request is on the author for the review lifecycle, which means the author resolves comments and re-requests review explicitly.",
                "Reviewers read for correctness, security, and maintainability, and they leave a comment for anything that would block shipping.",
                "Silent approvals are discouraged: a meaningful review either states what changed and why, or asks for the change to be reworked.",
                "Security scanning runs on every dependency change and flags any new vulnerability at the version the code actually ships.",
                "An emergency hotfix can merge with a single approval and a post-incident review, reported in the next engineering retrospective.",
                "The pull request template asks for the change summary, the test plan, and a manual verification screenshot where relevant.",
                "Noise is controlled: trivial changes carry the label chore and can be reviewed by a teammate, not a code owner.",
                "CI caches dependencies between runs, so an unchanged lockfile keeps the feedback loop at the advertised twenty minutes.",
                "A reviewer who stops reviewing a draft states that explicitly, and the draft author is notified to re-request when ready.",
                "The merge button stays disabled until the CI checks are green, and the repository settings enforce that at the platform level.",
                "A long-lived branch older than three weeks is rebased before the final review to keep the diff reviewable.",
            ],
        ),
        (
            "Deployment",
            [
                "Nightly releases deploy to staging automatically and production deployments ship through a blue-green strategy that keeps the previous version live as an instant rollback.",
                "A production deploy is never initiated on a Friday after 15:00, and holiday freezes are announced at least two weeks in advance.",
                "Deploys are verified against the synthetic health checks before traffic is shifted, and the shift is gradual across the shared router.",
                "Each deploy carries a changelog built from the merged pull requests, and the changelog is posted to the internal release channel.",
                "The deploy pipeline builds one immutable artifact per commit and promotes the same artifact from staging to production.",
                "Database migrations run forward-only, are exercised on staging first, and are reversible during the deploy window only.",
                "A failed deploy rolls the router back to the previous blue version within three minutes while the incident team triages the new one.",
                "Deploy metrics, including duration and rollback count, feed the reliability review every quarter.",
                "Staging mirrors production configuration, including feature flags, so a flag misconfiguration cannot hide until the final deploy.",
                "A rollback of code does not roll back data, which is why forward-only migrations are a hard rule in this handbook.",
                "Deployment credentials are scoped to the pipeline, and no engineer holds a standing deploy password individually.",
                "The deploy ticket is the live record of what shipped, and it links the changelog, the health checks, and the rollback decision.",
                "Every Thursday the on-call engineer reviews the rollback playbook for the services they answer for.",
                "Deploy windows are published on the internal calendar so a product change is not scheduled against a migration.",
            ],
        ),
        (
            "On-Call Rotation",
            [
                "Every engineer above a tenure of six months participates in the on-call rotation, which runs one week at a time and is covered by a named primary and secondary.",
                "Pages are routed from the monitoring stack to PagerDuty, and the primary must acknowledge a page within fifteen minutes.",
                "The secondary shadows the primary for the week and takes over only when the primary cannot respond or explicitly hands over.",
                "Severity one pages trigger an immediate bridge; severity two pages are triaged in the channel and can wait until morning.",
                "Every page has a postmortem when a service was left degraded, with an owner and a follow-up issue assigned.",
                "Handover happens on Monday at 10:00 with the previous primary, and the outgoing pair writes a short summary into the on-call channel.",
                "Compensation for on-call weeks follows the financial policy's shift allowance schedule.",
                "No engineer is on call during a booked vacation, and the rota planner protects that automatically from Calendly data.",
                "The on-call runbook lives in the internal knowledge base, one page per service, and is updated after every incident.",
                "An engineer who is paged overnight follows the escalation ladder: primary, secondary, then the engineering director.",
                "The alert budget is a weekly metric, and a noisy alert is tuned or deleted rather than muted silently.",
                "New joiners pair with the primary during their first rotation before taking the week alone.",
                "The rotation calendar is published a quarter ahead, so swapping a week is possible without breaking cover.",
                "An incident wrap-up is posted to the engineering channel within one day of the page being closed.",
            ],
        ),
    ],
    "Security Policy": [
        (
            "Access and Authentication",
            [
                "Multi-factor authentication is mandatory on every Lumina account, and passwords expire every ninety days.",
                "The second factor can be an authenticator app, a hardware key, or a recovery code, and SMS is reserved for recovery only.",
                "Administrator accounts that can change production infrastructure require a separate approval step for each session.",
                "Service accounts must use short-lived credentials from the secrets manager rather than long-lived static keys where possible.",
                "Access reviews are run quarterly and revoke stale access for anyone who left the organization or changed roles.",
                "The identity provider logs every successful and failed sign-in, and the security team scans the log for anomalies nightly.",
                "A terminated employee's access is cut within two hours, and the IT team confirms the cut with a checklist that day.",
                "Password managers are the standard, and engineers are asked never to reuse a work password on personal services.",
                "Guest access for a contractor is time-boxed to the engagement window and cannot be extended by the contractor themselves.",
                "Administrator sessions on the identity provider expire after one hour of inactivity, which is shortest for the most privileged role.",
                "A second factor that is lost or replaced is re-enrolled by the identity team over a verified channel.",
                "The quarterly access review is owned by each department head and evidences the list of people with production access.",
                "Failed sign-in rate is reported on the security dashboard, and a pattern triggers a targeted reset for the affected account.",
                "New infrastructure permissions are requested through the access catalogue, never granted ad-hoc in chat.",
            ],
        ),
        (
            "Data Encryption",
            [
                "Customer data is encrypted at rest with AES-256 and in transit with TLS 1.2 or newer, and encryption keys rotate at least once a year.",
                "Keys are stored in a hardware-backed key service, and the team that manages them is separate from the team that runs the databases.",
                "Backups are written to a separate encrypted bucket and are never single-copy or exposed to general production credentials.",
                "Encryption is applied before the data layer, so storage, cache, and search tiers never hold plaintext customer content.",
                "TLS certificates are issued automatically and expire after ninety days, and the certificate pipeline is scanned weekly.",
                "Customer data exported for support debugging is masked by default and deleted within seven days of the ticket closing.",
                "Encryption status, including the cipher and key age, is exposed on the customer security report that the trust page publishes.",
                "In-transit protection also covers the internal network between services, which is mTLS on the production mesh.",
                "Key rotation is rehearsed twice a year against a copy of the data, so the documented procedure is proven, not assumed.",
                "Field-level encryption is available for the highest-sensitivity columns and is applied by the customer as an option.",
                "Encryption keys have a recovery member, and no single person can restore the key store alone.",
                "The trust page lists the cipher suites in use and disables the weak ones on a published annual cadence.",
                "Encryption coverage is verified by a quarterly scan that flags any store that still holds plaintext classified fields.",
                "Backup encryption is verified by a quarterly restore drill of the oldest available snapshot.",
            ],
        ),
        (
            "Incident Response",
            [
                "A confirmed P0 security incident must be reported to the security team within one hour, while a confirmed P1 must be reported within twenty-four hours.",
                "A P0 is defined as active data loss or unauthorized access to customer data; a P1 is a suspected but unconfirmed exposure.",
                "The on-call security engineer delivers the first assessment within thirty minutes of the report and a written review by the end of the week.",
                "The reporting channel is the security slack channel and the incident line, both staffed around the clock.",
                "Containment comes before forensics, and anyone who suspects a P0 should disconnect the affected service rather than keep investigating.",
                "Customers are notified of a confirmed exposure within the timeline their contract requires, which is at most 48 hours.",
                "Every incident produces a postmortem with root cause, timeline, and corrective actions, tracked to closure in the security board.",
                "An incident tabletop exercise is run once a quarter to rehearse a different scenario with the full response team.",
                "The severity definitions are written in the runbook and are applied by the reporter, not only by the security team.",
                "A P2, like a misconfiguration with no exposure, is reported by the end of the day and tracked in the daily review.",
                "The incident line is reachable by phone and channel, and the routing test is run monthly to check both paths.",
                "No employee is disciplined for reporting a suspected incident in good faith, even when it turns out to be a false alarm.",
                "The customer-facing status page reflects a confirmed P0 as soon as containment begins, before details are known.",
                "The incident commander is named in the first ten minutes, and the role rotates hourly for long incidents.",
            ],
        ),
        (
            "Vendor Risk",
            [
                "Every third-party vendor that touches customer data must complete a security questionnaire and sign a data processing agreement before onboarding.",
                "Vendors with subprocessors must list them in the agreement, and the list is revalidated at each annual renewal.",
                "Procurement records the vendor review in the vendor register, which security audits once a year.",
                "A vendor's security posture is rescored whenever they publish a material breach or change their data processing terms.",
                "Vendors that process payment card data must additionally attest to the PCI scope they hold.",
                "The onboarding review covers encryption, access control, breach notification, and the geographical location of the subprocessors.",
                "A vendor that fails the questionnaire can start a remediation plan, but production data cannot move until it is closed.",
                "The register lists every vendor, the risk score, the review date, and the owner, and it is exported on request for the annual audit.",
                "The data processing agreement names the categories of data the vendor will hold and the retention period for each.",
                "A vendor renewal is blocked automatically in the register when the annual review is overdue.",
                "Security response time is part of the vendor score, so a vendor with a slow breach-notification record scores down.",
                "A new vendor is suggested by an employee, but the review starts in the register and needs security sign-off before procurement.",
                "The vendor questionnaire is the standard twenty questions used across the industry, so most vendors can short-circuit it.",
                "A vendor that holds personal data of Lumina employees follows the employee privacy notice as well as the register review.",
            ],
        ),
        (
            "Physical Security",
            [
                "Datacenter access is restricted to certified engineers and requires both badge access and a second-factor approval recorded in the access log.",
                "Office badges grant access only to the floors of the badge holder's team, and lost badges are deactivated the same day they are reported.",
                "Servers and network equipment inside the office are housed in locked racks with an asset register kept up to date by the IT team.",
                "Visitors sign in at reception, are met by their host, and wear a clearly marked visitor badge at all times.",
                "Clean desk is expected: unlocked screens, printed customer data, and unsecured keys are a finding in the internal security audit.",
                "The CCTV system covers entrance, reception, and the server room, with footage retained for thirty days and reviewed on request.",
                "Physical security incidents are reported through the same channel as digital ones and are handled by the security team.",
                "The access log is reconciled monthly against the HR directory so badges match people who actually work here.",
                "The server room door alarms after business hours, and the duty engineer is paged through the same PagerDuty flow as the digital alerts.",
                "Shipping and receiving is a single desk with a camera, and packages are logged against the recipient's name before entry.",
                "A contractor working in the office overnight is escorted and cannot stay after the escort leaves.",
                "Laptops are encrypted at the disk level, so a lost device does not expose work data even when the badge rule was followed.",
                "The physical security audit happens twice a year and closes out findings within thirty days.",
                "An asset sweep reconciles the laptop register against the device inventory each quarter.",
            ],
        ),
    ],
    "Customer Policy": [
        (
            "Support Levels",
            [
                "Customers on the Business plan receive a support response within four business hours, while Team customers receive one within one business day.",
                "Support is reached through the help centre, the in-app chat, or email, and every request is tracked with a public ticket id.",
                "Enterprise contracts include a named success engineer and a phone line reserved for priority issues.",
                "Support hours are 8:00 to 19:00 on business days for Team and Business plans, with an on-call rotation outside those hours for Enterprise.",
                "A support response means a human acknowledgement with a first diagnosis, not an automated receipt.",
                "Severity on a support ticket is agreed with the customer and drives the response target, with severity one being an active outage.",
                "Support agents see the full workspace context when a ticket is opened from inside the product.",
                "The support team publishes the average response time by plan each quarter on the customer portal.",
                "A ticket that stalls moves lanes automatically after the response target passes, and the customer is told about the escalation.",
                "Support provides workarounds backed by the product team, and a permanent fix is tracked back to the customer's ticket.",
                "The help centre is the first line for common questions, and its articles are versioned with the product release.",
                "Support tickets can be opened without an account, and a submitted ticket id is the confirmation the customer keeps.",
                "The success engineer for Enterprise reviews every ticket opened in the first month of a subscription.",
                "A customer can reopen a closed ticket within seven days, and it resumes on the same thread.",
            ],
        ),
        (
            "Uptime Guarantee",
            [
                "Lumina guarantees ninety-nine point nine percent monthly uptime for Business and Enterprise plans, excluding scheduled maintenance published seven days in advance.",
                "Measured downtime credits the customer 5 percent of the affected monthly fee for every full hour above the guarantee, capped at 50 percent.",
                "Uptime numbers are published on the status page and are computed from the same probes used for the credit calculation.",
                "The status page reports per-region availability, the current incident, and a seven-day history of outages.",
                "Credit requests are approved automatically when the status page matches the published downtime window.",
                "Scheduled maintenance windows are published seven days in advance and never count against the guarantee.",
                "An incident that affects the platform is broadcast on the status page within ten minutes of confirmation.",
                "The guarantee is measured per calendar month and per region, with the highest offending region driving the credit.",
                "Probes run from multiple vantage points so a regional network issue cannot hide a service outage from the calculation.",
                "The status page exposes an API, and customers commonly point their own monitors at the published incident feed.",
                "A partial degradation, like a slow query path, is logged as degraded even when the site stays up, and the time counts toward the credit.",
                "Credits are applied to the next invoice and are reported on the invoice line items, so the customer can reconcile easily.",
                "Maintenance announcements include the expected duration, and the window starts and ends on the stated times.",
                "The achieved uptime for the trailing three months is displayed at the top of the status page.",
            ],
        ),
        (
            "Data Retention and Export",
            [
                "Customer data is retained for the life of the subscription, backups are kept for fourteen days, and a full export of the customer's data is available at any time from the settings page.",
                "Exports are generated within twenty-four hours and delivered as a protected download link that expires after seven days.",
                "After cancellation, customer data is deleted within thirty days except where the customer asks for a final export.",
                "Deletion covers warehouse tables, caches, backups, and search indexes, and it is verified by a deletion report sent to the administrator.",
                "Backups are encrypted and are restored only for a documented recovery request or a customer-initiated point-in-time restore.",
                "A restore request is acknowledged within one business day and completed within forty-eight hours for the main regions.",
                "Connection logs and usage telemetry are retained for ninety days to support billing and abuse investigation.",
                "The retention schedule, including the backup window and the deletion grace period, is documented on the trust page.",
                "A customer can choose auto-export on cancellation, which schedules the final archive before the deletion window starts.",
                "The export format is JSON with the CSV option for tables, and the export layout is versioned with the API.",
                "An administrator can download an export log that lists every archive created for the workspace.",
                "Point-in-time restore is available on Business and Enterprise and restores data to the exact retained timestamp.",
                "The trust page keeps a changelog of the retention schedule so customers can see when a figure changed.",
                "A deletion is re-verified forty-eight hours after it completes in case a late export was requested.",
            ],
        ),
        (
            "Refunds and Cancellation",
            [
                "Annual subscriptions cancelled within the first thirty days receive a full refund, and monthly plans cancel at the end of the current billing period without penalty.",
                "Refunds are issued to the original payment method within ten business days of the cancellation confirmation.",
                "A cancellation is confirmed in writing with a final invoice date, and the workspace stays readable until that date.",
                "The refund policy covers platform subscription fees only and does not apply to professional services or add-on data packages.",
                "A refund does not erase usage history, but the account is closed after the refund is issued.",
                "Professional services delivered in the first month are invoiced at the agreed rate even when the platform fee is refunded.",
                "Downgrading early in a cycle is prorated rather than refunded, so the customer keeps credits on the cheaper plan.",
                "Disputes about a refund are reviewed by the finance team within five business days of the written request.",
                "A customer who cancels mid-cycle keeps read-only access to exports until the deletion window ends.",
                "Billing questions and cancellation requests both route through the billing portal, which keeps the paper trail in one place.",
                "Annual customers who cancel before the first renewal wish to close the account can request a prorated final invoice instead of a refund.",
                "The cancellation flow asks for a single reason at the final step, and the response is aggregated into the product roadmap.",
                "A refunded account can be re-created with a new subscription, and the new workspace starts fresh with the evaluation limits.",
                "The refund receipt names the plan, the period, and the amount so the customer's records match the invoice line.",
            ],
        ),
        (
            "Acceptable Use",
            [
                "Customers may not use Lumina to store personal health records, cardholder data, or regulated personal data without a signed business associate addendum.",
                "Scraping the platform or bypassing its rate limits is prohibited and can suspend the account without notice.",
                "Questionable use is investigated by the trust and safety team, and the customer is informed of the outcome in writing.",
                "Sending spam, phishing, or malware through the platform, including through webhook subscriptions, is prohibited.",
                "Customers are asked to keep their API tokens secret and to rotate them when a contractor leaves their team.",
                "Exporting data to a competitor of Lumina is not prohibited, but bulk automated scraping of the product is.",
                "The trust and safety team publishes the acceptable use review criteria on the customer portal.",
                "A first violation leads to a written warning and a remediation plan; a repeat violation leads to suspension.",
                "The business associate addendum is available as a document in the trust centre and can be signed electronically.",
                "A customer under investigation is notified of the outcome even when the investigation finds no violation.",
                "Display of the Lumina logo in a customer dashboard requires written permission from marketing.",
                "Accounts shared across an entire company should use the member-based plans rather than shared credentials, per this policy.",
                "The acceptable use criteria are reviewed semi-annually and change notifications are sent to the billing contact.",
                "Suspension is reversible, and the workspace data is intact for the customer's remediation window.",
            ],
        ),
    ],
    "Financial Policy": [
        (
            "Expense Reimbursement",
            [
                "Business expenses must be submitted within thirty days of purchase, and receipts are required for every expense above twenty-five dollars.",
                "Expenses are submitted with a photo of the receipt or a direct feed from the corporate card, whichever applies.",
                "Reimbursements are paid on the next payroll run after the expense is approved, plus no later than thirty days from submission.",
                "A missing receipt for a small expense is flaggable once, and a repeat offender receives a written reminder from finance.",
                "Personal purchases on a business claim are treated as a disciplinary matter, not an administrative error.",
                "Currency conversion uses the rate published on the day of the expense, and the tool records the original amount.",
                "Subscription renewals related to work tools are mapped to the right cost centre when they are submitted.",
                "Expense claims older than ninety days are declined automatically unless the manager writes an exception note.",
                "A receipt photo must show the vendor, the date, and the total, because that is what the audit check re-validates.",
                "Expenses below twenty-five dollars still need the receipt when the same merchant appears three times in a month.",
                "The pending list on the expense page shows the reimbursement status, moving from submitted to approved to paid.",
                "Finance processes reimbursements in a weekly batch on Fridays, and the schedule is published on the finance page.",
                "A claim that is rejected comes back with the reason, and the employee can correct and resubmit it once without escalation.",
                "Meal expenses with the team are pre-approved through the calendar invite, which is attached to the claim.",
            ],
        ),
        (
            "Approvals",
            [
                "Expenses and purchase orders above five hundred dollars require manager approval, and anything above two thousand dollars additionally requires finance sign-off.",
                "Approvals are recorded with a timestamp in Compass, and the approver cannot also be the requester.",
                "An exception to the approval limits needs a commercial director's written sign-off attached to the order.",
                "The manager approval applies at the line-item level, so a single order with several small vendors still trips the threshold once.",
                "Recurring orders, like a SaaS subscription, are approved once and re-validated every twelve months.",
                "Finance auditing samples ten percent of approved orders above five hundred dollars against the corresponding receipts.",
                "A rejected approval returns to the requester with the reason visible, and the requester can revise and resubmit.",
                "Approval workflows are configurable per cost centre and are reviewed by finance each quarter.",
                "The approval email carries the item, the amount, and the requester, so a manager can approve from the phone without opening the tool.",
                "An order that was approved then changed by more than twenty percent goes back through the same approval step.",
                "The approver list per cost centre is visible in Compass, and a change of approver is recorded with a date.",
                "A purchase order above ten thousand dollars goes through the finance committee as well as the money-sign-off layer.",
                "The approval audit log is part of the finance archive and is kept for seven years as the retention schedule requires.",
                "A split order aimed at evading the threshold is flagged by the tool and routed to finance directly.",
            ],
        ),
        (
            "Corporate Cards",
            [
                "Company credit cards are issued to the finance team and to employees with travel-heavy roles, and every card transaction must be reconciled within seven days.",
                "Personal use of a corporate card is prohibited and is treated as a disciplinary matter.",
                "Card limits are set per holder at issue and are raised only through a manager and finance authorisation.",
                "A cardholder who leaves the company surrenders the card to IT on the last working day, and finance closes it that week.",
                "The card feed posts into the expense tool automatically, and the cardholder allocates each transaction to a cost centre.",
                "Cards are set to decline above their limit rather than pre-authorising, which avoids surprise monthly statements.",
                "Foreign transaction fees are refunded to the expense claim automatically when the receipt is attached.",
                "The finance team reviews card activity weekly for unusual patterns, including duplicate charges and out-of-policy merchants.",
                "A card holder travelling internationally flags the travel dates in the tool so the charge pattern is expected.",
                "A submitted card transaction that lacks a business purpose is queried, and a second query with no answer suspends the card.",
                "The corporate card number is never typed into an unknown form, and the zero-use card is the viable fallback for a risky vendor.",
                "Card statements are reconciled to the expense ledger monthly, and unreconciled transactions age-out to the owner's manager.",
                "New card requests are approved by finance within two working days and arrive as a virtual card immediately.",
                "Virtual single-use cards are available for one-off software purchases above the standard merchant risk line.",
            ],
        ),
        (
            "Travel and Expenses",
            [
                "Domestic flights are booked in economy, hotels are capped at two hundred dollars per night, and meals are reimbursed at a per-diem of sixty-five dollars per day.",
                "Flights and hotels for external customers are booked by the sales team in advance and never reimbursed individually.",
                "Long-haul international flights may be booked in premium economy when the journey exceeds eight hours.",
                "The per-diem covers meals only and does not need receipts, while any alcohol within it is the employee's own choice to absorb.",
                "Taxis to and from the airport are reimbursable, but rideshare surcharge fees in peak hours are not.",
                "A travel advance can be requested for trips longer than ten days and is settled against receipts within fifteen days of return.",
                "Team travel is planned at least two weeks ahead when budgets allow, because last-minute fares are a frequent overspend.",
                "The reimbursement for a regional office visit follows the office's own per-diem because local rates can differ.",
                "The hotel cap is per night before taxes, and the booking tool flags any night above the cap before the reservation is made.",
                "A business-class upgrade is paid for personally, and the employee claims the economy fare on the expense form.",
                "Trip insurance is automatic through the booking portal, and an employee never buys a separate policy manually.",
                "The travel desk pre-books transport to a customer site when the meeting is customer-facing and above the one-day mark.",
                "Cancelled travel is rebooked at no cost through the portal, and the original booking is closed the same day.",
                "A trip that combines business and personal days uses the personal-day rule in the travel tool so the split is explicit.",
            ],
        ),
        (
            "Budget Reviews",
            [
                "Department budgets are reviewed quarterly in a finance review cycle and reforecast against actuals every month.",
                "Overspend against a quarterly budget is escalated to the finance business partner before it reaches double digits.",
                "Capital purchases above five thousand dollars follow the procurement process in this policy and require a board approver.",
                "Each department owns a forecast at the cost-centre level, and variances are explained in the monthly business review.",
                "The rolling reforecast covers the next two quarters and resets the planning baseline on the first of the month.",
                "Headcount requests are budgeted in the recruiting cycle and count against the department's people cost in the same quarter.",
                "Finance publishes the budget calendar on the first of January with the review, freeze, and publish dates.",
                "A department that underspends is not automatically losing the money, but unspent licence renewal buffers are flagged.",
                "The finance business partner attends every quarterly review and prepares the variance pack in advance.",
                "A reforecast that grows a department by more than fifteen percent is presented to the board before it is entered.",
                "The budget tool derives the forecast from committed spend, so a renewal notice counts toward the projection automatically.",
                "Unbudgeted spend over one thousand dollars goes through the finance sign-off layer even when the approval workflow passed.",
                "The year-end review closes the books in January and publishes the actuals against the final forecast for every cost centre.",
                "The variance pack shows the top five drivers of any gap, so the discussion concentrates on what changed.",
            ],
        ),
    ],
    "HR Policy": [
        (
            "Benefits and Insurance",
            [
                "Health, dental, and vision coverage starts on the first day of employment, and a life insurance policy of twice the annual salary is provided at no cost to the employee.",
                "Family members can be added to the health plan within sixty days of the qualifying event, which includes a new hire date.",
                "The wellness allowance of five hundred dollars a year can be spent on gym memberships, mental health sessions, or ergonomic equipment.",
                "A pension plan with a company match of up to 5 percent of salary is enrolled automatically after the probation period.",
                "The private health plan is chosen once a year in an open enrolment window announced by the people team.",
                "Income protection covers seventy-five percent of salary from the fourth month of a long-term illness.",
                "Dependants can be declared in Compass by the employee themselves, and the people team validates the list once a year.",
                "An employee market's benefits are localised, so maternity, pension, and insurance terms follow the employment contract's jurisdiction.",
                "The dental and vision cover rides on the same provider as the health plan, so claims go to one portal.",
                "A new parent can extend the health plan to the baby at birth without waiting for the enrolment window.",
                "The wellness allowance is spent through expense claims tagged wellness, and COVID-era telehealth is still a covered claim.",
                "Opting out of the health plan is possible when a partner's coverage is equal, and the opt-out is annual.",
                "The benefits comparison table, with monthly costs and cover limits, is on the internal benefits page.",
                "Benefit changes are announced on the change feed at least thirty days before the enrolment window opens.",
            ],
        ),
        (
            "Leave Process",
            [
                "Leave requests are filed in Compass, requests longer than two weeks must be submitted at least forty-five days in advance, and the team lead approves or denies within five working days.",
                "A leave request can be withdrawn at any time before approval, and approved leave that is unused rolls over up to five days.",
                "Peak periods for approval are the Christmas shutdown and the August holiday period, and overlapping requests there are granted by seniority.",
                "The request form asks for the dates, the type of leave, and a handover note that the requester's cover reads in case of handover.",
                "Approved leave is locked in Compass, and an accidental overlap with a sprint can be appealed to the team lead.",
                "Unplanned sick leave does not go through the request flow: the employee calls in on the morning line and updates Compass the same day.",
                "A manager who denies a request records a reason, and the employee can ask the people team to review the denial.",
                "Leave balances reconcile automatically at the year boundary, and any rollover exceptions are handled by the people team.",
                "A request is shown on the team calendar as soon as it is filed, so overlap is visible before approval.",
                "Compressed-week and part-time leave is prorated by the tool, and the amount shown is what remains usable.",
                "The forty-five day notice applies to the span of working days, and weekends and holidays do not shorten it.",
                "A public holiday inside a leave span is not deducted from the balance, and the tool removes it automatically.",
                "Leave is a right, not a request to be earned, and the approval rule exists only to protect team capacity.",
                "Parental leave is filed through the same Compass flow and routes to the people team for the statutory paperwork.",
            ],
        ),
        (
            "Payroll",
            [
                "Salaries are paid twice each month, on the fifteenth and the last working day of the month, into the bank account recorded in Compass.",
                "Payslips are available in Compass three working days before each pay date.",
                "The first salary payment may be prorated when a start date does not align with a pay date, which is normal and expected.",
                "Salary bands are reviewed once a year in the compensation cycle and adjusted for market at the December review.",
                "Tax and social contributions are deducted at source, and the employee keeps national insurance records in the payroll portal.",
                "A change to the bank account is verified with two-factor authentication and takes effect on the next pay run.",
                "Overpayment is recovered from the next payslip unless a payment plan is agreed with payroll in writing.",
                "Payroll runs on a cut-off schedule, and a change submitted after the cut-off applies to the following run.",
                "A payslip shows gross, deductions, net, and the year-to-date totals for each line.",
                "Bonus payments ride on the regular payslip, and the bonus letter is attached as the payslip note.",
                "Payroll corrections are processed in the next run, and the corrected line carries the word adjusted.",
                "The payroll portal keeps the last twelve months of payslips, and older ones are archived by the payroll provider.",
                "A team that works a holiday that is a local public holiday receives the company holiday policy treatment in the same run.",
                "Salary history is visible only to the payroll team and the employee, never to a line manager.",
            ],
        ),
        (
            "Equity",
            [
                "Granted stock options vest over four years with a one-year cliff and a ten-year exercise window.",
                "Option grants are confirmed in each employee's offer letter and are viewed as compensation rather than a bonus.",
                "A one-year cliff means no options vest before the anniversary, after which one quarter vests monthly-going forward.",
                "Equity statements are issued once a year in the compensation portal and list the granted, vested, and unvested units.",
                "Exercising options requires payment of the exercise price, and the process is detailed in the share plan booklet.",
                "Equity questions are routed to the people team, not to finance, so that one party owns the answer.",
                "Leaving the company keeps the exercise window open for the standard post-employment period in the plan documents.",
                "A refresh grant is considered in the annual compensation review based on tenure and performance.",
                "The option agreement is a personal document and is not assignable, transferred, or gifted while unvested.",
                "Early exercising is possible where the plan allows, and the tax treatment is the employee's own responsibility to review.",
                "The share count on the cap table is visible to option holders in the annual statement against the fully diluted number.",
                "A promotion does not trigger a new grant automatically, but it is a factor considered at the next review.",
                "The exercise price is set at grant and stays fixed through the exercise window, as the plan documents state.",
                "Vesting is tracked electronically, and the statement email confirms the next vesting date after each twelve-month block.",
            ],
        ),
        (
            "Grievance and Discipline",
            [
                "Employees can raise concerns confidentially through the people team, and disciplinary action follows a documented corrective counselling step.",
                "A grievance is acknowledged within two working days and a resolution summary is provided within ten working days.",
                "No reprisal against anyone who raises a concern in good faith is tolerated and is itself a disciplinary matter.",
                "The corrective counselling step is a meeting with the manager and a people partner that results in a written improvement plan.",
                "A written warning is issued only after counselling, and it expires from the record after twelve months.",
                "Gross misconduct, including fraud, theft, and harassment, is investigated separately and can lead to summary dismissal.",
                "The employee may bring a colleague to any disciplinary meeting, and that colleague is not a lawyer by default.",
                "All grievances and outcomes are logged in a register that is reviewed anonymously by the people committee each quarter.",
                "A concern can be raised anonymously through the speaking-up channel, and the reporter is given a reference number.",
                "The improvement plan names the expectations, the support available, and the review dates, and it is signed by both sides.",
                "Suspension during an investigation is paid leave, and the employee is told in writing before it happens.",
                "A disciplinary outcome can be appealed once, and the appeal is heard by a different manager than the decision-maker.",
                "The grievance procedure time limits are suspended when the employee is on sick leave, as the people team records in the case file.",
                "Investigation notes are held confidentially and are shared with the employee as the procedure requires.",
            ],
        ),
    ],
    "API Documentation": [
        (
            "Authentication",
            [
                "Every API request must include a bearer token in the Authorization header, obtained from the account settings page under API tokens.",
                "Tokens are scoped to a workspace and can be restricted to read-only access when they are created.",
                "A revoked token stops working immediately, and audit logs record the token id rather than the secret value.",
                "Tokens expire after the duration chosen at creation, from one day to one year, and the expiry is listed next to the token name.",
                "A workspace can hold up to twenty active tokens, and a token is created with a comment describing its purpose.",
                "The Authorization header uses the standard scheme with a space: the word Bearer followed by a space and the token string.",
                "Employees rotate their API tokens quarterly by policy, and the security review reports stale tokens older than a year.",
                "OAuth flows are available for production integrations and issue scoped access tokens through the account's single sign-on.",
                "Token creation writes an audit entry with the creator, the scope, and the expiry, so a workspace can review its own keys.",
                "A token is shown in full exactly once, at creation, and cannot be recovered from the dashboard afterwards.",
                "The settings page marks tokens with a last-used column, which makes the quarterly rotation audit quick.",
                "An API request made with a valid format but an expired token receives a 401 with a clear error code.",
                "Users can create a short-lived token for a one-off script and delete it in the same session.",
                "The API reference examples carry a placeholder token that is rejected, so a copied snippet cannot leak credentials.",
            ],
        ),
        (
            "Rate Limits",
            [
                "The standard rate limit is sixty requests per minute per token, and Business plans may raise the limit to six hundred requests per minute by opening a support ticket.",
                "Rate limits are applied per token, not per account, so several tokens on the same account multiply the available quota.",
                "The current limit is returned in the X-RateLimit-Limit header on every response.",
                "A request that would exceed the limit is rejected rather than queued, so clients can plan their own backoff.",
                "Bursts are measured across the rolling minute, and the limit resets on a sixty-second sliding window.",
                "The header pair X-RateLimit-Remaining and X-RateLimit-Reset lets clients schedule their calls precisely.",
                "Business plan increases apply within one hour of the ticket being approved, and the change is reflected in the headers.",
                "Long-running exports do not consume the write-limit quota after they are queued, because export is asynchronous.",
                "A write-heavy integration that posts many exports in a burst still fits the budget because the export queue is separate.",
                "Webhook delivery calls do not count against the customer's token quota at all.",
                "The limit documentation shows worked examples for a daily export job and a live dashboard side by side.",
                "Rate limit headers are present on success and error responses alike, so a client can always observe its remaining budget.",
                "A support ticket for a higher limit states the current volume and the planned traffic, which sets the approval expectation.",
                "The limit is enforced on the edge, so a burst is rejected before it reaches the query engine.",
            ],
        ),
        (
            "Core Endpoints",
            [
                "The core endpoints are GET /v1/metrics, POST /v1/export, and GET /v1/dashboards, and every response is returned as JSON under a resource envelope.",
                "POST /v1/export starts an asynchronous export and returns a job id that the client polls with GET /v1/export/{jobId}.",
                "Idempotency is supported on POST endpoints with an Idempotency-Key header, and a replayed key returns the original result.",
                "The resource envelope wraps every response in a data field and keeps pagination metadata in a separate meta field.",
                "List endpoints support cursor-based pagination with the page size passed in a query parameter.",
                "A metric value request accepts a from and a to timestamp, and the API validates that the window is closed, not open-ended.",
                "The dashboards endpoint returns definitions only; panel data is fetched from the interactive query endpoint.",
                "Every endpoint documents its scope in the OpenAPI preview, and a token with read-only scope rejects write calls.",
                "Time series responses chunk by day at the default resolution, and a finer granularity is a query parameter.",
                "The export job poll returns a status of queued, running, or done, and done carries a signed download link.",
                "A repeated POST /v1/export without a key creates a new job; with the same key it returns the previous job.",
                "Deprecated endpoints return a warning header and are removed on the published deprecation calendar.",
                "The OpenAPI page is generated from the same schemas the routes validate against, so the documentation cannot drift from reality.",
                "Every endpoint returns an ETag, and a conditional GET referencing it skips a payload that has not changed.",
            ],
        ),
        (
            "Webhooks",
            [
                "Webhook events are delivered as signed POST requests to the subscribed URL, and each event carries an HMAC-SHA256 signature in the X-Lumina-Signature header.",
                "Subscribers must verify the signature against the shared webhook secret before trusting an event body.",
                "Failed deliveries retry with exponential backoff for up to twenty-four hours before the subscription is paused.",
                "An event is delivered with a unique id, and subscribers should deduplicate on that id because redelivery can occur.",
                "The webhook secret is shown once at creation and can be rotated from the settings page, which breaks in-flight subscribers.",
                "Events cover alerts fired, exports completed, and subscriptions changed, and the catalogue is versioned in the API reference.",
                "The delivery history page shows the last one hundred attempts with status, latency, and the response code.",
                "A paused subscription stops delivery and can be resumed without losing the event type configuration.",
                "The signature is computed over the raw request body and the timestamp header, which stops replay confusion.",
                "A subscriber that responds with a 2xx confirms delivery; any other response starts the retry backoff.",
                "The retry schedule is 1 minute, 5, 30, then hourly to a 24-hour ceiling, and the schedule is printed in the reference.",
                "The webhook endpoint is expected to answer within ten seconds, and a slow subscriber is flagged in delivery history.",
                "Event payloads include the workspace id and the event timestamp in the envelope, so filtering is possible without parsing the body.",
                "A test event can be sent from the webhook page to verify the subscriber before production traffic starts.",
            ],
        ),
        (
            "Error Handling",
            [
                "A request that exceeds the rate limit returns a 429 status code with a Retry-After header, and the caller must honour it before sending the next request.",
                "Missing or invalid authentication returns a 401, and a token without the required scope returns a 403.",
                "Validation failures return 400 with a machine-readable list of field errors, and the API never renders stack traces to the client.",
                "A requested resource that does not exist returns 404 without revealing whether it was deleted or never created.",
                "Internal failures return 500 with a request id that the customer can quote to support, and no sensitive detail in the body.",
                "An endpoint that is temporarily overloaded returns 503 with a Retry-After header that the client should read.",
                "All error bodies follow the same envelope, with a code, a message, and a details list that fields map to.",
                "Documentation maps every status code to a recovery step, so a 409 conflict explains the exact fix the caller should apply.",
                "A conflict indicates the resource changed under the caller's request, and the fix is usually to re-read then retry.",
                "The request id appears in the 5xx body and in the server logs, which shortens every support conversation about errors.",
                "A validation error lists the field path and the expected type, so a merge of a broken payload is fixed without a ticket.",
                "The client error guide is a decision tree from the status code to the retry strategy.",
                "Headers like Retry-After and X-RateLimit-Remaining are documented as machine-readable instructions the client should follow.",
                "A 422 is used for semantic conflicts that pass the schema but violate a rule, such as overlapping alert windows.",
            ],
        ),
    ],
    "Onboarding Guide": [
        (
            "Day One",
            [
                "On day one new hires receive a laptop, an office or remote access kit, and credentials to the systems needed for their role before lunch.",
                "The access kit covers the badge, a hardware token for the vault, and a keyboard and monitor when the role is desk-based.",
                "Day one ends with a thirty-minute meeting with the team and a checklist of the accounts they should have received.",
                "The people team sends a day-one agenda the afternoon before, including the building entry instructions for office-based roles.",
                "The laptop arrives pre-imaged with the standard toolchain, and the first login walks the new hire through the password setup.",
                "A welcome message in the internal channel introduces the new hire by name, role, and first project.",
                "The day-one checklist lives in the internal wiki and is the single place the new hire ticks off each account.",
                "If anything from the access kit is missing, the new hire opens an IT ticket labelled onboarding and it is routed to day-one queue.",
                "A hardware token in the access kit is enrolled the same morning it is received, as the security reminder explains.",
                "The new hire picks their desk or home setup on the first day, and the facilities team confirms the monitor height and chair.",
                "The welcome lunch is booked by the team lead for the first Wednesday, and travel is covered for remote hires.",
                "The first paycheck is followed up by payroll with a personal note confirming the prorated amount is expected.",
                "The day-one agenda reserves forty-five minutes to read the handbook sections that matter for the first week.",
                "The team lead adds the new hire to the delivery calendar on the spot, so the first stand-up appears immediately.",
            ],
        ),
        (
            "Getting Started",
            [
                "All accounts are provisioned through Okta single sign-on, and new hires are asked to enrol a second factor the same day they receive access.",
                "The central staples are Slack, Compass for people operations, the internal wiki, and the source control server.",
                "Passwords are never sent over email; access is granted through the provisioning flow or a secure handover.",
                "Okta is the identity provider for the internal tools and for the customer-facing analytics platform when the role uses it.",
                "The second factor at enrolment is an authenticator app, and the hardware token from the access kit is the backup.",
                "Slack channels are joined from a starter list in the wiki, and the new hire can invite themselves to more.",
                "Compass holds the employee's personal data, leave, expenses, and pay history from that day forward.",
                "The source control server is the single code home, and the wiki explains the repository naming convention.",
                "A single Okta login unlocks the product, the wiki, and the source control server, so the new hire does not juggle credentials.",
                "The starter list of Slack channels is curated by the people team and is reviewed each quarter.",
                "The wiki search index is the fastest way to find most answers, and the handbook channel exists for what search misses.",
                "Email is used for external correspondence and for critical notices, while day-to-day work stays in the chat tools.",
                "The provisioning receipt appears in Compass and lists every account the identity team created, which is the audit copy.",
                "The second-factor enrolment is verified by the identity team before any privileged tool is released.",
            ],
        ),
        (
            "First Two Weeks",
            [
                "The first two weeks include security training, a product demo, and a source control walkthrough delivered by the team lead.",
                "By the end of week two the new hire must complete the security awareness module and the data-handling quiz.",
                "A baseline pull request is created during week one so the first review happens while the context is fresh.",
                "The product demo covers the analytics platform end to end, from a connector to a dashboard alert.",
                "The data-handling quiz is five questions about what can be stored where, and it is graded automatically.",
                "The team lead books a one-hour walking session during week one for the codebase tour.",
                "By the end of week two the new hire has a first assigned issue and knows the sprint board they work from.",
                "The two-week mark is the point where the access checklist in the wiki is reconciled against reality.",
                "The security module covers phishing identification, safe handling of production data, and the DLP report button.",
                "The product demo is given by the customer-facing team and answers the why before the how.",
                "Week two includes a pairing session with a senior engineer on the code path the first issue touches.",
                "The sprint board column rules are part of the wiki tour, so the issue lifecycle is clear by the first stand-up.",
                "At the end of week two the team lead and the new hire review the checklist together over coffee.",
                "The data-handling quiz result is logged in Compass and expires at the annual security refresher.",
            ],
        ),
        (
            "Buddy Program",
            [
                "Every new hire is matched with a buddy within the first week, and the buddy hosts a weekly coffee for the first month.",
                "Buddies are volunteers with at least a year of tenure and receive a training note on what the role involves.",
                "The buddy is not the manager, so onboarding questions about the role can be asked without supervision pressure.",
                "The buddy introduces the new hire to the wider team and to the informal channels that carry real context.",
                "A weekly coffee can be a walk, a virtual hangout, or a lunch, and it is protected time in both calendars.",
                "The buddy checks that the new hire knows who to ask for each kind of help, not that they know all the answers.",
                "The buddy programme is rotated, so the same volunteer is not matched twice in a row, and both sides give feedback.",
                "At the end of the first month the buddy and the new hire close the loop with a short note to the people team.",
                "Buddy matching is volunteer-led and the people team pairs the list against team and timezone on the first week.",
                "The buddy training note covers the first-week common questions, so a first-timer buddy is not guessing.",
                "A buddy can hand over to a team-mate after two weeks when the pairing is not clicking, no questions asked.",
                "The weekly coffee is the protected slot to ask the awkward questions, from meeting etiquette to where the coffee machine is.",
                "The people team sends the pairing calendar automatically, so the first coffee is booked by the system.",
                "A buddy's volunteering counts as service time and is recognised in the annual review calibration.",
            ],
        ),
        (
            "The Thirty Sixty Ninety Plan",
            [
                "The 30-60-90 plan is a three-stage guide: first the new hire learns the platform, then ships a small real change, and finally owns a routine part of the team's work.",
                "The plan lives in the internal wiki and is reviewed in the weekly one-to-one with the team lead.",
                "At day ninety the new hire, the buddy, and the team lead debrief the plan and set the goals for the first review cycle.",
                "Stage one ends when the new hire can walk a visitor through the platform and explain the repository layout.",
                "Stage two ends with the small real change merged to production and visible to a real user.",
                "Stage three means the new hire owns a routine duty, like a weekly report or a rotating triage slot, with help available.",
                "Each stage has a checkable deliverable listed in the wiki, so progress is visible rather than felt.",
                "The plan is a guide, not a gantt chart: any stage can be readjusted in the one-to-one when the reality differs.",
                "The 30-day mark is a written check-in on the wiki, not a meeting, so the new hire can reflect in their own words.",
                "The 60-day mark is a working session where the first production change is reviewed by the team.",
                "The 90-day debrief feeds the first formal review cycle that follows in June or December.",
                "A new hire who is already ahead skips nothing but shortens the time, because every stage still validates a different skill.",
                "The plan document links to the demo recording, the baseline pull request, and the routine ownership template.",
                "The 30-60-90 deliverables are reviewed with the buddy before they are shown to the team lead.",
            ],
        ),
    ],
    "Research Report": [
        (
            "Executive Summary",
            [
                "This report summarises a twelve-month cohort study of twelve hundred customers that Lumina ran through the end of 2025.",
                "The study was commissioned to understand which product behaviours best predict long-term retention.",
                "Three findings stand out: Pulse adoption halves churn, the new panel editor doubles weekly adoption, and the query engine upgrade tripled perceived speed.",
                "The study is the first to use daily telemetry across the full customer base rather than a survey sample.",
                "The headline number is an eighteen percent reduction in churn among accounts that set up Pulse in their first ninety days.",
                "Methodologically the study isolates plan, team size, and scenery, so the retention effect is not explained by who upgraded.",
                "The recommendations call for making Pulse a guided onboarding step and moving feature education into the product.",
                "A follow-up study is proposed for the remaining p95 latency on very large workspaces.",
                "The executive read costs five minutes: the three findings, the headline number, and the two recommendations.",
                "The full evidence, code, and data are linked at the end of the report for the data science team.",
                "A glossary defines churn, weekly-active accounts, and the ninety-day adoption window at the back.",
                "The study sponsor was the growth director, and the analysis was independently reproduced by a second data scientist.",
                "Nothing in the report is a prediction; every figure is an observed measurement from the study window.",
                "The numbers quoted here are repeated in the quarterly review deck so the leadership reads one source.",
            ],
        ),
        (
            "Methodology",
            [
                "The study tracked twelve hundred customer accounts over twelve months, split into two cohorts by the year they adopted Lumina, and measured usage telemetry exported daily to the data warehouse.",
                "Accounts were excluded when they were internal, paused, or missing telemetry for more than a month.",
                "Churn was defined as non-renewal or downgrade to the free tier, and the analysis was run by the data science team independently of product.",
                "The telemetry export captured daily session counts, dashboard opens, alert rules created, and API calls, all anonymous at the account level.",
                "Two cohorts of six hundred accounts each were balanced on plan, region, and team size at the start of the window.",
                "The independent analysis means the findings were not tuned against a product roadmap, which is a deliberate credibility control.",
                "Statistical significance was set at the ninety-five percent confidence level with multiple-comparison correction applied.",
                "All results were reproduced on a held-out subset of two hundred accounts before the report was finalised.",
                "The telemetry pipeline ran nightly and backfilled gaps, so a failed export day did not create a missing week.",
                "Accounts were balanced on the seven variables that previous studies showed predicted churn, from plan to helper count.",
                "The correction applied was the Benjamini-Hochberg procedure, which bounds false discoveries across the finding set.",
                "The data pipeline's source code, the analysis notebook, and the raw aggregates are archived in the research repository.",
                "Anonymity was preserved by storing account ids hashed with a per-study key, discarded after the report was published.",
                "The exclusion criteria were fixed before the first export to keep the analysis pre-registered in spirit.",
            ],
        ),
        (
            "Finding: Retention",
            [
                "The study measured an eighteen percent reduction in customer churn among accounts that adopted Pulse alerting within their first ninety days.",
                "Accounts that set their first three alerts saw the largest effect, and the effect persisted after controlling for plan and team size.",
                "The churn reduction was largest in accounts with more than five dashboards, where the alerting habit signals daily use.",
                "Churn in the Pulse-adopting cohort was 11 percent against 13.4 percent in the matched control, a gap that widened each quarter.",
                "The effect was measured on renewals, not on survey intent, so it survived the gap between what people say and do.",
                "Accounts that adopted Pulse later than ninety days still churned less than never-adopters, but the effect was half the size.",
                "The reduction held in both regions at comparable magnitude, which rules out a one-market fluke.",
                "Product built the retention dashboard the study used, and it is now the weekly metric for the growth team.",
                "The eighteen percent figure margins in the appendix with the standard error, the sample size, and the confidence band.",
                "The control cohort was matched on the same plan mix and onboarding cohort, so the gap cannot be ascribed to a pricing change.",
                "A sensitivity analysis dropped the largest accounts, and the measured reduction moved less than one point.",
                "The dashboard splits churn by whether Pulse was the first alerting product the account ever used.",
                "Growth now treats the ninety-day Pulse window as the key activation metric in its quarterly plan.",
                "The surviving customers in the adopting cohort used Pulse every week, on average, across the window.",
            ],
        ),
        (
            "Finding: Feature Adoption",
            [
                "Dashboards using the new panel editor raised weekly active adoption from thirty-one percent to sixty-four percent over the study window.",
                "The jump was concentrated in the first month after the editor shipped, and it held across cohorts in every region.",
                "Weekly active adoption was measured as accounts that opened at least one dashboard per calendar week.",
                "The editor change alone explains the shift: accounts that kept the previous editor view did not move off thirty-one percent.",
                "The adoption gain translated into more scheduled reports, which is a leading indicator of renewals in earlier cohorts.",
                "Feature education, like the editor hint cards, moved adoption a further five points in new accounts.",
                "The percentage is measured against all active accounts, so the gain is not a denominator trick from churned accounts.",
                "The product team is now experimenting with a similar onboarding sequence for the connector catalogue.",
                "The sixty-four percent figure stayed above sixty for the full six months after the release, so it is not a launch blip.",
                "New accounts adopted the editor twice as fast as existing ones, which shapes the onboarding recommendation.",
                "The five-point education gain was isolated with a randomised hint-card rollout across two hundred new accounts.",
                "Scheduled-report growth lagged the adoption jump by six weeks, which matches the time to build a new reporting habit.",
                "The adoption dashboard is now the standard view the product leadership reviews at the weekly metrics meeting.",
                "The adoption measure excludes accounts that only open the editor and never save a panel.",
            ],
        ),
        (
            "Finding: Performance",
            [
                "Dashboard load times improved two point three times after the query engine upgrade, moving the median from eight point four seconds to three point seven seconds.",
                "The p95 load time improved even more, from forty-one seconds to twelve seconds, removing the largest source of opened-then-closed tabs.",
                "The gains came from query pruning and a shared cache, and no schema changes were required from customers.",
                "Median times were measured on the customer telemetry of the same twelve hundred accounts, before and after the release.",
                "The improvement was stable across dashboard sizes, from small panels to the largest sixty-panel workspaces.",
                "The performance gain removed the top reason customers gave for moving to a spreadsheet in exit surveys.",
                "Latency now sits under the internal budget line of five seconds at the median, with p95 monitored weekly.",
                "The two point three factor is reported as the geometric mean ratio because the median before was not normally distributed.",
                "The cache hit rate is part of the performance dashboard and rose from forty percent to seventy-two percent after the upgrade.",
                "The p95 twelve seconds is driven by the largest multi-warehouse workspaces, which the follow-up study targets.",
                "A rolled-back release window during the study lets the team verify that the improvement did not come from a seasonality effect.",
                "The measurement window spanned both a busy and a quiet quarter, so the factor is not peak-load-dependent on one side.",
                "The internal budget line is documented in the performance engineering wiki alongside the weekly p95 chart.",
                "The median improvement is quoted as two point three times, not as a percentage, to keep the before and after comparison honest.",
            ],
        ),
        (
            "Recommendations",
            [
                "The report recommends making Pulse activation a guided part of onboarding and moving feature education into the product itself.",
                "Specifically, the onboarding flow should prompt a first alert within the first ninety days and surface the speed improvement as a benchmark.",
                "The data science team recommends one follow-up study on what drives the remaining p95 latency for very large workspaces.",
                "Product should rank Pulse activation as a first-week onboarding step, not as a settings page buried in configuration.",
                "The growth team should report churn by first-alert-date so the ninety-day window becomes a visible operational metric.",
                "Feature education should move from the wiki into empty states and hint cards inside the panel editor.",
                "The benchmark reporting should quote the speed factor to new accounts as an adoption lever in the sales deck.",
                "A re-run of the cohort study is scheduled for the end of 2026 to confirm the recommendations hold at a larger scale.",
                "The first-week Pulse prompt should be tracked with a product analytics event so the activation funnel is measurable.",
                "The churn-by-first-alert-date report should be a saved dashboard template that any growth analyst can duplicate.",
                "The sales deck benchmark should quote the median, not the factor, to stay within the data-supported claim.",
                "The follow-up latency study should oversample the largest workspaces and report the contributing query patterns.",
                "Each recommendation names an owner team and a review date, and the growth lead owns the combined plan.",
                "A checkpoint review of the recommendations is scheduled four months after the report.",
            ],
        ),
    ],
}

DOC_FILENAMES: dict[str, str] = {
    "Employee Handbook": "Employee Handbook.pdf",
    "Product Documentation": "Product Documentation.pdf",
    "Engineering Handbook": "Engineering Handbook.pdf",
    "Security Policy": "Security Policy.pdf",
    "Customer Policy": "Customer Policy.pdf",
    "Financial Policy": "Financial Policy.pdf",
    "HR Policy": "HR Policy.pdf",
    "API Documentation": "API Documentation.pdf",
    "Onboarding Guide": "Onboarding Guide.pdf",
    "Research Report": "Research Report.pdf",
}

# Realistic closing sentences appended to short sections so every section lands
# in the PAD_FLOOR..budget token window. Sentences are taken in order per
# document (never repeated within a document while the pool lasts).
PAD_POOLS: dict[str, list[str]] = {
    "Employee Handbook": [
        "A question about this section can be raised in the internal handbook channel, and the people team triages it within two working days.",
        "The people team reviews this section once a year and consults the team that the policy affects before changing it.",
        "Suggested updates are logged in the handbook repository and discussed at the monthly people-lead meeting.",
        "Nothing in this section overrides a written agreement signed by the company, and hard cases are resolved by the people team.",
        "The version history of this section is visible in the handbook footer, so everyone can see what changed and when.",
        "Managers are reminded of this section during the quarterly one-to-one preparation notes.",
        "Translated summaries of the handbook are reviewed by the people team so the intended meaning, not the literal phrasing, is what travels.",
    ],
    "Product Documentation": [
        "This behaviour is the default in the current release, and the release notes call out any change to it.",
        "Administrators can verify the behaviour described here in a development workspace before it matters in production.",
        "The product team reviews this section each release and links it to the feature's original specification.",
        "Where the platform behaves differently in a particular region, the region note is shown at the top of the relevant help page.",
        "Comments on this page are collected by the product team and rolled into the next documentation review.",
        "The help centre keeps the previous two versions of this page for customers on older releases.",
        "A demonstration workspace is available from the evaluation template to try this flow without setting up data.",
        "Edge cases are covered in the troubleshooting section linked from every page in this document.",
    ],
    "Engineering Handbook": [
        "The engineering working group owns this section and reviews it at the monthly engineering guild meeting.",
        "A change to this section goes through the same review process as a change to production code.",
        "The automation that watches these rules reports a dashboard on the engineering metrics page each week.",
        "Questions are raised in the engineering channel, and the answer is added back to this page when it is not already there.",
        "The examples in this section are illustrative, and the pinned toolchain is the binding reference for any dispute.",
        "A team that finds a new pattern argues for it here rather than in a repository, so the rule stays searchable.",
        "The owner of this section is named in the page footer, and an issue can be raised against the page directly.",
    ],
    "Security Policy": [
        "The security team owns this section and reviews it with the quarterly access review cycle.",
        "A suggested change is raised as a security request and triaged on the security board before it lands here.",
        "The control described in this section is verified by the annual independent security assessment.",
        "Tooling changes that affect this section are announced on the security changelog at least one month in advance.",
        "Exceptions to this section require a written risk acceptance signed by the security lead.",
        "The evidence for this control is retained in the security evidence store for the audit retention period.",
        "Relevant employees confirm they have read this section in the annual security confirmation in January.",
    ],
    "Customer Policy": [
        "This section is part of the customer-facing terms, and changes are announced on the policy log at least thirty days in advance.",
        "Questions about this section are answered by customer operations from the address published on the support page.",
        "The customer success team walks new customers through this section during onboarding when it applies to their plan.",
        "The policy log keeps the history of this section, and every change names the date it takes effect.",
        "A customer who needs the detail behind a number in this section can request the working document from customer operations.",
        "Automation in the product enforces the promises in this section, so a breach of the promise surfaces as an incident, not as a manual miss.",
        "Regional wording differences are noted inline, and the English version is the binding one for contracting.",
    ],
    "Financial Policy": [
        "The finance team publishes a change log for this policy on the finance page, and line owners are notified of each revision.",
        "A question about this section is routed to the finance partner named on the finance hub page.",
        "The controls in this section are exercised by the finance tooling itself, with the audit trail kept in the finance archive.",
        "A case not covered by this section is resolved by the finance team and then written back into the policy for next time.",
        "This section is reviewed at the quarterly budget review alongside the numbers it governs.",
        "The version history of this policy is retained for seven years as the retention schedule requires.",
        "Approvers are expected to read this section before they approve, and the approval tool links it at the point of signing.",
    ],
    "HR Policy": [
        "The people team owns this section and reviews it whenever a change in law or tooling affects the process.",
        "A question about this section is answered in the people team channel, and the answer is added here when it is generally useful.",
        "The workflow described here is enforced by Compass, so the recorded steps and the practiced process stay in step.",
        "A deviation from this section is possible only with a written note from the people team attached to the relevant record.",
        "The change history of this section is kept at the document footer, with a summary shared on the internal feed.",
        "Employment contracts reference this section by name, so the binding document is the one current at the event date.",
        "The annual review of this section is logged against the people team's compliance calendar.",
    ],
    "API Documentation": [
        "This endpoint behaviour is stable in the current API version, and a breaking change would be announced on the deprecation calendar.",
        "The reference implementation for this behaviour ships in the SDKs, which are regenerated from the same schemas.",
        "A question about this behaviour reaches the API team through the developer forum, and the answer is folded back into this page.",
        "The OpenAPI preview renders this page's examples, so the two can never disagree about a shape.",
        "This behaviour is exercised by the public contract tests, which run on every release candidate.",
        "A developer who needs an exception to this behaviour opens a support ticket and names the workspace.",
        "Rate and quota numbers in this section are live values from the current pricing page, kept in sync by the same tooling.",
    ],
    "Onboarding Guide": [
        "The onboarding checklist in the wiki mirrors this section and is the single place the new hire tracks completion.",
        "The people team updates this section ahead of every onboarding cohort, so it reflects the current tooling.",
        "A missing step here is a feedback item to the people team, and the guide is amended before the next cohort.",
        "The owners named in this section are the people to ask when something is not available on the day.",
        "A short feedback survey closes on day thirty and feeds the next quarterly review of this guide.",
        "The onboarding team reviews time-to-completion for each step in this section every quarter.",
        "The guide is written for a first-week reader, and deeper answers link out to the relevant policy instead of being duplicated.",
    ],
    "Research Report": [
        "The appendix reproduces every figure in this section with its confidence interval and sample size.",
        "The data and code behind this section are archived in the research repository and linked from the report page.",
        "A reviewer should read this section alongside the methodology section, because definitions travel with the numbers.",
        "The analysis team will re-run this section's figures on request for any customer who partners with the research program.",
        "Figures in this section are rounded to one decimal place, and the appendix keeps the exact values.",
        "A change to the underlying telemetry definitions would be flagged in the methodology section before it affects these results.",
        "This section was reviewed by the study's independent reviewer, whose comments are archived with the report.",
    ],
}


def _estimate_tokens(text: str) -> int:
    """Same deterministic 0.25 chars/token math as extraction/chunking."""
    return max(1, math.ceil(len(text) * 0.25))


def _size_sections(title: str, sections) -> list[tuple[str, list[str]]]:
    """Pad short sections so the default chunker emits one chunk per section.

    The paragraph chunker opens a new chunk only when the running token count
    would exceed the 512 budget, ignoring page boundaries. A section that is
    too short therefore lets its own chunk swallow the head of the next
    section, where the next section's golden sentence lives — and the citation
    then points at the previous section. Padding every section up to
    PAD_FLOOR..budget closes that headroom. Extraction keeps a ~1.00
    source/extracted token ratio, so the source estimate is the right knob.
    """
    pool = PAD_POOLS[title]
    pointer = 0
    short = 0
    padded: list[tuple[str, list[str]]] = []
    for index, (name, paragraphs) in enumerate(sections):
        paragraphs = list(paragraphs)
        head = title if index == 0 else ""
        tokens = _estimate_tokens("\n".join(filter(None, [head, name])) + "\n" + "\n\n".join(paragraphs))
        while tokens < PAD_FLOOR:
            if PAD_TARGET - tokens >= 22:
                extra = pool[pointer % len(pool)]
                pointer += 1
            else:
                extra = PAD_SHORT[short % len(PAD_SHORT)]
                short += 1
            paragraphs[-1] = f"{paragraphs[-1]} {extra}"
            tokens = _estimate_tokens("\n".join(filter(None, [head, name])) + "\n" + "\n\n".join(paragraphs))
        padded.append((name, paragraphs))
    return padded


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
    """Rebuild /Outlines with correct page numbers (see the same function in
    evaluation/build_corpus.py for why this exists)."""
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


def _doc(title: str, sections, filename: str) -> None:
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


def build_all() -> list[Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for title, filename in DOC_FILENAMES.items():
        _doc(title, _size_sections(title, DOCUMENTS[title]), filename)
        paths.append(OUT_DIR / filename)
    return paths


def main() -> None:
    for path in build_all():
        print("wrote", path)


if __name__ == "__main__":
    main()