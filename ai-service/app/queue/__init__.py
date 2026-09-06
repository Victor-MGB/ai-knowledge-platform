"""Day-12 queue package: durable ledger + RQ integration + derived state.

    repository   ingestion_jobs row access (atomic claims, retry budget)
    state        UPLOADED/PROCESSING/EMBEDDING/READY/FAILED derivation
    service      QueueService facade: enqueue (API) + one run of a phase
    jobs         RQ task functions the worker resolves by dotted path
    worker       `python -m app.queue.worker` entrypoint
"""

from .repository import JobRecord, JobRepository
from .service import QueueService, RunOutcome, build_default_queue_service
from .state import derive_state

__all__ = [
    "JobRecord",
    "JobRepository",
    "QueueService",
    "RunOutcome",
    "build_default_queue_service",
    "derive_state",
]