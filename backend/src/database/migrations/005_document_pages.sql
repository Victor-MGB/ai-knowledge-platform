-- Day 9 - per-page extracted text (migration for the live Day-3 database).
--
-- The upload path only records file metadata (Day 8). This adds the extraction
-- layer: one row per page of extracted text, ordered by page_number, owned by
-- the same tenant graph so table-level scoping conventions hold. `section`
-- carries the PDF outline/bookmark title that anchors the page (NULL until a
-- structural section is derivable - heading heuristics are Day-10 chunking);
-- `source` preserves object-level provenance for citations (e.g.
-- s3://knowflow/<org>/<uuid>.pdf). token_count is a deterministic estimate so
-- retrieval can budget context without joining embeddings.

CREATE TABLE IF NOT EXISTS public.document_pages (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES public.organizations (id) ON DELETE CASCADE,
    document_id     uuid NOT NULL REFERENCES public.documents (id) ON DELETE CASCADE,
    page_number     integer NOT NULL CHECK (page_number > 0),
    content         text NOT NULL,
    token_count     integer NOT NULL DEFAULT 0 CHECK (token_count >= 0),
    section         text,
    source          text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, page_number)
);

CREATE INDEX IF NOT EXISTS idx_document_pages_org ON public.document_pages (organization_id);
CREATE INDEX IF NOT EXISTS idx_document_pages_doc ON public.document_pages (document_id);
CREATE INDEX IF NOT EXISTS idx_document_pages_doc_page
    ON public.document_pages (document_id, page_number);
CREATE INDEX IF NOT EXISTS idx_document_pages_content_trgm
    ON public.document_pages USING gin (content gin_trgm_ops);