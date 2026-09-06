"""Per-request correlation id, shared between middleware and log formatters.

The HTTP middleware assigns a request id (incoming `x-request-id` or a fresh
uuid), stores it on `request.state` so handlers can echo it, and sets this
contextvar so every log line emitted anywhere during the request — including
inside the rag/chat/embedding services — carries the same id.
"""

import contextvars

request_id = contextvars.ContextVar("request_id", default="")