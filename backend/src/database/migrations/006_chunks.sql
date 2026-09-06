-- Day 10 - chunked text for retrieval (migration for the live Day-3 database).
--
-- Day 9 produced one row per page of extracted text (document_pages). This
-- adds the chunking layer: the retrieval unit. A chunk is text that was
-- traversed together by a chunking strategy, preserving the citation chain:
-- document_id, page_number (the page the chunk starts on) and `metadata`
-- (strategy, section, source, page_range) so a hit can be cited back to the
-- page and the source object. chunk_index is the ordinal within the document;
-- UNIQUE(document_id, chunk_index) is the repair/idempotency key.
--
-- The Day-3 v1 skeleton (empty `chunks` with `seq` + the hand-seeded
-- `embeddings` demo table) is dropped here: the real extraction->chunking
-- pipeline replaces both, and Day 11 rebuils embeddings against this table.

DROP TABLE IF EXISTS public.embeddings;
DROP TABLE IF EXISTS public.chunks;

CREATE TABLE public.chunks (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES public.organizations (id) ON DELETE CASCADE,
    document_id     uuid NOT NULL REFERENCES public.documents (id) ON DELETE CASCADE,
    content         text NOT NULL,
    chunk_index     integer NOT NULL CHECK (chunk_index >= 0),
    page_number     integer NOT NULL CHECK (page_number > 0),
    token_count     integer NOT NULL CHECK (token_count >= 0),
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_org      ON public.chunks (organization_id);
CREATE INDEX IF NOT EXISTS idx_chunks_doc      ON public.chunks (document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_doc_idx  ON public.chunks (document_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_chunks_content_trgm
    ON public.chunks USING gin (content gin_trgm_ops);