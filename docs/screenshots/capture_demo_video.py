"""Day 35 — drive the live full-stack demo for a screen recording.

Plays the portfolio narrative through a real, headful Chromium while ffmpeg
grabs the X display (see make_demo_video.sh): app login -> cited RAG answer
with sources -> honest refusal -> a burst of questions -> the provisioned
Grafana dashboard refreshing live -> the Prometheus expression browser with
http_requests_total / rate ticking up as two more questions fire.

Prereqs:
  - full compose stack up (frontend :80, grafana :3001, prometheus :9090)
  - playwright with a headful chromium (the X display must be recordable;
    on a headless box run it under Xvfb :99)
  - optional KNOWFLOW_DEMO_EMAIL / KNOWFLOW_DEMO_PASSWORD to reuse a tenant;
    otherwise a fresh tenant is registered, seeded with the demo corpus and
    awaited READY before the recording timeline starts.

Run (from repo root):
    docs/screenshots/make_demo_video.sh docs/screenshots/live-demo.mp4
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

from docs.screenshots.capture_playwright import (  # noqa: E402
    PASSWORD,
    register_org,
    upload_document,
    wait_all_ready,
)
from tests.e2e.demo_corpus import DOC_FILENAMES  # noqa: E402

BASE = os.environ.get("KNOWFLOW_DEMO_BASE", "http://localhost")
SCREEN_W = 1366
SCREEN_H = 768
STRONG_Q = "How long is the refund window if a customer cancels their annual subscription?"
REFUSAL_Q = "How should employees claim tax deductions for cryptocurrency mining at home?"
EXTRA_QS = [
    "What HTTP status code is returned when a request exceeds the rate limit?",
    "How much notice must an employee give for a leave that is longer than two weeks?",
]
GRAFANA_URL = (
    "http://localhost:3001/d/knowflow-observability/knowflow-service-health"
    "?from=now-5m&to=now&refresh=5s"
)
PROM_URL = (
    "http://localhost:9090/graph"
    "?g0.expr=sum(http_requests_total)%20by%20(job)&g0.tab=1"
    "&g1.expr=sum(rate(http_requests_total%5B1m%5D))%20by%20(job)&g1.tab=1"
)


def dwell(seconds: float) -> None:
    time.sleep(seconds)


def login(page: Page, email: str, password: str) -> None:
    page.goto(f"{BASE}/login", wait_until="networkidle")
    page.get_by_placeholder("Email").press_sequentially(email, delay=18)
    page.get_by_placeholder("Password", exact=True).press_sequentially(password, delay=12)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(f"{BASE}/")
    dwell(1.5)


def ask(page: Page, text: str) -> None:
    box = page.locator("textarea[placeholder='Ask your knowledge base']")
    box.press_sequentially(text, delay=14)
    box.press("Enter")


def await_answer(page: Page, timeout_s: float = 60.0) -> None:
    last = -1
    stalled = 0
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        size = len(page.locator("body").inner_text())
        if size == last:
            stalled += 1
            if stalled >= 3:
                return
        else:
            stalled = 0
            last = size
        dwell(0.4)
    raise TimeoutError("answer did not settle")


def ensure_tenant() -> tuple[str, str]:
    email = os.environ.get("KNOWFLOW_DEMO_EMAIL")
    password = os.environ.get("KNOWFLOW_DEMO_PASSWORD")
    if email and password:
        return email, password
    print("   registering fresh tenant + awaiting the demo corpus READY...")
    with httpx.Client(timeout=30.0) as client:
        token, _org_id, creds = register_org(client)
        document_ids = [
            upload_document(client, token, filename)
            for filename in DOC_FILENAMES.values()
        ]
        wait_all_ready(client, token, document_ids)
    return creds["email"], creds["password"]


def propose() -> None:
    email, password = ensure_tenant()
    print(f"== recording with tenant {email}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                f"--window-position=0,0",
                f"--window-size={SCREEN_W},{SCREEN_H}",
            ],
        )
        context = browser.new_context(viewport={"width": SCREEN_W, "height": SCREEN_H - 40})
        app = context.new_page()
        app.set_default_timeout(30_000)

        login(app, email, password)
        app.goto(f"{BASE}/chat", wait_until="networkidle")
        dwell(2)

        print("   Q1 cited answer")
        ask(app, STRONG_Q)
        await_answer(app)
        expect(app.get_by_text("Sources").first).to_be_visible()
        dwell(3)

        print("   Q2 honest refusal")
        app.get_by_text("Refuse weak matches").click()
        dwell(1)
        ask(app, REFUSAL_Q)
        await_answer(app)
        expect(app.locator("body")).to_contain_text("I don't know")
        dwell(3)

        print("   Q3/Q4 activity burst")
        for question in EXTRA_QS:
            ask(app, question)
            await_answer(app, timeout_s=45)
            dwell(1.5)

        print("   Grafana live dashboard")
        grafana = context.new_page()
        grafana.goto(GRAFANA_URL, wait_until="domcontentloaded")
        dwell(7)
        grafana.mouse.move(SCREEN_W // 2, SCREEN_H - 60)
        grafana.mouse.wheel(0, 420)
        dwell(8)
        grafana.mouse.wheel(0, -260)
        dwell(5)

        print("   Prometheus live counters")
        prom = context.new_page()
        prom.goto(PROM_URL, wait_until="domcontentloaded")
        dwell(8)

        print("   Q5/Q6 fire while Prometheus stays in view")
        app.bring_to_front()
        app.mouse.move(SCREEN_W // 4, SCREEN_H - 200)
        ask(app, "What happens when a queued job fails to process?")
        await_answer(app, timeout_s=45)
        prom.bring_to_front()
        dwell(8)
        app.bring_to_front()
        ask(app, "Which dashboard UID is provisioned for the platform?")
        await_answer(app, timeout_s=45)
        prom.bring_to_front()
        dwell(6)

        browser.close()


if __name__ == "__main__":
    propose()
    print("done — stop the recorder now")