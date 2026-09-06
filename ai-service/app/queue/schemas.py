from datetime import datetime

from pydantic import BaseModel

# Day 12 — one row of ingestion_jobs as the queue facade and the API see it.
# `kind` is the phase (process | embed), `status` the durable lifecycle
# (queued -> processing -> succeeded / failed) and attempts/max_attempts the
# retry budget. RQ keeps the transient task in Redis; this is the audit trail.


class JobRecord(BaseModel):
    job_id: str
    organization_id: str
    document_id: str
    kind: str
    status: str
    attempts: int
    max_attempts: int
    error: str | None = None
    enqueued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class QueueStatus(BaseModel):
    """Derived pipeline state (UPLOADED/PROCESSING/EMBEDDING/READY/FAILED)
    plus the raw job ledger rows, so a client can see both where the document
    is and why (error, attempts)."""

    document_id: str
    state: str
    process_job: JobRecord | None = None
    embed_job: JobRecord | None = None