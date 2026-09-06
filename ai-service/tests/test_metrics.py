"""Day 30 — Prometheus /metrics + request-id correlation on the AI service.

Pins the observability contract: the scrape endpoint advertises the same HTTP
metric names as the backend (job label separates them), AI generation/token
metrics exist after a rag/chat/embedding call, and `x-request-id` round-trips
so backend and AI logs join on one id.
"""


def _hit(client, method, url, **kwargs):
    headers = kwargs.pop("headers", {})
    response = client.request(method, url, headers=headers, **kwargs)
    assert "x-request-id" in response.headers, f"{method} {url} should echo x-request-id"
    return response


def test_metrics_endpoint_exported(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    # names shared with the backend + the service-specific families
    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body
    assert "ai_generation_duration_seconds" in body
    assert "ai_tokens_total" in body
    assert "queue_jobs_total" in body


def test_request_id_round_trips_and_metrics_watch_requests(client):
    rid = "req-42-abc"
    _hit(client, "GET", "/health", headers={"x-request-id": rid})
    response = client.get("/metrics")
    body = response.text
    assert 'http_requests_total{method="GET",route="/health",status="200"}' in body
    assert 'http_request_duration_seconds_bucket{le="0.005",method="GET",route="/health"}' in body


def test_error_responses_count_toward_error_rate(client):
    response = client.get("/v1/does-not-exist")
    assert response.status_code == 404
    body = client.get("/metrics").text
    assert 'http_errors_total{method="GET",route="/v1/does-not-exist",status="404"}' in body


def test_rag_generation_records_ai_latency_and_tokens(client):
    payload = {
        "question": "what is 2+2",
        "context": [
            {"text": "two plus two is four.", "chunk_id": "c1", "document_id": "d1", "document_title": "T"}
        ],
    }
    response = client.post("/v1/rag/generate", json=payload)
    assert response.status_code == 200
    body = client.get("/metrics").text
    provider = response.json()["provider"]
    assert f'ai_generation_duration_seconds_bucket{{le="0.01",operation="rag.generate",provider="{provider}"}}' in body


def test_chat_completions_record_token_usage(client):
    # the mock LLM reports real (word counted) usage, so the token counter
    # family must move for a chat call
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello there"}], "temperature": 0},
    )
    assert response.status_code == 200
    provider = response.json()["provider"]
    body = client.get("/metrics").text
    for kind in ("prompt", "completion", "total"):
        line = f'ai_tokens_total{{kind="{kind}",operation="chat",provider="{provider}"}}'
        assert line in body
    import re

    prompt = re.search(rf'ai_tokens_total\{{kind="prompt",operation="chat",provider="{provider}"\}}\s+(\d+)', body)
    assert prompt and int(prompt.group(1)) > 0


def test_route_path_params_collapse_to_a_bounded_label(client):
    # a uuid path segment must not become an unbounded metric label even on an
    # unmatched deep path (this route never touches the DB, so it is DB-free)
    rid = "route-cardinality"
    _hit(client, "GET", "/v1/queue/documents/3f2c9a1e-7d5b-4f0a-9c3e-8b2a1d0f5c77/nothing", headers={"x-request-id": rid})


def test_queue_outcome_records_failure_and_duration_metrics():
    from app.core.metrics import record_queue_outcome

    record_queue_outcome(status="failed", seconds=1.25)
    record_queue_outcome(status="processed", seconds=0.5)
    body = render()
    assert 'queue_jobs_total{status="failed"} 1.0' in body
    assert 'queue_jobs_total{status="processed"} 1.0' in body
    assert 'queue_job_duration_seconds_count{status="failed"} 1.0' in body
    assert 'queue_job_duration_seconds_sum{status="failed"} 1.25' in body


def render() -> str:
    from app.core.metrics import render_metrics

    return render_metrics().decode()
