-- Day 11: production embeddings over Day-10 chunks.
--
-- The Day-3 `embeddings` demo (hand-seeded vectors next to the v1 chunks
-- skeleton) was retired by migration 006; this is the real rebuild. One row
-- per (chunk_id, model) so several embedding models can coexist per chunk and
-- be compared — the multi-model plan from Day 3, now fed by the pipeline.
--
-- `embedding` is dimension-flexible (`vector` with no fixed length) on
-- purpose: the local hash provider is 384-D while real providers differ
-- (text-embedding-3-small = 1536, large = 3072), and they must not fight the
-- schema. A fixed HNSW/IVFFlat index is a Day-15 retrieval concern; by then
-- the model-of-record dimension is decided and the index is built on the
-- chosen column.
--
-- `embedding_status` tracks the *phase*, separate from `status` so ingestion
-- (queued -> ready, Days 9-10) and vectorization (none -> ready) each have
-- an honest lifecycle: retrieval (Day 15) will pick up exactly the documents
-- whose status = ready AND embedding_status = ready.

ALTER TABLE documents
    ADD COLUMN embedding_status text NOT NULL DEFAULT 'none'
        CHECK (embedding_status IN ('none', 'processing', 'ready', 'failed')),
    ADD COLUMN embedding_error text;

CREATE TABLE embeddings (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    chunk_id        uuid NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    model           text NOT NULL,
    dimensions      integer NOT NULL CHECK (dimensions > 0),
    embedding       vector NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (chunk_id, model)
);

CREATE INDEX idx_embeddings_organization ON embeddings (organization_id);