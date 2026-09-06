"""KnowFlow — one-pass screenshot capture against the live compose stack.

Drives the real app (Playwright): registers a fresh tenant through the API,
uploads the demo corpus, waits for the worker pipeline to land every document
in READY, then logs in through the UI and captures the shots the README
promises. No mocked numbers, no stored credentials — the tenant is created on
the fly and discarded at the end.

Shots produced (1440x900, PNG) in this directory:

    01_login.png        entry point
    02_register.png     tenant signup
    03_documents.png    READY document list (page/chunk counts)
    04_chat.png         cited RAG answer with sources
    05_refusal.png      honest "I don't know" guardrail
    06_grafana.png      provisioned ingestion + RAG dashboard
    07_api_docs.png     ai-service OpenAPI (Swagger)

Prereqs:
  - docker compose up --build  (the full stack on :80)
  - playwright + chromium installed outside the repo:
        pipx install playwright && playwright install chromium

Run:
    python docs/screenshots/capture_playwright.py [--base http://localhost]
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ai-service"))

import httpx  # noqa: E402
from playwright.sync_api import Page, expect  # noqa: E402

from tests.e2e.demo_corpus import DOC_FILENAMES, OUT_DIR  # noqa: E402

BASE = "http://localhost"
PASSWORD = "Sup3rSecret!1"
OUT = Path(__file__).resolve().parent
POLL_INTERVAL_S = 2.0
POLL_TIMEOUT_S = 300.0
SHOT_DELAY_S = 1.2  # let the SPA settle before each screenshot


def register_org(client: httpx.Client) -> tuple[str, str, dict]:
    email = f"caps-{uuid.uuid4().hex[:10]}@example.com"
    org_name = f"portfolio-{uuid.uuid4().hex[:6]}"
    payload = {
        "email": email,
        "password": PASSWORD,
        "organization": org_name,
        "name": "Portfolio Capture",
    }
    response = client.post(f"{BASE}/auth/register", json=payload)
    response.raise_for_status()
    body = response.json()
    creds = {"email": email, "password": PASSWORD, "organization": org_name}
    return body["tokens"]["accessToken"], body["organization"]["id"], creds


def upload_document(client: httpx.Client, token: str, filename: str) -> str:
    with (OUT_DIR / filename).open("rb") as fh:
        response = client.post(
            f"{BASE}/api/v1/documents",
            headers={"authorization": f"Bearer {token}"},
            files={"file": (filename, fh, "application/pdf")},
        )
    response.raise_for_status()
    body = response.json()
    assert body["status"] == "queued", body
    return body["id"]


def wait_all_ready(client: httpx.Client, token: str, document_ids: list[str]) -> None:
    pending = set(document_ids)
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while pending and time.monotonic() < deadline:
        for document_id in tuple(pending):
            response = client.get(
                f"{BASE}/api/v1/documents/{document_id}/status",
                headers={"authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            stage = response.json()["stage"]
            if stage in ("ready", "failed"):
                assert stage == "ready", response.text
                pending.discard(document_id)
        if pending:
            print(f"   {len(document_ids) - len(pending)}/{len(document_ids)} READY...")
            time.sleep(POLL_INTERVAL_S)
    assert not pending, f"timed out waiting for: {pending}"


def login(page: Page, email: str, password: str) -> None:
    page.goto(f"{BASE}/login", wait_until="networkidle")
    page.get_by_placeholder("Email").fill(email)
    page.get_by_placeholder("Password", exact=True).fill(password)
    page.get_by_role("button", name="Sign in").click()
    expect(page).to_have_url(f"{BASE}/")


def ask(page: Page, text: str) -> None:
    page.locator("textarea[placeholder='Ask your knowledge base']").fill(text)
    page.locator("textarea[placeholder='Ask your knowledge base']").press("Enter")


def shot(page: Page, name: str) -> None:
    time.sleep(SHOT_DELAY_S)
    page.screenshot(path=str(OUT / name), full_page=False)
    print(f"   wrote {OUT / name}")


def main() -> int:
    global BASE
    parser = argparse.ArgumentParser(description="Capture KnowFlow screenshots.")
    parser.add_argument("--base", default=BASE, help=f"base URL (default {BASE})")
    parser.add_argument(
        "--swagger",
        default="http://localhost:8000/docs",
        help="ai-service Swagger URL (needs a host-reachable port)",
    )
    args = parser.parse_args()
    BASE = args.base.rstrip("/")

    print(f"== software? creating fresh tenant + corpus @ {BASE}")
    with httpx.Client(timeout=30.0) as client:
        token, org_id, creds = register_org(client)
        print(f"   registered {creds['email']} (org {org_id})")

        document_ids = [
            upload_document(client, token, filename)
            for filename in DOC_FILENAMES.values()
        ]
        print(f"   uploaded {len(document_ids)} documents, awaiting pipeline...")
        wait_all_ready(client, token, document_ids)
        print("   all READY")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.set_default_timeout(30_000)

        page.goto(f"{BASE}/login", wait_until="networkidle")
        shot(page, "01_login.png")

        page.goto(f"{BASE}/register", wait_until="networkidle")
        shot(page, "02_register.png")

        login(page, creds["email"], creds["password"])

        page.goto(f"{BASE}/documents", wait_until="networkidle")
        expect(page.get_by_text("ready", exact=True).first).to_be_visible()
        shot(page, "03_documents.png")

        page.goto(f"{BASE}/chat", wait_until="networkidle")
        ask(
            page,
            "How many paid vacation days do employees accrue per year and "
            "after how long can they use them?",
        )
        expect(page.get_by_text("Sources", exact=True).first).to_be_visible(
            timeout=60_000
        )
        shot(page, "04_chat.png")

        page.get_by_text("Refuse weak matches").click()
        ask(
            page,
            "How should employees claim tax deductions for cryptocurrency "
            "mining at home?",
        )
        expect(page.get_by_text("I don't know", exact=False).first).to_be_visible(
            timeout=60_000
        )
        shot(page, "05_refusal.png")

        page.goto(f"{BASE}/documents", wait_until="networkidle")
        # Grafana needs its own context (separate origin).
        g = browser.new_page(viewport={"width": 1440, "height": 900})
        g.goto("http://localhost:3001/d/knowflow-observability", wait_until="domcontentloaded")
        time.sleep(5)
        g.screenshot(path=str(OUT / "06_grafana.png"), full_page=False)
        print(f"   wrote {OUT / '06_grafana.png'}")
        g.close()

        page.goto(args.swagger, wait_until="domcontentloaded")
        time.sleep(2)
        page.screenshot(path=str(OUT / "07_api_docs.png"), full_page=False)
        print(f"   wrote {OUT / '07_api_docs.png'}")

        browser.close()

    print("done — review the PNGs before committing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())