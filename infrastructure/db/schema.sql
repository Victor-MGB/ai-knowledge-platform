-- KnowFlow schema — Day 3 (PostgreSQL 18 + pgvector 0.8.6)
--
-- Multi-tenant knowledge platform. Tenant root is `organizations`; every
-- tenant-owned table carries organization_id and every query MUST filter on
-- it. Today tenant scoping is enforced by schema + indexes; row-level
-- security policies land on the security hardening day.
--
-- Tables: organizations, users, refresh_tokens, documents, document_pages,
-- chunks, embeddings, ingestion_jobs, conversations, messages.
-- `refresh_tokens` (Day 6) is append-only; expired rows get swept.
-- `document_pages` (Day 9) is the per-page extraction layer that chunking
-- consumes; `source` keeps s3:// provenance and `section` carries the
-- structural anchor (PDF outline today, heading heuristics from Day 10).
-- `chunks` (Day 10) is the retrieval unit produced by the chunking pipeline;
-- `metadata` preserves strategy/section/source/page_range for citations.
-- `embeddings` (Day 11) stores the models' vectors per chunk in an
-- dimension-flexible `vector` column, keyed (chunk_id, model) so several
-- models coexist; the vector index is a Day-15 retrieval decision.
-- `ingestion_jobs` (Day 12) is the durable ledger for the RQ worker pool:
-- one row per (document, phase), an atomic queued -> processing claim so two
-- workers cannot double-run a phase, and an attempts/max_attempts retry budget.
-- `conversations`/`messages` (Day 19) are the Q&A chat layer: a tenant's chat
-- sessions and the append-only log of turns, where an assistant message stores
-- its citation provenance in `payload` (jsonb).

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
                    CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    last_login_at   timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, email)
);

-----------------------------------------------------------------
-- 2b. Refresh tokens (Day 6) — opaque, single-use, stored sha256-hashed,
--     revoked rather than deleted so replay of a rotated token is visible.
-----------------------------------------------------------------
CREATE TABLE refresh_tokens (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    token_hash      text NOT NULL UNIQUE,
    expires_at      timestamptz NOT NULL,
    revoked_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-----------------------------------------------------------------
-- 2c. Invitations (Day 22) — how a team grows without every person
--     registering a fresh tenant. An owner/admin invites an email with a
--     role; the invitee accepts with a password, creating their `users` row
--     in that org (a user row IS the membership). Role values mirror users.role
--     (OWNER > ADMIN > MEMBER > VIEWER). Tokens are stored sha256-hashed
--     (same convention as refresh_tokens); `status` spends accept/revoke.
-----------------------------------------------------------------
CREATE TABLE invitations (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email           text NOT NULL,
    role            text NOT NULL DEFAULT 'member'
                    CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    token_hash      text NOT NULL UNIQUE,
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'accepted', 'revoked')),
    expires_at      timestamptz NOT NULL,
    invited_by      uuid NOT NULL REFERENCES users(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    accepted_at     timestamptz
);
CREATE UNIQUE INDEX idx_invitations_org_email_pending
    ON invitations (organization_id, email)
    WHERE status = 'pending';

-----------------------------------------------------------------
-- 3. Documents — source files, their lifecycle, and who owns them
-----------------------------------------------------------------
CREATE TABLE documents (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    uploaded_by     uuid NOT NULL REFERENCES users(id),
    title           text NOT NULL,
    filename        text NOT NULL,          -- original client filename
    mime_type       text NOT NULL,          -- server-detected MIME (not client claim)
    size            bigint NOT NULL DEFAULT 0
                    CHECK (size >= 0),
    source_type     text NOT NULL DEFAULT 'md'
                    CHECK (source_type IN ('pdf', 'docx', 'md', 'html', 'txt')),
    storage_key     text,          -- object-storage path (S3/MinIO)
    status          text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'processing', 'ready', 'failed')),
    error           text,          -- failure detail from the ingestion pipeline
    embedding_status text NOT NULL DEFAULT 'none'     -- (Day 11) vectorization phase
                    CHECK (embedding_status IN ('none', 'processing', 'ready', 'failed')),
    embedding_error text,          -- failure detail from the embedding pipeline
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-----------------------------------------------------------------
-- 4. Document pages (Day 9) — one row per page of extracted text.
--    chunking consumes these; section anchors structural position.
-----------------------------------------------------------------
CREATE TABLE document_pages (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    document_id     uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_number     integer NOT NULL CHECK (page_number > 0),
    content         text NOT NULL,
    token_count     integer NOT NULL DEFAULT 0 CHECK (token_count >= 0),
    section         text,
    source          text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, page_number)
);

-----------------------------------------------------------------
-- 5. Chunks (Day 10) — the atomic units of retrieval, produced by the
--    chunking pipeline from document_pages. chunk_index orders them within
--    their document so the RAG layer can cite positions; metadata carries
--    the chunking strategy, section, source and page_range used. (The Day-3
--    v1 `chunks`/`embeddings` skeleton was dropped in migration 006.)
-----------------------------------------------------------------
CREATE TABLE chunks (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    document_id     uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    content         text NOT NULL,
    chunk_index     integer NOT NULL CHECK (chunk_index >= 0),
    page_number     integer NOT NULL CHECK (page_number > 0),
    token_count     integer NOT NULL CHECK (token_count >= 0),
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);

-----------------------------------------------------------------
-- 6. Embeddings (Day 11) — one row per (chunk, model) vector.
--    Dimension-flexible `vector` so the local hash provider (384-D) and real
--    models (text-embedding-3-small = 1536, large = 3072) coexist per chunk;
--    `dimensions` records what each row actually holds. Deletes CASCADE with
--    the chunk, the document, or the tenant. The HNSW/IVFFlat index lands
--    with retrieval (Day 15) once the model-of-record dimension is chosen.
-----------------------------------------------------------------
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

-- Day 15: HNSW cosine index over the model-of-record's vectors. HNSW needs a
-- fixed dimension, so the index is an expression over the cast to the chosen
-- 384-D, partial on the model — retrieval orders by the same cast expression
-- (`e.embedding::vector(384) <=> $q`) so pgvector matches it. A future
-- 1536-D real model gets its own index instead of fighting the schema.
CREATE INDEX idx_embeddings_hnsw_cosine
    ON embeddings USING hnsw ((embedding::vector(384)) vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE model = 'knowflow-hash-384';

-----------------------------------------------------------------
-- 7. Ingestion jobs (Day 12) — the durable ledger for background
--    processing. Uploads are enqueued (backend -> AI service), RQ workers
--    pull jobs off Redis and run them here; one job per (document, kind)
--    keeps enqueueing idempotent and gives the worker an atomic
--    queued -> processing claim (UPDATE ... RETURNING) so two workers can
--    never run the same phase twice. attempts vs max_attempts is the retry
--    budget; the backoff schedule lives in the worker (RQ enqueue_in).
-----------------------------------------------------------------
CREATE TABLE ingestion_jobs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    document_id     uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
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

-----------------------------------------------------------------
-- 8. Conversations & messages (Day 19) — the Q&A chat layer on top of RAG.
--    A conversation is a tenant's chat session; messages is the append-only
--    log of turns. An assistant message stores its citation provenance
--    (citations / refused / provider / model) in `payload` jsonb so clients
--    can render the "Sources [n] ..." block on replay without a second call.
--    `position` is a per-conversation monotonic ordinal so turn order is exact
--    even when a user+assistant pair shares one created_at instant.
-----------------------------------------------------------------
CREATE TABLE conversations (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    title           text NOT NULL,
    created_by      uuid NOT NULL REFERENCES users(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE messages (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    position        integer NOT NULL CHECK (position > 0),
    role            text NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content         text NOT NULL,
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-----------------------------------------------------------------
-- Indexes
--   1. B-tree: fast tenant lookup + FK-driven joins
--   2. trigram GIN: hybrid/fuzzy keyword search on chunk text (later)
-----------------------------------------------------------------
CREATE INDEX idx_users_org            ON users (organization_id);
CREATE INDEX idx_refresh_tokens_user  ON refresh_tokens (user_id);
CREATE INDEX idx_refresh_tokens_expires ON refresh_tokens (expires_at);
CREATE INDEX idx_documents_org        ON documents (organization_id);
CREATE INDEX idx_documents_org_status ON documents (organization_id, status);
CREATE INDEX idx_document_pages_org   ON document_pages (organization_id);
CREATE INDEX idx_document_pages_doc   ON document_pages (document_id);
CREATE INDEX idx_document_pages_doc_page ON document_pages (document_id, page_number);
CREATE INDEX idx_document_pages_content_trgm
    ON document_pages USING gin (content gin_trgm_ops);
CREATE INDEX idx_chunks_org      ON chunks (organization_id);
CREATE INDEX idx_chunks_doc      ON chunks (document_id);
CREATE INDEX idx_chunks_doc_idx  ON chunks (document_id, chunk_index);
CREATE INDEX idx_chunks_content_trgm ON chunks USING gin (content gin_trgm_ops);
CREATE INDEX idx_embeddings_organization ON embeddings (organization_id);
CREATE INDEX idx_ingestion_jobs_org     ON ingestion_jobs (organization_id);
CREATE INDEX idx_ingestion_jobs_status  ON ingestion_jobs (status);
CREATE INDEX idx_ingestion_jobs_doc     ON ingestion_jobs (document_id);
CREATE INDEX idx_conversations_org      ON conversations (organization_id);
CREATE INDEX idx_conversations_user     ON conversations (created_by);
CREATE INDEX idx_messages_org           ON messages (organization_id);
CREATE INDEX idx_messages_conv          ON messages (conversation_id, position);
CREATE INDEX idx_invitations_org        ON invitations (organization_id);
CREATE INDEX idx_invitations_expires    ON invitations (expires_at);

-----------------------------------------------------------------
-- updated_at honesty (Day 7): a BEFORE-UPDATE trigger keeps the
-- modified timestamp truthful on documents and users.
-----------------------------------------------------------------
CREATE OR REPLACE FUNCTION touch_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_documents_updated_at ON documents;
CREATE TRIGGER trg_documents_updated_at
    BEFORE UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

DROP TRIGGER IF EXISTS trg_users_updated_at ON users;
CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

DROP TRIGGER IF EXISTS trg_ingestion_jobs_updated_at ON ingestion_jobs;
CREATE TRIGGER trg_ingestion_jobs_updated_at
    BEFORE UPDATE ON ingestion_jobs
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

DROP TRIGGER IF EXISTS trg_conversations_updated_at ON conversations;
CREATE TRIGGER trg_conversations_updated_at
    BEFORE UPDATE ON conversations
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();