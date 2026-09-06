# KnowFlow — Architecture

A multi-tenant, RAG-powered knowledge platform. Users upload documents, an
async pipeline turns them into queryable chunks + embeddings in Postgres,
and a retrieval-augmented generation layer answers questions with citations
over their own corpus.

This file is the as-built system overview (draw-the-picture interview
material). The daily "why we built what" record lives in
[`docs/build-log.md`](build-log.md); deep dives: [API](api.md) ·
[Database](database.md) · [RAG](rag.md) · [Security](security.md) ·
[Decisions](decisions.md) · [Evaluation](evaluation.md).

---

## 1. Components

| Component | Tech | Role | Dev port | In-compose |
|---|---|---|---|---|
| **frontend** | React + Vite + TS, nginx | SPA (chat UI, uploads); nginx serves it and proxies API + SSE | — | 80 |
| **backend** | Node.js + Fastify (TS) | Auth, validation, document API, search/RAG orchestration, chat sessions | 3000 | 3000 |
| **ai-service** | Python + FastAPI | Embeddings, LLM completions, RAG generation, document processing/embedding phases | 8000 | 8000 |
| **worker** | Python + RQ `SimpleWorker` | Consumes the Redis queue, drives process → embed phases | — | 8000/8001* |
| **postgres** | PostgreSQL 18 + pgvector | Tenant data: users, orgs, documents, chunks, vectors, jobs | 5434 | 5432 |
| **redis** | Redis 7 | RQ queue + job registries | 6375 | 6379 |
| **minio** | MinIO (S3 API) | Object store for source documents | 9000 | 9000 |
| **prometheus** | Prometheus | Time-series metrics; scrapes backend/ai-service/worker | — | 9090 |
| **grafana** | Grafana | Provisioned dashboard (anonymous read) | — | 3001 |

\* The worker process exposes its own Prometheus endpoint on 8001.

There is no separate "billing" or "worker pool orchestrator" — the whole
product is this one compose file.

---

## 2. Logical request flows

### Registration / login
```
POST /auth/register ─► backend ─► organizations + users (bcrypt hash)
POST /auth/login    ─► backend ─► access (JWT HS256, ~15m) + opaque refresh
                                 (SHA-256 hash stored, rotated on use)
```
Access token carries `sub`, `org`, `role`, `typ: "access"`. Every protected
route in front of nginx gets the same JWT; no cookies, no session store.

### Ingestion (upload → retrieval-ready)
```
POST /api/v1/documents  (multipart)
  backend: validate extension + magic bytes + page/size limits
  nginx ─► backend ─► minio          object {org}/{uuid}.{ext}
                     ─► postgres      documents row (status=queued)
                     ─► ai-service    best-effort enqueue  (process job row)
  ai-service ─► redis   app.queue.jobs.run_job_process
  worker spins:
    claim   : postgres ingestion_jobs queued→processing (atomic UPDATE…RETURNING)
    extract : minio → pypdf → pages
    chunk   : chunkers (paragraph default)
    commit  : pages + chunks + status=ready in ONE transaction
    chain   : enqueue run_job_embed
    embed   : ready doc chunks → hash/openai vectors (batch 64, retries)
    commit  : vectors + embedding_status=ready in ONE transaction
```
Failure semantics: transient failures retry with exponential backoff up to 3
attempts; terminal failures (`pdf extraction failed`, retries exhausted) mark
the job failed and the document's error column; the document stays visibly
`queued`/`failed` in the status API — never silently dropped.

### Search
```
POST /api/v1/search  { query, limit(≤20), minSimilarity, sourceType, metadata }
  backend ─► ai-service /v1/embeddings  (embeds the question)
  backend ─► postgres HNSW cosine top-K, filtered by tenant + uploaded_by,
            optional pre-filters {sourceType, metadata, minSimilarity}
```

### RAG answer (non-streaming)
```
POST /api/v1/rag/generate { question, limit, maxContextTokens, minScore, … }
  backend: retrieve tenant chunks → build token-budgeted context
  backend ─► ai-service /v1/rag/generate
              provider: extract (verbatim, refusal-capable) | openai
  ✓ answer text with inline [n] citations + evidence + refusal flag
```

### RAG streaming (the chat experience)
```
POST /api/v1/rag/generate/stream ─► backend ─► ai-service /v1/rag/generate/stream
  ai-service emits SSE:  data: {"delta": "…"}  …  data: {"done": …}  data: [DONE]
  backend proxies verbatim via reply.raw (no buffering)
  nginx must flush (SSE header pass-through)
```

### Chat sessions
```
Conversations are per tenant: POST/GET /api/v1/conversations, POST
/api/v1/conversations/:id/messages (streams a RAG answer and appends the
turn). Assistant turns store their citation provenance in `payload` (jsonb).
```

---

## 3. Data model (summary)

All tenant tables carry `organization_id`; every query filters on it
(app-level tenancy — see [decisions](decisions.md)).

| Table | Purpose | Notable constraints |
|---|---|---|
| `organizations` | tenant root | slug unique |
| `users` | members; per-org credentials | email unique per org |
| `refresh_tokens` | opaque refresh tokens | stored **hashed**, append-only, sweeper |
| `invitations` | invite tokens with expiry | unique (org, email, pending) |
| `documents` | uploaded sources + status | status, embedding_status, error |
| `document_pages` | per-page extraction | page_number, section anchor |
| `chunks` | retrieval unit | `UNIQUE(document_id, chunk_index)`, trgm idx |
| `embeddings` | vectors per (chunk, model) | `UNIQUE(chunk_id, model)`, dim-flexible |
| `ingestion_jobs` | durable job ledger | `UNIQUE(document_id, kind)`, attempts/max |
| `conversations` / `messages` | chat sessions + transcripts | citations in message payload jsonb |

Migrations are numbered `002…011` in `backend/src/database/migrations/`,
applied as additive SQL; there is no ORM — repositories run parameterized SQL.

Full detail: [database.md](database.md).

---

## 4. Why this shape (one-line each)

Full rationale: [decisions.md](decisions.md).

- **PostgreSQL + pgvector** — one transactional store for relational + vector
  needs; HNSW cosine index inside the same engine that owns the join graph.
- **Async processing** — extraction/embedding are slow and provider-bound;
  a queue decouples upload latency from pipeline work and gives retries,
  idempotency, and a durable ledger.
- **Two services (Node + Python)** — the AI/ML ecosystem (pypdf, provider
  SDKs, pgvector bindings) is Python-native; Fastify gives the API layer
  cheap concurrency, first-class JSON schema validation, and static typing
  the frontend already shares.
- **Redis** — the simplest reliable broker for RQ: a FIFO queue plus job
  registries, with no durability burden (Postgres is the ledger of record).
- **Chunking: paragraph default** — fewest, most coherent retrieval units
  that cite back to source (see [rag.md](rag.md) §Chunking).
- **Embeddings: hash provider as model-of-record** — deterministic, offline,
  dimension-stable, letting the whole pipeline be testable and re-runnable;
  OpenAI is a drop-in swap behind the same seam.

---

## 5. Reliability & failure handling

- **Upload→enqueue is best-effort.** If the enqueue call dies, a sweep heals
  `queued` documents; job rows are idempotent (`UNIQUE(document_id, kind)`),
  so re-submission is safe.
- **Claims are atomic.** `ingestion_jobs.claim` flips `queued → processing`
  with `UPDATE…RETURNING`; two workers can never double-run a phase. Outcome
  `ignored` covers a lost claim.
- **Transient vs terminal.** Embedding retries its own spatial backoff
  (5xx/429/timeout retry; 4xx fail fast); the queue retries the job with
  exponential backoff; markers like `pdf extraction failed` are terminal.
- **No partial documents.** `ready` (pages+chunks) and embedding `ready`
  (vectors) each flip atomically in their own transaction.
- **Observability (Day 30).** Each service exports Prometheus metrics
  (`http_requests_total`, `http_errors_total`, `http_request_duration_seconds`,
  plus `ai_generation_duration_seconds`, `ai_tokens_total`,
  `queue_jobs_total`, `queue_job_duration_seconds`); one `x-request-id`
  joins backend ↔ ai-service ↔ worker log lines.

See [security.md](security.md) for threat model + controls and
[evaluation.md](evaluation.md) for how retrieval/RAG quality is measured.

---

## 6. Final architecture diagram

The complete system — same shape as the README's headline diagram.

```mermaid
flowchart TB
    REACT["React SPA<br/>(nginx :80 — serve + proxy)"]

    API["Backend — Node.js + Fastify<br/>auth · validation · documents · retrieval · RAG orchestration<br/>:3000"]

    PG[("PostgreSQL 18 + pgvector<br/>orgs · users · documents · chunks · vectors · job ledger<br/>:5434")]
    RD[("Redis 7 — RQ queue<br/>task broker · job registries<br/>:6375")]
    OBJ[("MinIO — S3 object storage<br/>original sources {org}/{uuid}.{ext}<br/>:9000")]

    WORKER["Worker — Python RQ SimpleWorker<br/>extract → chunk → embed (in-process phases)"]
    AISVC["Python AI Service — FastAPI<br/>embeddings · chat · RAG generation · guardrails<br/>:8000"]
    EMB["Embeddings<br/>hash (384-D) | openai"]
    LLM["LLM<br/>mock | openai"]

    REACT -->|auth · sessions · streamed RAG| API
    API -->|uploads · users · orgs · ledger| PG
    API -->|best-effort enqueue| RD
    API -->|store uploads| OBJ
    API -->|embed query · rag generate| AISVC
    PG -->|semantic retrieval — pgvector HNSW top-K| API
    RD -->|pop tasks| WORKER
    WORKER -->|claim · pages · chunks · vectors · ready flips| PG
    WORKER -->|download source| OBJ
    WORKER -.->|provider stack (in-process)| AISVC
    AISVC -->|embeddings| EMB
    AISVC -->|chat · RAG| LLM
```

Two flows through the same components:

- **Ingestion:** React → API → store object + queue → Worker (extract → chunk
  → embed) → Postgres ledger + vectors, with atomic `ready` flips.
- **Q&A:** React → API → pgvector retrieval (tenant-scoped top-K) → AI
  service → grounded (or refused) answer with citations → back up the same path.

The worker's phases run inside the RQ process using the *same* provider stack
the FastAPI service exposes over HTTP — that dashed edge is process-boundary
routing, not HTTP. Monitoring (Prometheus scraping the API, AI service, and
worker; Grafana dashboard) is intentionally omitted from this diagram for
shape clarity — it is covered in §5 and `docs/security.md` §10.

deployment mapping (compose, 10 services): the 9 boxes above plus
`prometheus` + `grafana`.