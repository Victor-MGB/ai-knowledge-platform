"""Day 30 — request-scoped observability middleware.

One middleware per request: assigns/echoes `x-request-id` (into a contextvar so
every log line carries it), records the HTTP metrics families for this
service, and returns the id to the caller so backend/client logs can be joined
end to end.
"""

import time
import uuid

from fastapi import Request

from .core.context import request_id as request_id_var
from .core.metrics import record_http


def install(app) -> None:
    @app.middleware("http")
    async def observability(request: Request, call_next):
        started = time.monotonic()
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request_id_var.set(rid)
        request.state.request_id = rid
        try:
            response = await call_next(request)
        except Exception:
            record_http(request.method, request.url.path, 500, time.monotonic() - started)
            raise
        record_http(
            request.method,
            request.url.path,
            response.status_code,
            time.monotonic() - started,
        )
        response.headers["x-request-id"] = rid
        return response