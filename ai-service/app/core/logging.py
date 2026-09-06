"""Day 30 — structured logging.

Production logs are single-line JSON (one object per event, with the request id
and tracer-friendly fields); development stays human-readable. A filter injects
the current `x-request-id` (see `app.core.context`) into every record, so a
trace can be threaded through the whole service from one header.
"""

import json
import logging
import sys
import time


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        from .context import request_id

        record.request_id = request_id.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        payload = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": message,
            "request_id": getattr(record, "request_id", ""),
        }
        if hasattr(record, "extra"):
            payload["extra"] = record.extra
        return json.dumps(payload, default=str, ensure_ascii=True)


def configure_logging(debug: bool = False, json_format: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    # basicConfig only applies when there is no existing handler; a repeated
    # create_app() (tests) must not stack handlers. Replace handlers once.
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(JsonFormatter() if json_format else logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    ))
    root.addHandler(handler)