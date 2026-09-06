"""Day 34 — the production demo, live and end-to-end.

Drives the whole stack through the public API, exactly as a presenter would:
register a fresh tenant, upload all ten Lumina demo PDFs, wait for the worker
pipeline to land every document in READY (extract -> chunk -> embed -> pgvector),
then ask the same fourteen questions the offline eval proves answerable and
print each answer with its citation (document / section / page / similarity).

The demo makes two guarantees, both verified against the golden corpus:

  1. every grounded question cites the right document + section (the offline
     eval proves the quoted sentence is that section's golden fact), and
  2. the out-of-scope question is refused by the minScore guardrail instead of
     hallucinated.

The tenant is left in place (with credentials printed) so the presenter can log
in and point-and-click afterwards; pass `--cleanup` to delete the documents via
the public API and drop the throwaway org at the end.

Run:
    ai-service/.venv/bin/python -m tests.e2e.demo [--base http://localhost] [--cleanup]

Prereqs: the docker compose stack up (backend, ai-service, workers, postgres,
minio, redis) with the frontend reachable on :80 as above.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ai-service"))

from tests.e2e.demo_corpus import DOC_FILENAMES, OUT_DIR  # noqa: E402
from tests.e2e.offline_eval import DEMO_QUESTIONS  # noqa: E402

BASE = "http://localhost"
DATABASE_URL_DEFAULT = "postgres://knowflow:knowflow@localhost:5434/knowflow"
POLL_INTERVAL_S = 2.0
POLL_TIMEOUT_S = 300.0

PASSWORD = "Sup3rSecret!1"


def register_org(client: httpx.Client) -> tuple[str, str, dict]:
    email = f"demo-{uuid.uuid4().hex[:10]}@example.com"
    org_name = f"lumina-demo-{uuid.uuid4().hex[:6]}"
    payload = {
        "email": email,
        "password": PASSWORD,
        "organization": org_name,
        "name": "Demo Runner",
    }
    response = client.post(f"{BASE}/auth/register", json=payload)
    response.raise_for_status()
    body = response.json()
    creds = {"email": email, "password": PASSWORD, "organization": org_name}
    return body["tokens"]["accessToken"], body["organization"]["id"], creds


def upload_document(client: httpx.Client, token: str, title: str, filename: str) -> str:
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


def status(client: httpx.Client, token: str, document_id: str) -> dict:
    response = client.get(
        f"{BASE}/api/v1/documents/{document_id}/status",
        headers={"authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
    return response.json()


def wait_all_ready(client: httpx.Client, token: str, document_ids: list[str]) -> dict[str, dict]:
    pending = set(document_ids)
    final: dict[str, dict] = {}
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while pending and time.monotonic() < deadline:
        for document_id in tuple(pending):
            body = status(client, token, document_id)
            stage = body["stage"]
            if body.get("error") or body.get("embeddingError"):
                raise AssertionError(f"pipeline failed for {document_id}: {body}")
            if stage in ("ready", "failed"):
                if stage == "failed":
                    raise AssertionError(f"document {document_id} failed: {body}")
                final[document_id] = body
                pending.discard(document_id)
        if pending:
            done = len(document_ids) - len(pending)
            print(f"      {done}/{len(document_ids)} documents READY, waiting...")
            time.sleep(POLL_INTERVAL_S)
    if pending:
        raise AssertionError(f"timed out waiting for: {pending}")
    return final


def stamp() -> str:
    return time.strftime("%H:%M:%S")


def main() -> int:
    global BASE
    parser = argparse.ArgumentParser(description="Run the live production demo.")
    parser.add_argument("--base", default=BASE, help=f"base URL (default {BASE})")
    parser.add_argument("--cleanup", action="store_true", help="delete the tenant afterwards")
    args = parser.parse_args()

    BASE = args.base.rstrip("/")

    print(f"== Day 34 demo @ {stamp()}: base={BASE}")
    with httpx.Client(timeout=30.0) as client:
        landing = client.get(f"{BASE}/")
        assert landing.status_code < 500, f"base URL not reachable: {landing.status_code}"
        print(f"   stack reachable  @ {stamp()}")

        token, org_id, creds = register_org(client)
        print(f"   registered tenant {creds['organization']} "
              f"(org {org_id})  @ {stamp()}")

        document_ids = {}
        for title, filename in DOC_FILENAMES.items():
            document_id = upload_document(client, token, title, filename)
            document_ids[document_id] = title
            print(f"   uploaded {filename} -> {title}")

        print(f"   awaiting worker pipeline...  @ {stamp()}")
        finals = wait_all_ready(client, token, list(document_ids))
        total_chunks = sum(body["counts"]["chunks"] for body in finals.values())
        total_pages = sum(body["counts"]["pages"] for body in finals.values())
        print(f"   all READY  @ {stamp()}  -> {len(finals)} documents, "
              f"{total_pages} pages, {total_chunks} chunks")

        results = []
        passed = 0
        for index, (question, doc, section, golden, min_score) in enumerate(DEMO_QUESTIONS, start=1):
            payload: dict = {"question": question, "limit": 5, "maxContextTokens": 1200}
            if min_score is not None:
                payload["minScore"] = min_score
            response = client.post(
                f"{BASE}/api/v1/rag/generate",
                headers={"authorization": f"Bearer {token}"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
            evidence = body.get("evidence") or []
            first = evidence[0] if evidence else None

            expected_doc = doc
            expected_section = section
            if expected_doc is None:
                ok = body["refused"] is True
            else:
                ok = (
                    body["refused"] is False
                    and first is not None
                    and first.get("documentTitle") == expected_doc
                    and first.get("section") == expected_section
                )
            passed += ok

            citation = ""
            if first is not None:
                citation = (
                    f"{first.get('documentTitle')} / {first.get('section')} "
                    f"p.{first.get('page')} (sim {first.get('similarity'):.3f})"
                )
            status_text = "REFUSED" if body["refused"] else f"{citation}"
            print(f"\nQ{index:02d}  {'PASS' if ok else 'FAIL'}  {status_text}")
            print(f"     {question}")
            if expected_doc is None:
                print(f"     -> {body['answer'][:160]}")
                continue
            print(f"     -> {body['answer']}")
            for cite in body.get("citations", []):
                print(
                    f"     [src {cite['id']}] {cite.get('title')} / "
                    f"{cite.get('section')} p.{cite.get('page')} "
                    f"(sim {cite.get('similarity'):.3f})"
                )

        print(f"\n{'='*60}")
        print(f"{passed}/{len(DEMO_QUESTIONS)} questions PASS  @ {stamp()}")
        print(f"tenant kept:\n   organization: {creds['organization']}\n"
              f"   email: {creds['email']}\n   password: {PASSWORD}")

        if args.cleanup:
            for document_id in document_ids:
                response = client.delete(
                    f"{BASE}/api/v1/documents/{document_id}",
                    headers={"authorization": f"Bearer {token}"},
                )
                assert response.status_code == 204, response.text
            print(f"   documents deleted via the API  @ {stamp()}")
            import psycopg

            with psycopg.connect(
                DATABASE_URL_DEFAULT, autocommit=True
            ) as conn:
                conn.execute("DELETE FROM organizations WHERE id = %s", (org_id,))
            print(f"   org {org_id} removed")

    return 0 if passed == len(DEMO_QUESTIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())