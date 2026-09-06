import time
import uuid

from fastapi import APIRouter, Request

from ..core.metrics import record_ai_call
from ..schemas import ChatCompletionResponse, ChatRequest, Choice, ChoiceMessage

router = APIRouter(tags=["chat"])


@router.post("/chat/completions", response_model=ChatCompletionResponse)
def create_chat_completion(body: ChatRequest, request: Request) -> ChatCompletionResponse:
    service = request.app.state.llm_service
    started = time.monotonic()
    result = service.complete(
        [message.model_dump() for message in body.messages], body.temperature
    )
    record_ai_call("chat", result.provider, time.monotonic() - started, result.usage)
    return ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:24]}",
        created=int(time.time()),
        model=result.model,
        provider=result.provider,
        choices=[Choice(message=ChoiceMessage(content=result.reply), finish_reason=result.finish_reason)],
        usage=result.usage,
    )