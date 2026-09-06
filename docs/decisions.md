# KnowFlow — Architecture Decision Records (ADR)

Numbered decisions for review-glance and interview stories. Each entry states
*the problem, what was considered, what we chose, and what it costs*. Most
decisions now have a test or a measurement that pins them (see
[evaluation.md](evaluation.md)).

| # | Decision | One-line reason |
|---|---|---|
| 001 | PostgreSQL + pgvector | one transactional engine for relational + vector data |
| 002 | Asynchronous document processing | slow provider-bound work must not sit in the upload HTTP path |
| 003 | Separate Node + Python services | best ecosystem per layer, shared JSON contract |
| 004 | Redis (+RQ) for the queue | simplest broker that schedules work across a real worker pool |
| 005 | `paragraph` chunking as default | fewest coherent citationable units, measured |
| 006 | Hash embeddings (384-D) as model-of-record | deterministic, offline, dimension-stable, testable end to end |
| 007 | Two-phase document pipeline (process → embed) | readable ≠ retrievable; provider call must not block legibility |
| 008 | Postgres job ledger → Redis task queue | durable, replayable, claim-protected phases; Redis stays a dumb pipe |
| 009 | App-level tenancy (not RLS) | Ship tenancy correctness for this product depth now |
| 010 | JWT access + rotating opaque refresh | stateless API auth with revocation where it matters |
| 011 | Fastify + schema-first validation | one validation source, `additionalProperties:false` enforced |
| 012 | MinIO (S3 API) for objects | pipeline contracts speak S3; deploy any provider |

---

## ADR 001 — PostgreSQL + pgvector

**Context.** The app needs relational joins (orgs → users → documents →
chunks), transactional integrity across a multi-phase pipeline, and semantic
search over vector embeddings. Most stacks bolt a vector database onto a
primary store, trading consistency and joining for a specialized index.

**Options.**
1. Postgres + a vector *extension* (pgvector) — one engine, real joins, SQL,
   HNSW/IVFFlat indexes in-database.
2. Dedicated vector DB (Pinecone/Qdrant/Weaviate) alongside Postgres — best
   raw ANN performance and managed scaling, at the cost of two systems to
   operate, dual writes, and join complexity across a network boundary.
3. Store vectors as blobs + brute-force scan — trivial but unscaleable.

**Decision.** Postgres 18 + pgvector 0.8.6, with a **dimension-flexible**
`vector` column and a HNSW *expression* index (partial on model-of-record) so
real models can coexist without a migration.

**Consequences.** One transaction can flip `documents.status='ready'` and
write 384-D vectors atomically; the queue ledger, chat transcripts, and
vectors share one backup story. Costs: ANN performance beyond the HNSW
config (`m=16`, `ef_construction=64`) is second-tier vs a dedicated ANN
engine; hybrid keyword/vector search stays hand-tuned (trgm + HNSW) rather
than a managed ranking service.

---

## ADR 002 — Asynchronous document processing

**Context.** Upload must answer fast, but extraction + chunking + embedding
are CPU/IO-bound and provider-bound (seconds to minutes). A synchronous
pipeline would turn every upload into an HTTP call whose latency is a remote
AI provider's.

**Options.** 1) Inline processing in the upload handler. 2) A background
queue with a durable ledger.

**Decision.** Upload → validate → store object → insert `documents` row →
**best-effort enqueue**; a worker pool then drives `process` → `embed`. The
upload response is immediate; `status`/`embedding_status` columns and the
`ingestion_jobs` ledger are the source of truth for "where is my document".
Enqueue itself is idempotent (`UNIQUE(document_id, kind)`) and healable (a
sweep re-queues stuck `queued` documents) so the fire-and-forget call is safe.

**Consequences.** Separate worker service + Redis to operate; a queue becomes
a first-class part of the architecture — but upload latency is decoupled from
pipeline latency, retries exist without duplicating work, and the app still
works (queues, reports *queued*) if an AI provider is down.

---

## ADR 003 — Separate Node.js and Python services

**Context.** The API layer and the ML/processing layer pull in opposite
directions. The former is I/O-bound HTTP with strict schema validation shared
with a TypeScript frontend; the latter is Python-native (pypdf for extraction,
provider SDKs, pgvector drivers, the eval/retrieval-quality tooling).

**Options.** 1) One language for everything. 2) Two services with an
explicit HTTP contract.

**Decision.** Fastify+TS backend and FastAPI+Python ai-service (plus the
Python RQ worker), exchanging JSON. Both compile/typecheck as CI gates and
talk over well-defined endpoints (`/v1/embeddings`, `/v1/rag/generate`,
`/v1/queue/…`). Cross-layer behavior is tested as integration, not assumed.

**Consequences.** Two runtimes to build/test/deploy (compose handles it; the
CI pipeline builds both). The win is real: the pipeline lives in the
ecosystem that owns it, and the API/validation layer shares types and
validation philosophy with the SPA.

---

## ADR 004 — Redis

**Context.** Need a work queue with one-or-more workers, a retry/delay
mechanism, and no durability burden — the *ledger of record* lives in
Postgres (**ADR 008**).

**Options.** Redis+RQ, RabbitMQ, Kafka, or Postgres-as-a-queue (LISTEN/NOTIFY
or polling).

**Decision.** Redis 7 + RQ. RQ gives FIFO queues, job registries
(queued/started/finished/failed), and a `SimpleWorker` that executes
in-process so per-job metrics stay countable. Because Postgres owns job
state, losing Redis loses *velocity*, not *truth*: a claim sweep re-heals.

**Consequences.** Minimal operational surface (Redis is a service compose
already runs); no consumer-group semantics if we ever need partitioned
fan-out at scale — that's the point we'd revisit RabbitMQ/Kafka.

---

## ADR 005 — Chunking strategy: `paragraph`

**Context.** RAG quality bottoms out in retrieval quality, which bottoms out
in chunk *coherence*. Options traded off on the same 6-page fixture:
`fixed` (hard char budget), `token` (word-aligned budget), `overlap`
(sliding tail), `paragraph` (blank-line semantic units).

**Measurement.** `fixed` → 19 chunks (~5 redundant); `token` → 5; `overlap`
→ 6 (**+160 redundant tokens**); `paragraph` → 5 chunks / ~590 tokens / ~6
redundant — *and* never splits a sentence or section.

**Decision.** `paragraph` is the default, others stay swappable (per-request
or `--chunk-strategy`).

**Consequences.** Fewest, most coherent retrieval units; answers can cite a
claim back to its section. Trade-off, measured: wide merges keep the *first*
section label, so section-pin filters assume section == paragraph start.

---

## ADR 006 — Embedding model: `knowflow-hash-384`

**Context.** The pipeline must be testable and re-runnable with no API keys,
while still being a real vector pipeline a production team can swap to a paid
model behind one seam. Startup risk is LLM-vendor lock-in; model churn breaks
retrieval reproducibility.

**Options.** 1) OpenAI embeddings as sole option — real quality, but keyed,
locked, and dimension-brittle for dev. 2) Deterministic local hashing
vectorizer (384-D, L2-normalized) as the **model-of-record**, OpenAI as a
drop-in provider.

**Decision.** Default provider = `hash` (`knowflow-hash-384`); `openai`
(`text-embedding-3-small`) is the same `EMBEDDING_PROVIDER` switch. The
dimension-flexible vector store (**ADR 001**) lets both coexist per chunk.

**Consequences.** The whole 30-day pipeline (tests, eval, CI) is hermetic and
free; hash vectors are upright enough for retrieval-quality tooling but not a
production semantic matcher — flipping providers is a config change, then
re-embed, in place.

---

## ADR 007 — Two-phase pipeline (process → embed)

**Context.** A document becomes *readable* once it is chunked, but only
*retrievable* once vectors exist — and embedding hits an external provider
that may be down.

**Decision.** Two phases with separate statuses: `documents.status`
(`queued→processing→ready|failed`) and `documents.embedding_status`
(`none→processing→ready|failed`). Embed runs as its own job, never inside the
ingestion transaction.

**Consequences.** A dead embedder never blocks a submitted document from
being readable (it stays `ready`; vectors just aren't there). Retrievability
is `status=ready AND embedding_status=ready` — one query. A failed embed run
can be re-run (upserts on `UNIQUE(chunk_id, model)`) with no state rebuild.

---

## ADR 008 — Postgres ledger, Redis dirty-pipe queue

**Context.** Yesterday's design detail is tomorrow's data-loss question:
"what happens to the job when Redis restarts?"

**Decision.** Both. `ingestion_jobs` (Postgres) is the durable, auditable
ledger with an **atomic claim** (`queued→processing … RETURNING`) so two
workers can't double-run a phase and a task survives a broker restart. RQ
(Redis) just hands the task to whichever worker pops it; re-submission is
idempotent. Outcomes: `processed`/`embedded`/`ignored`/`retrying`/`failed`.

**Consequences.** Operational redundancy (two stores), but each is doing the
job it is best at, and every phase boundary is provable from Postgres alone.

---

## ADR 009 — Application-level tenancy (not RLS)

**Context.** Multi-tenant isolation is the platform's central bet. Postgres
offers row-level security (RLS) as a defense-in-depth tool.

**Decision.** Every tenant table carries `organization_id`; every repository
query filters on it (verified by a cross-user integration suite: a
colleague's perfect-match document yields 404/nothing, never a leak).
Application-level enforcement **today**, RLS reserved as a future
**defense-in-depth** layer (it interacts with connection pooling,
per-request `SET ROLE`, and the existing join-heavy queries).

**Consequences.** Correctness is pinned by tests rather than DB policies —
strong when the product is this shape; the missing DB policy is the known
residual risk in the threat model.

---

## ADR 010 — JWT access + rotating refresh

**Context.** Stateless API auth for the SPA, with a way to revoke something
when an operator must.

**Decision.** Short-lived HS256 access JWT (`typ: "access"`, `jti`, `exp`)
+ opaque 256-bit refresh tokens stored **hashed** in Postgres, rotated on
use, revoked on logout, swept on expiry. `typ`/`jti`/`sub` are all enforced
in the auth guard.

**Consequences.** No session store → horizontally scale the backend freely;
revocation lives at the refresh boundary, which is acceptable because access
tokens are short-lived. Hash-at-rest means a DB leak can't replay refreshes.

---

## ADR 011 — Fastify + schema-first validation

**Context.** Validation is a security control as much as DX: unknown keys,
escalated roles, oversized payloads.

**Decision.** Fastify's JSON-schema validation with `additionalProperties:
false` enforced (`removeAdditional: false`), a typed error envelope
(`{error:{code,message}}`), and rate limiting as route config. Schemas are
colocated with routes and compiled once at boot.

**Consequences.** Malformed/enriched payloads die at the door as `400`
before any handler runs; schema evolution is explicit; a typoed client flag
is never silently ignored.

---

## ADR 012 — MinIO (S3-compatible object storage)

**Context.** Source documents need object storage with a contract the
pipeline and any future cloud deploy both speak.

**Decision.** Store original uploads in a bucket (`{org}/{uuid}.{ext}`) via
the S3 API, served by MinIO locally. The pipeline speaks S3; swapping to S3/
S3-compatible cloud hosting is an endpoint + credential change.

**Consequences.** Local parity with production object semantics; one extra
service in compose. Objects are immutable after write; the only lifecycle is
create/delete-cascade.