#!/usr/bin/env python3
"""Build a ~1 minute demo video from browser-captured frames.

Playwright's sync API is single-threaded (background threads that call
page.screenshot() raise greenlet errors), and the env's virtual display won't
composite a headed browser to the X framebuffer for x11grab anyway. So we drive
the whole flow inline on one thread, ticking a frame capture between every
action — starting at the LOGIN screen and covering the full journey: login,
cited answer, honest refusal, more questions, the Grafana live dashboard, and
Prometheus live counters.

Frames are assembled into an mp4 at a framerate chosen so the clip lands close
to the requested duration (default ~60s), playing back up to 30fps.

Run (stack up on :80):
    ai-service/.venv/bin/python docs/screenshots/make_demo_video_frames.py \
        --out docs/screenshots/live-demo.mp4

Reuse an existing tenant (skips re-seeding the corpus) with:
    KNOWFLOW_DEMO_EMAIL / KNOWFLOW_DEMO_PASSWORD
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ai-service"))

import httpx  # noqa: E402
from playwright.sync_api import Page, sync_playwright  # noqa: E402

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
CAPTURE_EVERY = 0.10
TARGET_SECONDS = 60.0
STRONG_Q = ("How long is the refund window if a customer cancels their annual "
            "subscription?")
REFUSAL_Q = ("How should employees claim tax deductions for cryptocurrency "
             "mining at home?")
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


class Capture:
    """Single-threaded frame capture synchronised with the flow.

    The same thread that drives Playwright calls tick() from busy-loops; each
    tick screenshots the active page if CAPTURE_EVERY seconds have elapsed.
    """

    def __init__(self, output: Path):
        self.output = output
        self.page: Page | None = None
        self.count = 0
        self._last = 0.0

    def set_page(self, page: Page | None) -> None:
        self.page = page

    def tick(self) -> None:
        if self.page is None:
            return
        now = time.monotonic()
        if now - self._last < CAPTURE_EVERY:
            return
        self._last = now
        self.count += 1
        try:
            self.page.screenshot(
                path=str(self.output / f"f{self.count:05d}.jpg"),
                type="jpeg",
                quality=85,
                full_page=False,
            )
        except Exception:
            # navigated / closed mid-capture — carry on
            pass


def login(page: Page, email: str, password: str, cap: Capture) -> None:
    cap.set_page(page)
    page.goto(f"{BASE}/login", wait_until="domcontentloaded")
    cap.tick()

    email_box = page.get_by_placeholder("Email")
    for ch in email:
        email_box.press(ch)
        cap.tick()

    password_box = page.get_by_placeholder("Password", exact=True)
    for ch in password:
        password_box.press(ch)
        cap.tick()
    cap.tick()

    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(f"{BASE}/", timeout=30_000)

    cap.tick()
    page.goto(f"{BASE}/chat", wait_until="networkidle")
    cap.tick()


def ask(page: Page, text: str) -> None:
    box = page.locator("textarea[placeholder='Ask your knowledge base']")
    box.press_sequentially(text, delay=5)
    box.press("Enter")


def dwell(cap: Capture, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        cap.tick()
        time.sleep(0.05)


def await_answer(page: Page, cap: Capture, timeout_s: int = 60) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cap.tick()
        try:
            body = page.locator("body").inner_text(timeout=1000)
        except Exception:
            body = ""
        if "Sources" in body or page.locator("text=I don't know").count() > 0:
            return
        time.sleep(0.15)
    raise TimeoutError("answer did not settle")


def ensure_tenant() -> tuple[str, str]:
    email = os.environ.get("KNOWFLOW_DEMO_EMAIL")
    password = os.environ.get("KNOWFLOW_DEMO_PASSWORD")
    if email and password:
        return email, password
    print("  registering fresh tenant + awaiting the demo corpus READY...", flush=True)
    with httpx.Client(timeout=30.0) as client:
        token, _org_id, creds = register_org(client)
        document_ids = [
            upload_document(client, token, filename)
            for filename in DOC_FILENAMES.values()
        ]
        wait_all_ready(client, token, document_ids)
    return creds["email"], creds["password"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "docs/screenshots/live-demo.mp4"))
    parser.add_argument("--seconds", type=float, default=TARGET_SECONDS)
    args = parser.parse_args()
    target_s = args.seconds

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frames_dir = out_path.parent / f".frames_{uuid.uuid4().hex[:8]}"
    frames_dir.mkdir(parents=True, exist_ok=True)

    email, password = ensure_tenant()
    print(f"== recording video with tenant {email}", flush=True)
    t0 = time.monotonic()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", f"--window-size={SCREEN_W},{SCREEN_H}"],
        )
        context = browser.new_context(
            viewport={"width": SCREEN_W, "height": SCREEN_H - 40}
        )
        app = context.new_page()
        app.set_default_timeout(60_000)
        cap = Capture(frames_dir)

        # --- start capturing at the LOGIN screen ---------------------------
        print("  login", flush=True)
        login(app, email, password, cap)
        dwell(cap, 3.5)

        print("  Q1 cited answer", flush=True)
        ask(app, STRONG_Q)
        await_answer(app, cap)
        dwell(cap, 3.5)

        print("  Q2 honest refusal", flush=True)
        app.get_by_text("Refuse weak matches").click()
        dwell(cap, 3.0)
        ask(app, REFUSAL_Q)
        await_answer(app, cap)
        dwell(cap, 3.5)

        print("  Q3/Q4 activity burst", flush=True)
        for question in EXTRA_QS:
            ask(app, question)
            await_answer(app, cap, 45)
            dwell(cap, 3.5)

        print("  Grafana live dashboard", flush=True)
        grafana = context.new_page()
        grafana.goto(GRAFANA_URL, wait_until="domcontentloaded")
        cap.set_page(grafana)
        dwell(cap, 7.0)
        grafana.mouse.wheel(0, 420)
        dwell(cap, 6.0)

        print("  Prometheus live counters", flush=True)
        prom = context.new_page()
        prom.goto(PROM_URL, wait_until="domcontentloaded")
        cap.set_page(prom)
        dwell(cap, 6.0)

        print("  Q5/Q6 fire while Prometheus stays in view", flush=True)
        cap.set_page(app)
        app.mouse.move(SCREEN_W // 4, SCREEN_H - 200)
        ask(app, "What happens when a queued job fails to process?")
        await_answer(app, cap, 45)
        dwell(cap, 3.0)
        cap.set_page(prom)
        dwell(cap, 4.0)
        cap.set_page(app)
        ask(app, "Which dashboard UID is provisioned for the platform?")
        await_answer(app, cap, 45)
        dwell(cap, 3.0)
        cap.set_page(prom)
        dwell(cap, 6.0)

        total = cap.count
        browser.close()

    elapsed = time.monotonic() - t0
    print(f"  captured {total} frames over {elapsed:.0f}s", flush=True)

    # Source framerate scaled so the clip lands ~target_s long.
    import math

    fps = max(4.0, min(10.0, total / max(target_s, 1.0)))
    print(f"  assembling at {fps:.2f} fps -> ~{total/fps:.0f}s clip", flush=True)

    ffmpeg = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-framerate", f"{fps:.3f}",
        "-i", str(frames_dir / "f%05d.jpg"),
        "-vf", "fps=30,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-movflags", "+faststart",
        str(out_path),
    ]
    subprocess.run(ffmpeg, check=True)

    shutil.rmtree(frames_dir)
    size_kb = out_path.stat().st_size // 1024
    print(f"Recorded {out_path} ({size_kb} KB, {total/fps:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())