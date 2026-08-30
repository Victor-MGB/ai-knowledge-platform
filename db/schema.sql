-- KnowFlow schema — Day 3 (PostgreSQL 18 + pgvector 0.8.6)
--
-- Multi-tenant knowledge platform. Tenant root is `organizations`; every
-- tenant-owned table carries organization_id and every query MUST filter on
-- it. Today tenant scoping is enforced by schema + indexes; row-level
-- security policies land on the security hardening day.
--
-- Tables: organizations, users, documents, chunks, embeddings.
-- Embeddings live in their OWN table (not a column on chunks) so that:
--   * chunk text (hot, fetched often) stays small; vectors are cold
--   * multiple embedding models can coexist per chunk (UNIQUE chunk_id+model)
--   * re-embedding after a model upgrade is a pure data migration

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-----------------------------------------------------------------
-- 1. Organizations = tenants
-----------------------------------------------------------------
CREATE TABLE organizations (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name       text NOT NULL,
    slug       text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

-----------------------------------------------------------------
-- 2. Users — credentials are scoped per tenant, so email uniqueness
--    is (organization_id, email), NOT email alone.
-----------------------------------------------------------------
CREATE TABLE users (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email           text NOT NULL,
    password_hash   text NOT NULL,
    role            text NOT NULL DEFAULT 'member'
                    CHECK (role IN ('owner', 'admin', 'member')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, email)
);

-----------------------------------------------------------------
-- 3. Documents — source files, their lifecycle, and who owns them
-----------------------------------------------------------------
CREATE TABLE documents (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    uploaded_by     uuid NOT NULL REFERENCES users(id),
    title           text NOT NULL,
    source_type     text NOT NULL DEFAULT 'md'
                    CHECK (source_type IN ('pdf', 'md', 'html', 'txt')),
    storage_key     text,          -- object-storage path (S3/MinIO)
    status          text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'processing', 'ready', 'failed')),
    error           text,          -- failure detail from the ingestion pipeline
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-----------------------------------------------------------------
-- 4. Chunks — the atomic units of retrieval. seq orders them
--    within their document so the RAG layer can cite positions.
-----------------------------------------------------------------
CREATE TABLE chunks (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    document_id     uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq             integer NOT NULL,
    content         text NOT NULL CHECK (length(content) > 0),
    token_count     integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, seq)
);

-----------------------------------------------------------------
-- 5. Embeddings — vector(384) matches all-MiniLM-L6-v2, our canonical
--    open embedding model. Swapping models = migration of the column
--    dimension + a re-embed. `<=>` is cosine distance, our metric.
-----------------------------------------------------------------
CREATE TABLE embeddings (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    chunk_id        uuid NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    model           text NOT NULL,
    embedding       vector(384) NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (chunk_id, model)
);

-----------------------------------------------------------------
-- Indexes
--   1. B-tree: fast tenant lookup + FK-driven joins
--   2. HNSW: approximate nearest-neighbor over the vectors
--   3. trigram GIN: hybrid/fuzzy keyword search on chunk text (later)
-----------------------------------------------------------------
CREATE INDEX idx_users_org           ON users (organization_id);
CREATE INDEX idx_documents_org       ON documents (organization_id);
CREATE INDEX idx_documents_org_status ON documents (organization_id, status);
CREATE INDEX idx_chunks_org_doc      ON chunks (organization_id, document_id);
CREATE INDEX idx_chunks_content_trgm ON chunks USING gin (content gin_trgm_ops);
CREATE INDEX idx_embeddings_org_model ON embeddings (organization_id, model);

-- HNSW approximate nearest-neighbor. vector_cosine_ops = index over <=>
-- distance; m/ef_construction trade build speed vs recall. pgvector also
-- offers IVFFlat as the older, slower-to-build alternative.
CREATE INDEX idx_embeddings_hnsw_cosine
    ON embeddings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);