-- Day 12 - background processing: a durable job ledger for the RQ worker pool.
--
-- The upload -> process -> embed chain has been synchronous/trigger-based since
-- Day 9: a CLI or HTTP call drove each phase and nothing tracked "work to do".
-- Day 12 turns that into a queue: uploads are enqueued (backend calls the AI
-- service), RQ workers pick jobs off Redis, and `ingestion_jobs` is the source
-- of truth for what ran, when, and how many times it was attempted.
--
-- Design notes:
--   * one row per (document_id, kind) keeps enqueueing idempotent — a
--     re-enqueue just touches the row instead of stacking duplicate jobs;
--   * status queued -> processing -> succeeded inherits the claim discipline:
--     `claim` flips queued -> processing with UPDATE ... RETURNING so two
--     workers cannot double-run the same phase;
--   * attempts/max_attempts is the retry budget. The retry schedule itself
--     lives in the worker (exponential backoff via RQ enqueue_in);
--   * enqueued_at/started_at/finished_at/error give the API and the operator
--     an honest audit trail per phase;
--   * `kind` stays open (process | embed today) so new phases extend the
--     CHECK without a migration.
--
-- The RQ job in Redis is transient; this table is what survives a restart.
-- Redis being down is not fatal: the backend swallows enqueue failures and
-- leaves the document `queued`, and `documents.status` still shows exactly
-- how far the pipeline got, so a later sweep can re-enqueue stragglers.

CREATE TABLE public.ingestion_jobs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES public.organizations (id) ON DELETE CASCADE,
    document_id     uuid NOT NULL REFERENCES public.documents (id) ON DELETE CASCADE,
    kind            text NOT NULL CHECK (kind IN ('process', 'embed')),
    status          text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'processing', 'succeeded', 'failed')),
    attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts    integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    error           text,
    enqueued_at     timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_org     ON public.ingestion_jobs (organization_id);
CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_status  ON public.ingestion_jobs (status);
CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_doc     ON public.ingestion_jobs (document_id);

DROP TRIGGER IF EXISTS trg_ingestion_jobs_updated_at ON public.ingestion_jobs;
CREATE TRIGGER trg_ingestion_jobs_updated_at
    BEFORE UPDATE ON public.ingestion_jobs
    FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();