"""Day-12 queue facade: enqueueing (API side) + one worker run of a phase.

`QueueService` is the single object both sides talk to:

  * API calls `enqueue_process` / `enqueue_embed` — idempotent row create +
    an RQ submit. Duplicate enqueues just touch the same (document, kind) row
    instead of stacking tasks.
  * A worker calls `run_process` / `run_embed` (the RQ task functions in
    `app.queue.jobs` are thin wrappers). Each run:

      1. `claim` — the atomic queued -> processing UPDATE ... RETURNING. If it
         returns nothing another worker owns the phase (or the job was
         already finished), so the run is a no-op. Two workers can never run
         the same phase twice.
      2. delegate to the business runner (processor / embedding pipeline).
      3. classify the outcome (transient vs terminal) and settle:
         success  -> job succeeded, and a process run chains the embed phase;
         terminal -> job failed permanently (retrying won't heal it);
         transient with budget left -> requeued with exponential backoff;
         transient, budget exhausted -> job failed, document marked failed.

Retries at the *outer* level are a second safety net: the processor and the
embedding pipeline already retry their own transient failures internally. So
an outer retry mostly saves runs that crashed (worker killed, DB hiccup) or
hit a persist error — the honest edge cases the inner retries cannot cover.

Outcome contract for the RQ job result (and the CLI/tests):
  processed  process run finished, embed chained or already pending
  embedded   embed run finished
  ignored    claim lost / job already done (another worker or a prior run)
  retrying   transient failure < budget; job re-queued with a delay
  failed     terminal failure or budget exhausted; document marked failed
"""

from dataclasses import asdict, dataclass, field
from typing import Callable, Protocol

from .repository import FailSink, JobRepository
from .state import derive_state


@dataclass
class RunOutcome:
    kind: str
    document_id: str
    status: str
    detail: str = ""
    attempts: int = 0
    max_attempts: int = 0
    state: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class PhaseResult(Protocol):
    """What a worker run sees after delegating — ProcessorService.process and
    EmbeddingPipeline.embed_document both fit (status + detail)."""

    status: str
    detail: str = ""


# ProcessorService.process outcomes that no retry will fix.
_TERMINAL_PROCESS_DETAIL_MARKERS = ("pdf extraction failed",)
# EmbeddingPipeline outcomes that the inner layer already gave up on.
_TERMINAL_EMBED_DETAIL_MARKERS = ("not retryable", "retries exhausted")


class QueueService:
    def __init__(
        self,
        jobs: JobRepository,
submit: Callable[[str, float], None],
    *,
    max_attempts: int = 3,
    retry_backoff: float = 1.0,
    process_runner: Callable[[str], PhaseResult] | None = None,
    embed_runner: Callable[[str], PhaseResult] | None = None,
    fail_sink: FailSink | None = None,
):
        self._jobs = jobs
        self._submit = submit
        self._max_attempts = max(1, max_attempts)
        self._backoff = max(0.0, retry_backoff)
        self._process = process_runner
        self._embed = embed_runner
        self._fail = fail_sink

    # ---------------------------------------------------------------- enqueue

    def enqueue_process(self, document_id: str) -> tuple[object | None, str | None]:
        """Create/confirm the process job and hand it to RQ. Returns
        (job_row, doc_status_tuple); both None when the document is unknown."""
        return self._enqueue(document_id, "process")

    def enqueue_embed(self, document_id: str) -> tuple[object | None, str | None]:
        """Create/confirm the embed job and hand it to RQ. Returns
        (job_row, doc_status_tuple); both None when the document is unknown."""
        return self._enqueue(document_id, "embed")

    def status(self, document_id: str) -> object:
        """QueueStatus for the document, or None when it doesn't exist."""
        statuses = self._jobs.document_status(document_id)
        if statuses is None:
            return None
        rows = {job.kind: job for job in self._jobs.list_for_document(document_id)}
        state = derive_state(
            statuses[0],
            statuses[1],
            process_job=rows.get("process"),
            embed_job=rows.get("embed"),
        )
        from .schemas import QueueStatus

        return QueueStatus(
            document_id=str(document_id),
            state=state,
            process_job=rows.get("process"),
            embed_job=rows.get("embed"),
        )

    def _enqueue(self, document_id: str, kind: str) -> tuple[object | None, str | None]:
        job = self._jobs.ensure(document_id, kind, max_attempts=self._max_attempts)
        if job is None:
            return None, None
        # Submit even if the job already runs: the claim makes re-runs no-ops,
        # so a lost task is healed by the next one while a duplicate is safe.
        self._submit(f"app.queue.jobs.run_job_{kind}", 0, document_id)
        return job, self._jobs.document_status(document_id)

    # ------------------------------------------------------------------ worker

    def run_process(self, document_id: str) -> RunOutcome:
        job = self._jobs.claim(document_id, "process")
        if job is None:
            return RunOutcome(
                kind="process", document_id=document_id, status="ignored",
                detail="job not claimable (already running or finished)",
            )
        try:
            result = self._process(document_id)
        except Exception as exc:  # noqa: BLE001 - a crash is a retryable failure
            return self._settle(
                job, f"process job crashed: {type(exc).__name__}: {exc}", transient=True
            )
        if result.status == "ready":
            self._jobs.succeed(document_id, "process")
            return self._chain_embed(document_id, job)
        if result.status == "not_queued":
            # Someone else owns the phase (HTTP/CLI run won the claim or the
            # document moved on); nothing for us to do, no error to record.
            self._jobs.succeed(document_id, "process")
            return RunOutcome(
                kind="process", document_id=document_id, status="ignored",
                attempts=job.attempts, max_attempts=job.max_attempts,
                detail=result.detail or "claim lost to another runner",
            )
        transient = result.status == "failed" and not any(
            marker in (result.detail or "") for marker in _TERMINAL_PROCESS_DETAIL_MARKERS
        )
        return self._settle(job, result.detail or result.status, transient=transient)

    def run_embed(self, document_id: str) -> RunOutcome:
        job = self._jobs.claim(document_id, "embed")
        if job is None:
            return RunOutcome(
                kind="embed", document_id=document_id, status="ignored",
                detail="job not claimable (already running or finished)",
            )
        try:
            result = self._embed(document_id)
        except Exception as exc:  # noqa: BLE001
            return self._settle(
                job, f"embed job crashed: {type(exc).__name__}: {exc}", transient=True
            )
        if result.status == "embedded":
            self._jobs.succeed(document_id, "embed")
            return RunOutcome(
                kind="embed", document_id=document_id, status="embedded",
                attempts=job.attempts, max_attempts=job.max_attempts,
                state=self._current_state(document_id),
                detail=result.detail or "embedded",
            )
        transient = result.status == "failed" and not any(
            marker in (result.detail or "") for marker in _TERMINAL_EMBED_DETAIL_MARKERS
        )
        return self._settle(job, result.detail or result.status, transient=transient)

    # ------------------------------------------------------------------ internals

    def _chain_embed(self, document_id: str, job: object) -> RunOutcome:
        """A successful process run hands the document to the embed phase
        unless an embed job already exists (idempotent re-processes)."""
        existing = self._jobs.get(document_id, "embed")
        if existing is None:
            self._jobs.ensure(document_id, "embed", max_attempts=self._max_attempts)
            self._submit("app.queue.jobs.run_job_embed", 0, document_id)
        return RunOutcome(
            kind="process", document_id=document_id, status="processed",
            attempts=job.attempts, max_attempts=job.max_attempts,
            state=self._current_state(document_id),
            detail="processed; embed chained" if existing is None else "processed; embed already pending",
        )

    def _settle(self, job, reason: str, *, transient: bool) -> RunOutcome:
        document_id, kind = job.document_id, job.kind
        # `attempts` is this run's ordinal (claim incremented it). Budget is
        # consumed when we would need a) a retry, and b) have no attempts left.
        if transient and job.attempts < job.max_attempts:
            self._jobs.requeue(document_id, kind, reason)
            delay = self._backoff * (2 ** max(0, job.attempts - 1))
            self._submit(f"app.queue.jobs.run_job_{kind}", delay, document_id)
            return RunOutcome(
                kind=kind, document_id=document_id, status="retrying",
                attempts=job.attempts, max_attempts=job.max_attempts,
                state=self._current_state(document_id), detail=reason,
            )
        self._jobs.fail(document_id, kind, reason)
        if self._fail is not None and transient:
            # Only an *exhausted transient* failure gets its document marked.
            # Terminal business outcomes (unsupported_type, not_found,
            # not_embeddable, an inner unretryable provider error) were already
            # handled by the runner: the processor leaves an unsupported doc
            # queued on purpose (awaiting a parser), and the embedding pipeline
            # already flipped embedding_status on its own failures.
            if kind == "process":
                self._fail.mark_document_failed(document_id, reason)
            else:
                self._fail.mark_embedding_failed(document_id, reason)
        return RunOutcome(
            kind=kind, document_id=document_id, status="failed",
            attempts=job.attempts, max_attempts=job.max_attempts,
            state=self._current_state(document_id), detail=reason,
        )

    def _current_state(self, document_id: str) -> str:
        statuses = self._jobs.document_status(document_id)
        if statuses is None:
            return "FAILED"
        rows = {r.kind: r for r in self._jobs.list_for_document(document_id)}
        return derive_state(
            statuses[0], statuses[1],
            process_job=rows.get("process"), embed_job=rows.get("embed"),
        )


def build_api_queue_service(settings) -> QueueService:
    """API-side wiring: job ledger + RQ submit only (no business runners —
    workers execute phases; the API only enqueues and reports status)."""
    from rq import Queue
    from redis import Redis

    from .repository import PostgresJobRepository, ResilientPostgresConnection

    connection = ResilientPostgresConnection(settings.database_url)
    queue = Queue(settings.queue_name, connection=Redis.from_url(settings.redis_url))
    # RQ 2.x: per-job timeouts live on the queue, not the worker
    queue.default_timeout = settings.queue_worker_timeout

    def submit(func_name: str, delay: float = 0, *args: str) -> None:
        if delay <= 0:
            queue.enqueue(func_name, *args)
        else:
            from datetime import timedelta

            queue.enqueue_in(timedelta(seconds=delay), func_name, *args)

    return QueueService(
        PostgresJobRepository(connection),
        submit,
        max_attempts=settings.queue_max_attempts,
        retry_backoff=settings.queue_retry_backoff,
    )


def build_default_queue_service(settings) -> QueueService:
    """Production worker wiring: one shared DB connection (job ledger + fail
    sink) plus the business runners (each holding its own connection)."""
    from rq import Queue
    from redis import Redis

    from ..embedding import build_embedding_pipeline
    from ..processor import build_processor_service
    from .repository import PostgresFailSink, PostgresJobRepository, ResilientPostgresConnection

    connection = ResilientPostgresConnection(settings.database_url)
    queue = Queue(settings.queue_name, connection=Redis.from_url(settings.redis_url))
    # RQ 2.x: per-job timeouts live on the queue, not the worker
    queue.default_timeout = settings.queue_worker_timeout

    def submit(func_name: str, delay: float = 0, *args: str) -> None:
        if delay <= 0:
            queue.enqueue(func_name, *args)
        else:
            from datetime import timedelta

            queue.enqueue_in(timedelta(seconds=delay), func_name, *args)

    return QueueService(
        PostgresJobRepository(connection),
        submit,
        max_attempts=settings.queue_max_attempts,
        retry_backoff=settings.queue_retry_backoff,
        process_runner=build_processor_service(settings).process,
        embed_runner=build_embedding_pipeline(settings).embed_document,
        fail_sink=PostgresFailSink(connection),
    )