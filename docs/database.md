# KnowFlow — Database

PostgreSQL 18 + **pgvector** (0.8.6) at the heart of the platform: one
transactional engine owns tenant data, the document pipeline's durable state,
**and** the vector index semantic search queries. There is no ORM —
`backend/src/database/repositories/*` run parameterized SQL; the schema is
defined in [`infrastructure/db/schema.sql`](../infrastructure/db/schema.sql) and kept current through
numbered migrations in `backend/src/database/migrations/`.

Why Postgres is the right store: [decisions.md](decisions.md) §PostgreSQL.

---

## 1. Tables

| Table | Purpose | Key constraints |
|---|---|---|
| `organizations` | tenant root | `slug` unique |
| `users` | members, credentials scoped per tenant | `UNIQUE(organization_id, email)` |
| `refresh_tokens` | opaque refresh tokens | `token_hash` unique, **stored hashed**, append-only |
| `invitations` | team invites | `token_hash` unique, partial unique index on `(org, email)` where `pending` |
| `documents` | uploaded sources | `status` ∈ `queued\|processing\|ready\|failed`; `embedding_status` ∈ `none\|processing\|ready\|failed`; `error`/`embedding_error` hold failure detail; `source_type` ∈ `pdf\|docx\|md\|html\|txt`; `storage_key` is the S3 path |
| `document_pages` | per-page extraction | `UNIQUE(document_id, page_number)`; `section` = structural anchor |
| `chunks` | retrieval unit | `UNIQUE(document_id, chunk_index)`; `metadata` jsonb (strategy / section / source / page_range); trgm index on `content` |
| `embeddings` | vector per (chunk, model) | `UNIQUE(chunk_id, model)`; dimension-flexible `vector` + `dimensions`; HNSW expression index (partial) |
| `ingestion_jobs` | durable job ledger | `UNIQUE(document_id, kind)`; `status` ∈ `queued\|processing\|succeeded\|failed`; `attempts`/`max_attempts` |
| `conversations` | chat sessions per user/org | — |
| `messages` | transcript of turns | `payload` jsonb carries the assistant turn's citation provenance |

Every tenant-owned row carries `organization_id` with a matching btree index
(`idx_*_org`). Tenancy is enforced **at the application layer** (every
repository filters on it) — deliberately, not via RLS; see the trade-off in
[decisions.md](decisions.md).

---

## 2. The vector store

`embeddings.embedding` is `vector` **without a fixed dimension**, with
`dimensions` recorded per row. That lets models coexist per chunk
(`UNIQUE(chunk_id, model)`): the local hash provider (384-D,
`knowflow-hash-384`), and future real models (e.g. OpenAI
`text-embedding-3-small` = 1536-D) live side by side with zero migration.

```sql
CREATE TABLE embeddings (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    chunk_id        uuid NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    model           text NOT NULL,
    dimensions      integer NOT NULL CHECK (dimensions > 0),
    embedding       vector NOT NULL,           -- no dimension baked in
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (chunk_id, model)
);
```

**Why an expression index?** HNSW (pgvector) requires a fixed dimension, but
the column is dimension-flexible. The answer is an index over the cast, with
a partial predicate so only the model-of-record participates:

```sql
CREATE INDEX idx_embeddings_hnsw_cosine
    ON embeddings USING hnsw ((embedding::vector(384)) vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE model = 'knowflow-hash-384';
```

Queries must order by the **same cast expression** for the planner to use it:

```sql
SELECT c.*, 1 - (e.embedding::vector(384) <=> $q) AS similarity
FROM   embeddings e JOIN chunks c ON c.id = e.chunk_id
WHERE  e.organization_id = $org
  AND  e.model = 'knowflow-hash-384'
  AND  e.embedding::vector(384) <=> $q < $min_similarity
ORDER  BY e.embedding::vector(384) <=> $q
LIMIT  $k;
```

A future 1536-D model gets its own index on `::vector(1536)` — the base
store never assumed a dimension. Conversely, embedding validation is
dimension-strict at write time: a run whose provider changes dimension or
emits non-finite values fails the document rather than polluting the index.

---

## 3. The job ledger (queue durability)

Redis holds the *tasks*; Postgres holds the *ledger*. `ingestion_jobs`
makes every pipeline phase idempotent, replayable, and claim-protected:

```sql
CREATE TABLE ingestion_jobs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    document_id     uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    kind            text NOT NULL CHECK (kind IN ('process', 'embed')),
    status          text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'processing', 'succeeded', 'failed')),
    attempts        integer NOT NULL DEFAULT 0,
    max_attempts    integer NOT NULL DEFAULT 3,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, kind)
);
```

- **Claim** = `UPDATE ingestion_jobs SET status='processing' WHERE … AND status='queued' RETURNING …` — atomic, so two workers can never double-run a phase. A lost claim surfaces as outcome `ignored`, not a corrupt run.
- **Retries** = transient failures requeue with exponential backoff while `attempts < max_attempts`; terminal markers (`pdf extraction failed`, retries exhausted) fail fast — some failures never heal.
- Why Redis at all if Postgres tracks everything: [decisions.md](decisions.md) §Redis.

---

## 4. Transactions that keep data honest

Two invariants hold as **same-transaction flips**, so a document can never be
half-built:

1. **Pages + chunks + `status='ready'`** commit together at the end of the
   process phase (`UNIQUE(document_id, chunk_index)` makes repairs idempotent).
2. **Vectors + `embedding_status='ready'`** commit together; a failed embed
   run marks itself `failed` and may be re-run (upsert on
   `UNIQUE(chunk_id, model)`), while an in-flight or already-done run is
   refused with `409`.

Cross-cutting housekeeping: a trigger keeps `updated_at` current (migration
`003`); `refresh_tokens` is append-only with a sweeper for expired rows;
`documents.deletes` cascade to pages/chunks/embeddings/jobs via `ON DELETE
CASCADE`.

---

## 5. Migrations

Numbered, additive, runnable. `db/schema.sql` is the "from scratch in one
file" baseline (kept in sync so tests and seed can build a fresh DB); the
migrations keep *existing* databases current without a destructive rebuild.

| Mig | Change |
|---|---|
| `002_auth` | refresh tokens |
| `003_schema_hygiene` | `updated_at` trigger wiring, constraint fixes |
| `004_document_upload` | `documents` + storage columns |
| `005_document_pages` | per-page extraction layer |
| `006_chunks` | retrieval units (retires the Day-3 `chunks`/`embeddings` skeleton) |
| `007_embeddings` | dimension-flexible vector rows + validation |
| `008_ingestion_jobs` | durable queue ledger + claim semantics |
| `009_retrieval_hnsw` | HNSW expression index over model-of-record |
| `010_conversations` | chat sessions + messages + citation payload |
| `011_organizations` | invitations + roles |

---

## 6. Observation & operations

- **Search path perf**: HNSW for vectors, btree for org/status filters, trgm
  for chunk-content text matching; the composite org/status index keeps
  document list queries tenant-scoped.
- **Seed** (`infrastructure/db/seed.py`): resets tenant demo data to a known state —
  used by the integration tests and the eval harness; never run against prod.
- **Availability**: if Postgres is down, the status API reports `degraded`
  (`/health`), worker/queue phases fail loudly into the ledger, and nothing
  writes partial state.