"""Day 30 — Prometheus metrics for the AI service and the RQ worker.

Both processes (uvicorn serving the FastAPI app and the `app.queue.worker` RQ
process) instrument the SAME module-level metrics. The API process exposes
them on `GET /metrics`; the worker process starts its own scrape endpoint on
`metrics_port` (default 8001) in `app.queue.worker.main`. Keep the metric names
stable — the Grafana dashboard and the compose Prometheus scrape config depend
on them.

Naming matches the backend's HTTP metrics (`http_requests_total`, etc.) so a
dashboard can merge both jobs; workers/LLM/queue metrics carry an ai_/queue_
prefix because they are specific to this service.
"""

import re
from typing import Any

from prometheus_client import Counter, Histogram, generate_latest

# --------------------------------------------------------------------------- HTTP
# Same names as the backend: the `job` label (set by the Prometheus scrape
# config) tells the two services apart on one dashboard.
HTTP_REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP requests handled.",
    labelnames=["method", "route", "status"],
)
HTTP_ERRORS = Counter(
    "http_errors_total",
    "HTTP responses with a client/server error status (>= 400).",
    labelnames=["method", "route", "status"],
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds.",
    labelnames=["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

# ---------------------------------------------------------------------------- AI
# Generation (rag/chat) latency and token consumption. `operation` is the
# endpoint family (rag.generate, rag.generate.stream, chat, embeddings),
# `provider` the provider name reported by the result (mock/extract/openai...).
AI_DURATION = Histogram(
    "ai_generation_duration_seconds",
    "Latency of AI generation/embedding calls, by operation and provider.",
    labelnames=["operation", "provider"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
AI_TOKENS = Counter(
    "ai_tokens_total",
    "Tokens consumed by AI calls, by operation, provider and kind.",
    labelnames=["operation", "provider", "kind"],
)

# ------------------------------------------------------------------------- Queue
# RQ worker job outcomes — `status` is the RunOutcome kind (processed,
# embedded, retrying, failed, ignored). Failure count divided by total is the
# queue failure rate for the dashboard.
QUEUE_JOBS = Counter(
    "queue_jobs_total",
    "RQ ingestion jobs by terminal/live outcome kind.",
    labelnames=["status"],
)
QUEUE_JOB_DURATION = Histogram(
    "queue_job_duration_seconds",
    "Duration of one RQ ingestion job run, by outcome kind.",
    labelnames=["status"],
    buckets=(0.01, 0.05, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300),
)

# Heuristic: uuid-like / long-hex / pure-number path segments collapse to `:id`
# so route-cardinality stays bounded (document ids never become unbounded
# metric labels).
_UUID_RX = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HEX32_RX = re.compile(r"[0-9a-f]{32}")
_DIGITS_RX = re.compile(r"\d+")


def _normalize_route(path: str) -> str:
    if not path:
        return "unmatched"
    return "/".join(
        ":id"
        if _UUID_RX.fullmatch(part)
        or _HEX32_RX.fullmatch(part)
        or _DIGITS_RX.fullmatch(part)
        else part
        for part in path.split("/")
    )


def record_http(method: str, path: str, status: int, seconds: float) -> None:
    route = _normalize_route(path)
    HTTP_REQUESTS.labels(method, route, str(status)).inc()
    HTTP_DURATION.labels(method, route).observe(seconds)
    if status >= 400:
        HTTP_ERRORS.labels(method, route, str(status)).inc()


def record_ai_call(
    operation: str,
    provider: str,
    seconds: float,
    usage: dict[str, Any] | Any = None,
) -> None:
    """Time an AI call (rag/chat/embeddings) and account its tokens.

    `usage` may be a dict (raw API payload) or a pydantic `Usage` with
    prompt_tokens / completion_tokens / total_tokens fields.
    """
    AI_DURATION.labels(operation, provider).observe(seconds)
    if not usage:
        return
    for kind in ("prompt", "completion", "total"):
        attr = f"{kind}_tokens"
        value = usage.get(attr) if isinstance(usage, dict) else getattr(usage, attr, 0)
        if value:
            AI_TOKENS.labels(operation, provider, kind).inc(float(value))


def record_queue_outcome(status: str, seconds: float) -> None:
    """Account one worker run by its outcome kind (processed/retrying/failed...)."""
    QUEUE_JOBS.labels(status).inc()
    QUEUE_JOB_DURATION.labels(status).observe(seconds)


def render_metrics() -> bytes:
    return generate_latest()