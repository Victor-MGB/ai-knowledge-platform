# KnowFlow screenshots

This page explains what to capture and how, so the README's media sections
stay honest and reproducible.

## What to capture

| # | Shot | Where | What it proves |
|---|------|-------|----------------|
| 1 | **Login → upload** | `http://localhost` (register a tenant, upload the 10 PDFs from the demo corpus) | the product boots from one command and accepts documents |
| 2 | **Document list** | `/documents` — show a spread of `READY` docs with page/chunk counts | ingestion completes asynchronously and flips to READY |
| 3 | **Search results** | `/chat` (retrieval) — a query whose answer lists its sources | pgvector retrieval works through the UI |
| 4 | **Cited RAG answer** | `/chat` or the RAG panel — a question + the cited source chunk | answers are grounded, citations resolve to doc + section + page |
| 5 | **Refusal** | `/chat` with the **Refuse weak matches** floor checked, then an out-of-corpus question | honest `"I don't know."` guardrail, not a hallucination |
| 6 | **RAG monitor** | Grafana `http://localhost:3001` — ingestion + RAG dashboard | production-shaped observability |
| 7 | **Swagger/OpenAPI** | `http://localhost:8000/docs` (ai-service, host-mapped for the shot) | a machine-readable API contract |

## How to capture

- Use [Playwright](https://playwright.dev/) (the committed
  `capture_playwright.py` drives login → upload → search → cited RAG →
  refusal in one pass) or chromium headless (`capture.sh`) for static pages.
- Capture at 1440×900 or wider, PNG.
- Never mock a number: screenshot the real UI against the live compose stack.

### One-pass capture (Playwright)

Playwright is *not* a repo dependency — install it on demand:

```bash
pipx install playwright && playwright install chromium
python docs/screenshots/capture_playwright.py
```

The script registers a fresh tenant through the API, uploads the demo corpus,
waits for every document to land in READY, then drives the UI — including
checking the chat's **Refuse weak matches** floor so shot 5 is an honest
guardrail refusal (`minScore` is documented in `docs/rag.md`). It keeps the
session state in-memory (no credentials are stored) and writes `*.png` here.

> The Swagger shot needs the ai-service reachable on a host port. The compose
> file never exposes `:8000` past the edge (internal routes stay internal), so
> temporarily map it for the capture, e.g.:
>
> ```bash
> printf 'services:\n  ai-service:\n    ports:\n      - "8000:8000"\n' > /tmp/ai-port.yml
> docker compose -f docker-compose.yml -f /tmp/ai-port.yml up -d ai-service
> python docs/screenshots/capture_playwright.py --swagger http://localhost:8000/docs
> docker compose up -d ai-service   # drop the override
> ```

### Quick headless captures (chromium)

```bash
./docs/screenshots/capture.sh          # needs `chromium` on PATH
# products land in docs/screenshots/ as *.png
```

## Wiring into the README

Drop the files as `docs/screenshots/*.png` and reference them from the README
`## Screenshots` section so they render on GitHub (`./docs/screenshots/...`

## Keeping them honest

Re-run the capture every time the UI or the demo corpus changes. Stale
screenshots read as fabricated ones to an evaluator.

## Demo video (optional)

Record a ~3-minute Loom/OBS walkthrough of the same flow, spoken as
"upload → wait for READY → search → ask → read the citation → watch Grafana".
Link it in the README `## Demo video` section.