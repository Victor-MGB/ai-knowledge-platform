"""Derived pipeline state for a document, from the Day-12 state machine.

The user-visible lifecycle is UPLOADED -> PROCESSING -> EMBEDDING -> READY
(with FAILED reachable from any phase). It is *derived* — nothing stores a
single state column — from the two document lifecycle columns
(`documents.status` from Days 9-10, `documents.embedding_status` from Day 11)
plus the `ingestion_jobs` ledger rows (Day 12).

Rules, in priority order:
  FAILED     either lifecycle column read `failed`, or a job phase ended failed
             (a dead phase can never reach READY; re-enqueueing repairs it)
  READY      both read `ready`
  EMBEDDING  embedding run is claimed/queued (embedding_status processing, or
             an embed job processing/queued)
  PROCESSING extraction+chunking is claimed (documents.status processing)
  UPLOADED   a process job is queued but not yet running, or nothing enqueued
  READY      a ready-but-not-embedded doc with no embed work (legacy seed)
The "ready with embed queued" case is EMBEDDING by the embed-job rule; a
ready doc with no embed work at all falls through to READY.
"""

from typing import Protocol

STATE_FAILED = "FAILED"
STATE_READY = "READY"
STATE_EMBEDDING = "EMBEDDING"
STATE_PROCESSING = "PROCESSING"
STATE_UPLOADED = "UPLOADED"


class JobLike(Protocol):
    status: str


def derive_state(
    document_status: str,
    embedding_status: str,
    process_job: JobLike | None = None,
    embed_job: JobLike | None = None,
) -> str:
    if document_status == "failed" or embedding_status == "failed":
        return STATE_FAILED
    if process_job is not None and process_job.status == "failed":
        return STATE_FAILED
    if embed_job is not None and embed_job.status == "failed":
        return STATE_FAILED
    if document_status == "ready" and embedding_status == "ready":
        return STATE_READY
    if embedding_status == "processing" or (
        embed_job is not None and embed_job.status == "processing"
    ):
        return STATE_EMBEDDING
    if embed_job is not None and embed_job.status == "queued":
        return STATE_EMBEDDING
    if document_status == "processing" or (
        process_job is not None and process_job.status == "processing"
    ):
        return STATE_PROCESSING
    if process_job is not None and process_job.status == "queued":
        return STATE_UPLOADED
    return STATE_READY if document_status == "ready" else STATE_UPLOADED