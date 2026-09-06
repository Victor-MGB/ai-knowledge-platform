"""Day 12 (unit): derived pipeline state + worker orchestration against fakes.

`derive_state` maps (documents.status, embedding_status, job rows) onto the
five user-visible states; `QueueService.run_process`/`run_embed` exercise the
claim -> run -> classify -> settle loop and its retry/backoff/chain decisions
without Redis or Postgres (a fake job ledger + a recording submit function).
"""

from dataclasses import dataclass, field

import pytest

from app.queue.schemas import JobRecord
from app.queue.service import QueueService
from app.queue.state import derive_state


@dataclass
class FakeJob:
    document_id: str
    kind: str
    status: str = "queued"
    attempts: int = 0
    max_attempts: int = 3
    error: str | None = None
    detail: str = ""


def to_record(job: FakeJob) -> JobRecord:
    return JobRecord(
        job_id=f"{job.document_id}-{job.kind}",
        organization_id="org-1",
        document_id=job.document_id,
        kind=job.kind,
        status=job.status,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        error=job.error,
    )


@dataclass
class FakeRepo:
    max_attempts: int = 3
    rows: dict[str, FakeJob] = field(default_factory=dict)
    statuses: dict[str, tuple[str, str]] = field(default_factory=dict)

    def _key(self, document_id, kind):
        return f"{document_id}:{kind}"

    def ensure(self, document_id, kind, *, max_attempts=None):
        key = self._key(document_id, kind)
        if key not in self.rows:
            if document_id not in self.statuses:
                return None  # unknown document
            self.rows[key] = FakeJob(
                document_id, kind, max_attempts=max_attempts or self.max_attempts
            )
        return self.get(document_id, kind)

    def get(self, document_id, kind):
        row = self.rows.get(self._key(document_id, kind))
        return to_record(row) if row else None

    def list_for_document(self, document_id):
        return [
            to_record(j) for j in self.rows.values() if j.document_id == document_id
        ]

    def document_status(self, document_id):
        return self.statuses.get(document_id)

    def claim(self, document_id, kind):
        job = self.rows.get(self._key(document_id, kind))
        if job is None or job.status != "queued":
            return None
        job.status = "processing"
        job.attempts += 1
        return to_record(job)

    def succeed(self, document_id, kind):
        self.rows[self._key(document_id, kind)].status = "succeeded"

    def fail(self, document_id, kind, error):
        job = self.rows[self._key(document_id, kind)]
        job.status = "failed"
        job.error = error

    def requeue(self, document_id, kind, error):
        job = self.rows.get(self._key(document_id, kind))
        if job is None or job.status not in ("processing", "failed"):
            return None
        job.status = "queued"
        job.error = error
        return to_record(job)

    def delete(self, document_id, kind):
        self.rows.pop(self._key(document_id, kind), None)


class Recorder:
    def __init__(self):
        self.submits: list[tuple[str, float, tuple]] = []

    def submit(self, func_name, delay=0.0, *args):
        self.submits.append((func_name, delay, args))


@dataclass
class FakeResult:
    status: str
    detail: str = ""


class FakeFailSink:
    def __init__(self):
        self.doc_failures: list[tuple[str, str]] = []
        self.embed_failures: list[tuple[str, str]] = []

    def mark_document_failed(self, document_id, error):
        self.doc_failures.append((document_id, error))

    def mark_embedding_failed(self, document_id, error):
        self.embed_failures.append((document_id, error))


def fresh_doc(repo: FakeRepo, doc_id="doc-1", status="queued", embedding_status="none"):
    repo.statuses[doc_id] = (status, embedding_status)


def queued(repo: FakeRepo, doc_id: str, kind: str):
    """A worker run models reality: the job exists because enqueue created it."""
    return repo.ensure(doc_id, kind, max_attempts=repo.max_attempts)


def make_service(repo, recorder, *, process=None, embed=None, backoff=1.0, max_attempts=None):
    return QueueService(
        repo,
        recorder.submit,
        max_attempts=max_attempts or repo.max_attempts,
        retry_backoff=backoff,
        process_runner=process,
        embed_runner=embed,
        fail_sink=FakeFailSink(),
    )


class TestDeriveState:
    def test_fresh_upload_is_uploaded(self):
        assert derive_state("queued", "none") == "UPLOADED"

    def test_process_job_queued_stays_uploaded(self):
        assert derive_state("queued", "none", process_job=FakeJob("d", "process", status="queued")) == "UPLOADED"

    def test_process_claimed_is_processing(self):
        assert derive_state("processing", "none", process_job=FakeJob("d", "process", status="processing")) == "PROCESSING"

    def test_process_run_is_processing(self):
        assert derive_state("processing", "none") == "PROCESSING"

    def test_embed_job_queued_is_embedding(self):
        assert (
            derive_state("ready", "none", embed_job=FakeJob("d", "embed", status="queued"))
            == "EMBEDDING"
        )

    def test_embed_claimed_is_embedding(self):
        assert (
            derive_state("ready", "processing") == "EMBEDDING"
            and derive_state("ready", "none", embed_job=FakeJob("d", "embed", status="processing"))
            == "EMBEDDING"
        )

    def test_both_ready_is_ready(self):
        assert derive_state("ready", "ready") == "READY"

    def test_legacy_ready_with_no_embed_work_is_ready(self):
        assert derive_state("ready", "none") == "READY"

    def test_failed_status_is_failed(self):
        assert derive_state("failed", "none") == "FAILED"
        assert derive_state("ready", "failed") == "FAILED"

    def test_failed_job_is_failed_even_if_document_is_queued(self):
        # an unsupported doc: the phase is dead (awaiting a parser) and must
        # surface as FAILED rather than silently UPLOADED
        assert (
            derive_state("queued", "none", process_job=FakeJob("d", "process", status="failed"))
            == "FAILED"
        )
        assert (
            derive_state("ready", "ready", embed_job=FakeJob("d", "embed", status="failed"))
            == "FAILED"
        )
        # a failed phase wins over a pending sibling phase
        assert (
            derive_state(
                "queued", "none",
                process_job=FakeJob("d", "process", status="failed"),
                embed_job=FakeJob("d", "embed", status="queued"),
            )
            == "FAILED"
        )

    def test_succeeded_jobs_do_not_block_ready(self):
        assert (
            derive_state(
                "ready", "ready",
                process_job=FakeJob("d", "process", status="succeeded"),
                embed_job=FakeJob("d", "embed", status="succeeded"),
            )
            == "READY"
        )


class TestEnqueue:
    def test_enqueue_process_creates_job_and_submits(self):
        repo = FakeRepo()
        fresh_doc(repo)
        recorder = Recorder()
        svc = make_service(repo, recorder)
        job, statuses = svc.enqueue_process("doc-1")
        assert job is not None and job.status == "queued"
        assert statuses == ("queued", "none")
        assert recorder.submits == [("app.queue.jobs.run_job_process", 0, ("doc-1",))]

    def test_enqueue_is_idempotent_no_duplicate_submits(self):
        repo = FakeRepo()
        fresh_doc(repo)
        recorder = Recorder()
        svc = make_service(repo, recorder)
        svc.enqueue_process("doc-1")
        svc.enqueue_process("doc-1")
        assert len(repo.list_for_document("doc-1")) == 1
        assert len([s for s in recorder.submits if s[0] == "app.queue.jobs.run_job_process"]) == 2
        assert repo.list_for_document("doc-1")[0].status == "queued"

    def test_enqueue_unknown_document_is_none(self):
        repo = FakeRepo()
        recorder = Recorder()
        svc = make_service(repo, recorder)
        assert svc.enqueue_process("nope") == (None, None)
        assert recorder.submits == []

    def test_embeds_use_the_embed_task(self):
        repo = FakeRepo()
        fresh_doc(repo)
        recorder = Recorder()
        svc = make_service(repo, recorder)
        svc.enqueue_embed("doc-1")
        assert recorder.submits == [("app.queue.jobs.run_job_embed", 0, ("doc-1",))]


class TestStatus:
    def test_status_reports_states_across_the_lifecycle(self):
        repo = FakeRepo()
        fresh_doc(repo)
        recorder = Recorder()
        svc = make_service(repo, recorder)

        assert svc.status("doc-1").state == "UPLOADED"
        svc.enqueue_process("doc-1")
        assert svc.status("doc-1").state == "UPLOADED"
        repo.statuses["doc-1"] = ("processing", "none")
        assert svc.status("doc-1").state == "PROCESSING"
        repo.statuses["doc-1"] = ("ready", "none")
        svc.enqueue_embed("doc-1")
        assert svc.status("doc-1").state == "EMBEDDING"
        repo.statuses["doc-1"] = ("ready", "ready")
        repo.get("doc-1", "process").status = "succeeded"
        repo.get("doc-1", "embed").status = "succeeded"
        assert svc.status("doc-1").state == "READY"

    def test_status_unknown_document_is_none(self):
        repo = FakeRepo()
        assert make_service(repo, Recorder()).status("nope") is None


class TestRunProcess:
    def test_success_chains_embed(self):
        repo = FakeRepo()
        fresh_doc(repo)
        queued(repo, "doc-1", "process")
        recorder = Recorder()
        svc = make_service(repo, recorder, process=lambda d: FakeResult(status="ready"))
        outcome = svc.run_process("doc-1")
        assert outcome.status == "processed"
        assert repo.get("doc-1", "process").status == "succeeded"
        assert recorder.submits[-1][0] == "app.queue.jobs.run_job_embed"
        assert repo.get("doc-1", "embed") is not None

    def test_success_does_not_stack_a_second_embed_job(self):
        repo = FakeRepo()
        fresh_doc(repo)
        queued(repo, "doc-1", "process")
        repo.rows["doc-1:embed"] = FakeJob("doc-1", "embed", status="queued")
        recorder = Recorder()
        svc = make_service(repo, recorder, process=lambda d: FakeResult(status="ready"))
        svc.run_process("doc-1")
        assert recorder.submits == []  # embed already pending, nothing submitted

    def test_unclaimable_run_is_ignored(self):
        repo = FakeRepo()
        fresh_doc(repo)
        repo.rows["doc-1:process"] = FakeJob("doc-1", "process", status="processing")
        svc = make_service(repo, Recorder(), process=lambda d: FakeResult("ready"))
        outcome = svc.run_process("doc-1")
        assert outcome.status == "ignored"

    def test_not_found_is_terminal(self):
        repo = FakeRepo()
        fresh_doc(repo)
        queued(repo, "doc-1", "process")
        recorder = Recorder()
        svc = make_service(repo, recorder, process=lambda d: FakeResult(status="not_found", detail="gone"))
        outcome = svc.run_process("doc-1")
        assert outcome.status == "failed"
        assert repo.get("doc-1", "process").status == "failed"
        assert recorder.submits == []  # no retry scheduled

    def test_unsupported_type_is_terminal(self):
        repo = FakeRepo()
        fresh_doc(repo)
        queued(repo, "doc-1", "process")
        recorder = Recorder()
        svc = make_service(
            repo, recorder, process=lambda d: FakeResult(status="unsupported_type", detail="no parser for source_type=md")
        )
        assert svc.run_process("doc-1").status == "failed"
        assert recorder.submits == []

    def test_transient_failure_requeues_with_backoff(self):
        repo = FakeRepo()
        fresh_doc(repo)
        queued(repo, "doc-1", "process")
        recorder = Recorder()
        svc = make_service(
            repo, recorder, process=lambda d: FakeResult(status="failed", detail="processor error: TimeoutError: boom"), backoff=1.0
        )
        outcome = svc.run_process("doc-1")
        assert outcome.status == "retrying"
        assert repo.get("doc-1", "process").status == "queued"
        assert recorder.submits == [("app.queue.jobs.run_job_process", 1.0, ("doc-1",))]

    def test_backoff_doubles_per_attempt(self):
        repo = FakeRepo()
        fresh_doc(repo)
        queued(repo, "doc-1", "process")
        recorder = Recorder()
        svc = make_service(
            repo, recorder,
            process=lambda d: FakeResult(status="failed", detail="processor error: TimeoutError: boom"),
            backoff=2.0,
        )
        svc.run_process("doc-1")
        assert recorder.submits[0][1] == 2.0
        svc.run_process("doc-1")  # second run
        assert recorder.submits[1][1] == 4.0

    def test_budget_exhausted_fails_and_marks_document(self):
        repo = FakeRepo(max_attempts=2)
        fresh_doc(repo)
        queued(repo, "doc-1", "process")
        recorder = Recorder()
        svc = make_service(
            repo, recorder,
            process=lambda d: FakeResult(status="failed", detail="processor error: TimeoutError: boom"),
            max_attempts=2,
        )
        assert svc.run_process("doc-1").status == "retrying"
        assert svc.run_process("doc-1").status == "failed"  # attempts now 2 == budget
        assert recorder.submits == [("app.queue.jobs.run_job_process", 1.0, ("doc-1",))]  # no 3rd submit
        assert svc._fail.doc_failures  # delta: document marked failed on exhaustion

    def test_crash_is_retried_then_fails_document(self):
        repo = FakeRepo(max_attempts=1)
        fresh_doc(repo)
        queued(repo, "doc-1", "process")
        recorder = Recorder()

        def explode(doc_id):
            raise RuntimeError("worker vanished")

        svc = make_service(repo, recorder, process=explode, max_attempts=1)
        outcome = svc.run_process("doc-1")
        assert outcome.status == "failed"
        assert "worker vanished" in outcome.detail
        assert repo.get("doc-1", "process").status == "failed"

    def test_pdf_extraction_failure_is_terminal(self):
        repo = FakeRepo()
        fresh_doc(repo)
        queued(repo, "doc-1", "process")
        recorder = Recorder()
        svc = make_service(
            repo, recorder,
            process=lambda d: FakeResult(status="failed", detail="pdf extraction failed: broken bytes"),
        )
        assert svc.run_process("doc-1").status == "failed"
        assert recorder.submits == []

    def test_not_queued_is_a_noop(self):
        repo = FakeRepo()
        fresh_doc(repo)
        queued(repo, "doc-1", "process")
        recorder = Recorder()
        svc = make_service(
            repo, recorder, process=lambda d: FakeResult(status="not_queued", detail="already processing")
        )
        assert svc.run_process("doc-1").status == "ignored"
        assert repo.get("doc-1", "process").status == "succeeded"


class TestRunEmbed:
    def test_success_succeeds_the_job(self):
        repo = FakeRepo()
        fresh_doc(repo, status="ready")
        queued(repo, "doc-1", "embed")
        recorder = Recorder()
        svc = make_service(repo, recorder, embed=lambda d: FakeResult(status="embedded", detail="embedded 2 chunks"))
        outcome = svc.run_embed("doc-1")
        assert outcome.status == "embedded"
        assert repo.get("doc-1", "embed").status == "succeeded"

    def test_not_embeddable_is_terminal(self):
        repo = FakeRepo()
        fresh_doc(repo, status="ready")
        queued(repo, "doc-1", "embed")
        recorder = Recorder()
        svc = make_service(
            repo, recorder, embed=lambda d: FakeResult(status="not_embeddable", detail="document status is queued, not ready")
        )
        assert svc.run_embed("doc-1").status == "failed"
        assert recorder.submits == []

    def test_inner_unretryable_failure_is_terminal(self):
        repo = FakeRepo()
        fresh_doc(repo, status="ready")
        queued(repo, "doc-1", "embed")
        recorder = Recorder()
        svc = make_service(
            repo, recorder,
            embed=lambda d: FakeResult(status="failed", detail="embedding batch failed (not retryable, attempt 1): MissingApiKey"),
        )
        assert svc.run_embed("doc-1").status == "failed"
        assert recorder.submits == []

    def test_transient_embed_failure_retries(self):
        repo = FakeRepo()
        fresh_doc(repo, status="ready")
        queued(repo, "doc-1", "embed")
        recorder = Recorder()
        svc = make_service(
            repo, recorder,
            embed=lambda d: FakeResult(status="failed", detail="could not persist embeddings: OperationalError: conn reset"),
        )
        outcome = svc.run_embed("doc-1")
        assert outcome.status == "retrying"
        assert recorder.submits == [("app.queue.jobs.run_job_embed", 1.0, ("doc-1",))]

    def test_crash_marks_embedding_failed(self):
        repo = FakeRepo(max_attempts=1)
        fresh_doc(repo, status="ready")
        queued(repo, "doc-1", "embed")
        recorder = Recorder()

        def explode(doc_id):
            raise ConnectionError("redis gone")

        svc = make_service(repo, recorder, embed=explode, max_attempts=1)
        assert svc.run_embed("doc-1").status == "failed"
        assert svc._fail.embed_failures  # delta: embedding_status marked failed