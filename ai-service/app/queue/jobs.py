"""Convert a document into retrieval-ready pages + chunks in the background.

Thin module-level wrappers over `QueueService.run_process`: RQ resolves tasks
by dotted path, so the functions must be importable at the top level and the
service wiring must be shared across a worker's lifetime (one set of DB
connections per process, built lazily and reused). `reset_service()` exists
for tests that need a fresh wiring against changed settings.
"""

import time

from ..core.config import get_settings
from ..core.metrics import record_queue_outcome
from .repository import JobRepository
from .schemas import JobRecord, QueueStatus
from .service import QueueService, RunOutcome, build_default_queue_service
from .state import derive_state

__all__ = [
    "JobRecord",
    "JobRepository",
    "QueueService",
    "QueueStatus",
    "RunOutcome",
    "build_default_queue_service",
    "derive_state",
    "reset_service",
    "run_job_embed",
    "run_job_process",
]

_default: QueueService | None = None


def default_service() -> QueueService:
    global _default
    if _default is None:
        _default = build_default_queue_service(get_settings())
    return _default


def reset_service() -> None:
    global _default
    _default = None


def _record(outcome: RunOutcome) -> dict:
    """Account one worker run against the queue metrics (status is the outcome
    kind: processed / embedded / retrying / failed / ignored), then return the
    payload. Wrapped here so CLI/tests see the same numbers the worker does."""
    record_queue_outcome(outcome.status, outcome.duration_seconds)
    return outcome.to_dict()


def run_job_process(document_id: str) -> dict:
    """RQ task: extract + chunk one queued document, then chain the embed job."""
    started = time.monotonic()
    outcome = default_service().run_process(str(document_id))
    outcome.duration_seconds = time.monotonic() - started
    return _record(outcome)


def run_job_embed(document_id: str) -> dict:
    """RQ task: vectorize one ready document's chunks."""
    started = time.monotonic()
    outcome = default_service().run_embed(str(document_id))
    outcome.duration_seconds = time.monotonic() - started
    return _record(outcome)