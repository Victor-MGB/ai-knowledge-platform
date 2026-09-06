from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..core.metrics import record_ai_call
from ..schemas import RagGenerationRequest, RagGenerationResponse

router = APIRouter(tags=["rag"])


@router.post("/rag/generate", response_model=RagGenerationResponse)
def create_rag_generation(
    body: RagGenerationRequest, request: Request
) -> RagGenerationResponse:
    """Question + retrieved context -> grounded answer.

    The backend owns tenant-scoped retrieval and ships the top chunks here;
    this endpoint does the context formatting, prompt assembly and answer
    generation (plus the "I don't know" guardrails) via the configured
    provider. `refused=true` on the response is a *valid answer*, not an
    error — it is the honest "the corpus I was given cannot answer this".
    """
    import time

    started = time.monotonic()
    result = request.app.state.rag_service.generate(body)
    record_ai_call(
        "rag.generate", result.provider, time.monotonic() - started, result.usage
    )
    return result


@router.post("/rag/generate/stream")
def create_rag_generation_stream(
    body: RagGenerationRequest, request: Request
) -> StreamingResponse:
    """Streaming variant of /rag/generate.

    Answer tokens are pushed as `data: {"delta": "<text>"}` SSE events as they
    arrive from the LLM; the final event is
    `data: {"done": {answer, refused, citations, evidence, ...}}` carrying the
    full response. A refusal (`refused=true`) streams no tokens and immediately
    emits the done event. The final `done` payload is always valid JSON for
    `RagGenerationResponse`.
    """
    import json
    import time

    def event_stream():
        started = time.monotonic()
        provider = "unknown"
        usage = None
        stream = request.app.state.rag_service.generate_stream(body)
        for item in stream:
            if isinstance(item, str):
                yield f'data: {json.dumps({"delta": item})}\n\n'
            else:
                final = item.model_dump()
                provider = final.get("provider", "unknown")
                usage = final.get("usage")
                yield f'data: {json.dumps({"done": final})}\n\n'
        record_ai_call(
            "rag.generate.stream", provider, time.monotonic() - started, usage
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )