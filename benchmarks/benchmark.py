#!/usr/bin/env python3
"""Day-33 performance harness — measures the KnowFlow stack over the public
API plus the ingestion ledger, then reports the split the user asked for:

  documents:  10 / 100 / 1,000          via the `ingest` command
  concurrency:sustained searches/users    via the `search` / `users` commands
  large PDFs: multi-hundred-page file     via the `large` command

  metrics:    upload latency · processing time · embedding time ·
              search latency · LLM (generation) latency · total response time

Processing and embedding time come straight from the `ingestion_jobs` ledger
(enqueued/started/finished), so there is no polling race in the splits.

Run against the compose stack on :80 (nginx). Requires the ai-service venv
(psycopg + httpx) and a Postgres reachable at BENCH_PG_DSN (default the
compose store on 127.0.0.1:5434).
"""

import argparse
import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
RESULTS.mkdir(parents=True, exist_ok=True)

REGISTER = "/auth/register"
LOGIN = "/auth/login"
DOCS = "/api/v1/documents"
SEARCH = "/api/v1/search"
RAG = "/api/v1/rag/generate"
STATUS = "/api/v1/documents/{id}/status"

PASSWORD = "benchpass123"
BENCH_PG = os.environ.get("BENCH_PG_DSN", "postgres://knowflow:knowflow@127.0.0.1:5434/knowflow")


# ---------------------------------------------------------------- report utils
def summarize(values: list[float]) -> dict:
    values = sorted(values)
    n = len(values)
    if n == 0:
        return {"n": 0}
    def pct(p):
        return values[min(n - 1, int(p * n))]
    return {
        "n": n,
        "mean_ms": round(statistics.fmean(values), 1),
        "p50_ms": round(pct(0.50), 1),
        "p90_ms": round(pct(0.90), 1),
        "p95_ms": round(pct(0.95), 1),
        "p99_ms": round(pct(0.99), 1),
        "max_ms": round(values[-1], 1),
    }


def md_table(rows: list[dict]) -> str:
    if not rows:
        return "(no data)"
    headers = list(rows[0].keys())
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(r[h]) for h in headers) + " |")
    return "\n".join(out)


def save(name: str, data: dict) -> Path:
    path = RESULTS / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, default=str))
    return path


def load(name: str) -> dict:
    return json.loads((RESULTS / f"{name}.json").read_text())


# ------------------------------------------------------------------- http/api
def client(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, timeout=httpx.Timeout(120.0, connect=10.0), headers={"Accept": "application/json"})


def register(c: httpx.Client, email: str) -> dict:
    r = c.post(REGISTER, json={"email": email, "password": PASSWORD})
    r.raise_for_status()
    body = r.json()
    return {"org": body["organization"]["id"], "user": body["user"]["id"], "token": body["tokens"]["accessToken"]}


def login(c: httpx.Client, email: str) -> str:
    r = c.post(LOGIN, json={"email": email, "password": PASSWORD})
    r.raise_for_status()
    return r.json()["tokens"]["accessToken"]


def authed(base: str, email: str) -> tuple[httpx.Client, str]:
    """A logged-in client for `email`, registering the account if it doesn't
    exist yet (401 on login -> register)."""
    c = client(base)
    try:
        return c, login(c, email)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 401:
            raise
    register(c, email)
    return c, login(c, email)


def upload(c: httpx.Client, token: str, path: Path) -> tuple[str, float]:
    t0 = time.perf_counter()
    with path.open("rb") as fh:
        r = c.post(
            DOCS,
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (path.name, fh, "application/pdf")},
        )
    ms = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    return r.json()["id"], ms


def wait_ready(c: httpx.Client, token: str, doc_id: str, timeout: float = 900) -> str:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        r = c.get(STATUS.format(id=doc_id), headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        stage = r.json()["stage"]
        if stage in ("ready", "failed"):
            return stage
        time.sleep(0.2)
    raise TimeoutError(f"document {doc_id} never reached a terminal stage")


def search(c: httpx.Client, token: str, query: str) -> tuple[float, int]:
    t0 = time.perf_counter()
    r = c.post(SEARCH, json={"query": query}, headers={"Authorization": f"Bearer {token}"})
    ms = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    return ms, len(r.json()["results"])


def rag(c: httpx.Client, token: str, question: str) -> tuple[float, bool, str]:
    t0 = time.perf_counter()
    r = c.post(RAG, json={"question": question}, headers={"Authorization": f"Bearer {token}"})
    ms = (time.perf_counter() - t0) * 1000
    r.raise_for_status()
    body = r.json()
    return ms, bool(body.get("refused", False)), str(body.get("answer", ""))


# -------------------------------------------------------------- pg ledger read
def ledger_rows(doc_ids: list[str]) -> list[dict]:
    import psycopg

    with psycopg.connect(BENCH_PG) as conn:
        rows = []
        with conn.cursor() as cur:
            for doc_id in doc_ids:
                cur.execute(
                    """
                    SELECT kind, status,
                           EXTRACT(EPOCH FROM (COALESCE(finished_at, now()) - started_at)) AS dur_s,
                           EXTRACT(EPOCH FROM (started_at - enqueued_at)) AS wait_s
                    FROM ingestion_jobs WHERE document_id = %s ORDER BY kind
                    """,
                    (doc_id,),
                )
                for kind, status, dur_s, wait_s in cur.fetchall():
                    rows.append({"doc": doc_id, "kind": kind, "status": status,
                                 "dur_s": dur_s, "wait_s": wait_s})
    return rows


# ------------------------------------------------------------------ ingest mode
def cmd_ingest(args) -> None:
    c, token = authed(args.base, args.email) if args.email else (client(args.base), load("session")["token"])
    corpus = sorted(Path(args.corpus).glob("kf_doc_*.pdf"))
    if args.limit:
        corpus = corpus[: args.limit]
    n = len(corpus)
    print(f"ingesting {n} documents into {args.email or 'session org'} ({args.corpus})")

    # upload everything first (fast), then drain the queue — the batch wall
    # time is the pipeline throughput; per-doc splits come from the ledger.
    entries = []
    t_start = time.monotonic()
    if args.upload_workers > 1 and n >= args.upload_workers:
        def _up(i_path):
            doc_id, upload_ms = upload(c, token, i_path[1])
            return {"doc": doc_id, "file": i_path[1].name, "upload_ms": upload_ms}
        with ThreadPoolExecutor(max_workers=args.upload_workers) as ex:
            entries = list(ex.map(_up, enumerate(corpus, 1)))
    else:
        for i, path in enumerate(corpus, 1):
            doc_id, upload_ms = upload(c, token, path)
            entries.append({"doc": doc_id, "file": path.name, "upload_ms": upload_ms})
            if i % 250 == 0:
                print(f"  uploaded {i}/{n} (last {upload_ms:.0f} ms)")
    upload_wall = time.monotonic() - t_start
    print(f"  uploaded {n} docs in {upload_wall:.1f}s ({n / upload_wall:.0f} uploads/s)")

    import concurrent.futures as cf

    t_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=min(16, n)) as ex:
        futs = {ex.submit(wait_ready, c, token, e["doc"]): e for e in entries}
        for f in cf.as_completed(futs):
            futs[f]["stage"] = f.result(timeout=1800)
    drain_wall = time.monotonic() - t_start
    print(f"  all {n} docs READY in {drain_wall:.1f}s ({n / drain_wall * 60:.1f} docs/min)")

    ledger = {}
    for e in entries:
        for row in ledger_rows([e["doc"]]):
            ledger[(row["doc"], row["kind"])] = {"dur_s": row["dur_s"], "wait_s": row["wait_s"]}

    for e in entries:
        p = ledger.get((e["doc"], "process"), {})
        em = ledger.get((e["doc"], "embed"), {})
        e["process_ms"] = round((p.get("dur_s") or 0) * 1000, 1)
        e["embed_ms"] = round((em.get("dur_s") or 0) * 1000, 1)
        e["queue_wait_ms"] = round((p.get("wait_s") or 0) * 1000, 1)

    data = {
        "n": n,
        "org": args.email or load("session").get("org"),
        "upload_wall_s": round(upload_wall, 2),
        "drain_wall_s": round(drain_wall, 2),
        "docs_per_min": round(n / drain_wall * 60, 1),
        "upload_ms": summarize([e["upload_ms"] for e in entries]),
        "process_ms": summarize([e["process_ms"] for e in entries]),
        "embed_ms": summarize([e["embed_ms"] for e in entries]),
        "queue_wait_ms": summarize([e["queue_wait_ms"] for e in entries]),
    }
    save(f"ingest_{n}", data)
    print("\n## ingest", n)
    print("| metric | mean | p50 | p95 | max |")
    print("|---|---|---|---|---|")
    for k in ("upload_ms", "process_ms", "embed_ms", "queue_wait_ms"):
        s = data[k]
        print(f"| {k} | {s['mean_ms']} | {s['p50_ms']} | {s['p95_ms']} | {s['max_ms']} |")


# ---------------------------------------------------------------- search mode
def cmd_search(args) -> None:
    c, token = authed(args.base, args.email) if args.email else (client(args.base), load("session")["token"])
    queries = Path(args.queries).read_text().splitlines() if args.queries else ["refund policy in the EU region"]
    queries = [q for q in queries if q.strip()][: args.rounds] or ["refund window"]

    def one(i: int) -> tuple[float, int]:
        return search(clean_client(), token, queries[i % len(queries)])

    def clean_client() -> httpx.Client:
        return client(args.base)

    def warm() -> None:
        search(clean_client(), token, queries[0])

    warm()
    single = []
    for _ in range(args.warm):
        ms, n = search(clean_client(), token, queries[_ % len(queries)])
        single.append(ms)

    t0 = time.perf_counter()
    results = []
    if args.concurrent > 1:
        with ThreadPoolExecutor(max_workers=args.concurrent) as ex:
            futs = [ex.submit(one, i) for i in range(args.rounds)]
            for f in as_completed(futs):
                results.append(f.result())
    else:
        for i in range(args.rounds):
            results.append(one(i))
    wall = time.perf_counter() - t0
    lats = [r[0] for r in results]

    data = {
        "base": args.base,
        "concurrent": args.concurrent if args.concurrent > 1 else 1,
        "queries": args.rounds,
        "search_latency_ms": summarize(lats),
        "wall_s": round(wall, 2),
        "qps": round(len(lats) / wall, 1),
        "results_per_query": round(statistics.fmean([r[1] for r in results]), 1),
    }
    save("search", data)
    print("\n## search")
    print(json.dumps(data, indent=2))


# --------------------------------------------------------------------- rag mode
def cmd_rag(args) -> None:
    c, token = authed(args.base, args.email) if args.email else (client(args.base), load("session")["token"])
    questions = Path(args.questions).read_text().splitlines() if args.questions else ["What is the EU refund window?"]
    questions = [q for q in questions if q.strip()][: args.rounds] or ["refund policy in the EU region"]
    refused = 0
    lats = []
    for i in range(args.rounds):
        ms, bad, _ = rag(c, token, questions[i % len(questions)])
        lats.append(ms)
        refused += int(bad)
    data = {"rag_generate_latency_ms": summarize(lats), "refused": refused}
    save("rag", data)
    print("\n## rag generate")
    print(json.dumps(data, indent=2))


# ------------------------------------------------------------------- users mode
def cmd_users(args) -> None:
    """`users` users, each in their own org, each with `docs_per_user` docs;
    all pull concurrently with searches + one RAG each. Concurrency is real:
    separate JWTs, separate tenants, one shared backend/AI/DB stack."""
    c = client(args.base)
    corpus = sorted(Path(args.corpus).glob("kf_doc_*.pdf"))
    users = []
    for u in range(args.users):
        email = f"bench.user{u}@{args.domain}"
        try:
            users.append(register(c, email))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                users.append({"token": login(c, email), "org": None})
            else:
                raise
    print(f"{len(users)} users registered")

    for u, sess in enumerate(users):
        for j in range(args.docs_per_user):
            doc_id, _ = upload(c, sess["token"], corpus[(u * args.docs_per_user + j) % len(corpus)])
            sess.setdefault("docs", []).append(doc_id)
    print(f"{sum(len(u['docs']) for u in users)} documents uploaded")

    with ThreadPoolExecutor(max_workers=args.users) as ex:
        futs = [ex.submit(_user_load, args.base, u, sess, args.queries_per_user) for u, sess in enumerate(users)]
        outcomes = [f.result() for f in as_completed(futs)]
    outcomes.sort(key=lambda o: o["user"])

    searches = [o["search_latencies"] for o in outcomes]
    rags = [o["rag_latencies"] for o in outcomes]
    wall = max(o["wall_s"] for o in outcomes)  # global overlap period
    data = {
        "users": args.users,
        "searches_each": args.queries_per_user,
        "search_latency_ms": summarize([ms for lst in searches for ms in lst]),
        "rag_latency_ms": summarize([ms for lst in rags for ms in lst]),
        "wall_s": round(wall, 2),
        "search_qps": round(sum(len(lst) for lst in searches) / wall, 1),
        "rag_qps": round(sum(len(lst) for lst in rags) / wall, 1),
        "failed": sum(o["failures"] for o in outcomes),
    }
    save("users", data)
    print("\n## concurrent users")
    print(json.dumps(data, indent=2))
    for o in outcomes:
        print(f'  user {o["user"]:>3}  searches={len(o["search_latencies"])}  '
              f'p50={summarize(o["search_latencies"])["p50_ms"]}ms  wall={o["wall_s"]:.2f}s')


def _user_load(base: str, user: int, sess: dict, n: int) -> dict:
    """One simulated user: `n` sequential search+RAG turns against the shared
    stack on their own tenant (own JWT, own org). Returns per-user latencies
    plus the wall time that user's turns covered."""
    c = client(base)
    token = sess["token"]
    queries = ["refund policy EU", "onboarding security training", "network failover",
               "data retention", "churn risk", "incident response", "warehouse safety",
               "brand typography", "reporting calendar"]
    t0 = time.monotonic()
    search_lats, rag_lats, failures = [], [], 0
    for i in range(n):
        try:
            ms, _ = search(c, token, queries[i % len(queries)])
            search_lats.append(ms)
        except httpx.HTTPError:
            failures += 1
        try:
            ms2, _, _ = rag(c, token, "What is the EU refund window?")
            rag_lats.append(ms2)
        except httpx.HTTPError:
            failures += 1
    wall_s = time.monotonic() - t0
    return {"user": user, "search_latencies": search_lats, "rag_latencies": rag_lats,
            "failures": failures, "wall_s": wall_s}


# ------------------------------------------------------------------- large mode
def cmd_large(args) -> None:
    c, token = authed(args.base, args.email) if args.email else (client(args.base), load("session")["token"])
    path = Path(args.pdf)
    doc_id, upload_ms = upload(c, token, path)
    t0 = time.monotonic()
    stage = wait_ready(c, token, doc_id)
    wall = time.monotonic() - t0
    rows = ledger_rows([doc_id])
    p = next((r for r in rows if r["kind"] == "process"), {})
    em = next((r for r in rows if r["kind"] == "embed"), {})

    try:
        from pypdf import PdfReader
        pages = len(PdfReader(str(path)).pages)
    except Exception:
        pages = -1

    data = {
        "file": path.name,
        "size_bytes": path.stat().st_size,
        "pages": pages,
        "upload_ms": round(upload_ms, 1),
        "process_ms": round((p.get("dur_s") or 0) * 1000, 1),
        "embed_ms": round((em.get("dur_s") or 0) * 1000, 1),
        "total_to_ready_s": round(wall, 1),
        "ms_per_page": round(((p.get("dur_s") or 0) * 1000) / pages, 2) if pages > 0 else None,
        "stage": stage,
    }
    save("large", data)
    print("\n## large pdf")
    print(json.dumps(data, indent=2, default=str))


# --------------------------------------------------------------------- session
def cmd_setup(args) -> None:
    c = client(args.base)
    try:
        sess = register(c, args.email)
        print("registered new user")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 409:
            raise
        sess = {"token": login(c, args.email), "org": None}
        print("logged in existing user")
    sess["base"] = args.base
    sess["email"] = args.email
    save("session", sess)
    print(f"session: base={args.base} email={args.email}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def base_p(p):
        p.add_argument("--base", default="http://localhost:80")
        p.add_argument("--email", default="bench.suite@example.com")

    p = sub.add_parser("setup", help="register/login a session user")
    base_p(p); p.set_defaults(fn=cmd_setup)

    p = sub.add_parser("ingest", help="upload N docs, record upload + process + embed latencies")
    base_p(p)
    p.add_argument("--corpus", default=str(HERE / "corpus"))
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--upload-workers", type=int, default=4)
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("search", help="single + concurrent search latency")
    base_p(p)
    p.add_argument("--queries", default=str(HERE / "corpus" / "queries.txt"))
    p.add_argument("--rounds", type=int, default=50)
    p.add_argument("--concurrent", type=int, default=1)
    p.add_argument("--warm", type=int, default=3)
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("rag", help="RAG generation (LLM wrapper) latency")
    base_p(p)
    p.add_argument("--questions", default="")
    p.add_argument("--rounds", type=int, default=30)
    p.set_defaults(fn=cmd_rag)

    p = sub.add_parser("users", help="concurrent users (own org each) running searches + RAG")
    base_p(p)
    p.add_argument("--users", type=int, default=8)
    p.add_argument("--docs-per-user", type=int, default=3)
    p.add_argument("--queries-per-user", type=int, default=6)
    p.add_argument("--corpus", default=str(HERE / "corpus"))
    p.add_argument("--domain", default="bench.example.com")
    p.set_defaults(fn=cmd_users)

    p = sub.add_parser("large", help="ingest one large PDF")
    base_p(p)
    p.add_argument("--pdf", required=True)
    p.set_defaults(fn=cmd_large)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()