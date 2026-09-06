# KnowFlow Architecture — Day 7 Review (+ Days 8-32 additions)

> **Renamed (Day 31):** this file is now the *running as-built record* of
> Days 7–30. The clean, current system-design docs live in sibling files —
> see [`docs/architecture.md`](architecture.md) and the index in the README.
>
> Every claim below was re-verified against the live system on Day 7.
> Day 8 appended the upload path and MinIO storage;
> Day 9 appended the PDF processor and `document_pages`; Day 10 appended
> chunking and `chunks`; Day 11 appended embedding and `embeddings`; Day 12
> appended the background queue — `ingestion_jobs`, Redis + RQ workers, and
> the derived upload state machine; Day 13 appended the document management
> API — pagination/filtering, detail, owner-only deletion, and a status
> endpoint; Day 14 appended the end-to-end integration test harness
> (`e2e/run.sh` + `e2e/test_pipeline.py`) that proves the whole pipeline on a
> fresh boot with no manual steps; Day 15 appended semantic retrieval —
> `POST /api/v1/search` embeds the question via the AI service and walks the
> HNSW cosine index over each tenant's retrievable chunks; Day 16 appended the
> retrieval-quality evaluation — a golden-size benchmark (5 docs / 31 pages /
> 33 queries) sweeping Top-K, similarity threshold, chunk size/overlap and
> metadata filtering across the *real* chunkers + embedder, plus the measured
> search knobs (`minSimilarity` floor, `sourceType` and `metadata` filters)
> that the experiment justified; Day 17 appended the RAG generation layer —
> `POST /api/v1/rag/generate` retrieves each tenant's chunks, assembles a
> token-budgeted context, and hands it to the AI service's `/v1/rag/generate`,
> which owns prompt assembly and the "I don't know" guardrails (defaulting to
> a deterministic, faithful-by-construction extractive provider); Day 27
> appended the streaming RAG path — the AI service's `POST /v1/rag/generate/stream`
> emits `delta` / `done` / `[DONE]` SSE that the backend proxies verbatim via
> `reply.raw`, plus the React conversation-sidebar chat UI (markdown, `[n]`
> citations, sources, loading/error/empty states); Day 28 appended the
> deployment story — `docker-compose.yml` one-command boot of the full stack
> behind an nginx frontend that proxies `/api`, `/auth`, and the SSE stream.
> Deltas
> are marked **[Day 8]** / **[Day 9]** / **[Day 10]** / **[Day 11]** / **[Day 12]** / **[Day 13]** / **[Day 14]** / **[Day 15]** / **[Day 16]** / **[Day 17]** / **[Day 27]** / **[Day 28]**.

## 1. As-built diagram

Solid = implemented and verified. Dashed = planned (later days).

```mermaid
flowchart LR
    subgraph Client["Client"]
        WEB["Web App / SDK"]
    end

    subgraph Backend["backend/ — Fastify + TS (:3000)"]
        HTT["routes: /health, /auth/*,<br/>/api/v1/me, /api/v1/documents,<br/>/api/v1/search, /api/v1/rag/generate"]
        AUTH["AuthService<br/>bcrypt + JWT + refresh rotation"]
        MID["request-id, security-headers,<br/>error envelope"]
        DOC["DocumentsService<br/>validate → store → insert"]
        RAG["RagService<br/>retrieve → budget context → generate"]
    end

    subgraph AI["ai-service/ — FastAPI (:8001 host)"]
        EMB["/v1/embeddings</br>hash | openai"]
        LLM["/v1/chat/completions</br>mock | openai"]
        PROC["/v1/process/documents/{id}</br>claim → download → extract → chunk → store"]
        VECT["/v1/embed/documents/{id}</br>batch → retry → validate → store"]
        QUE["/v1/queue/documents/{id}/*</br>enqueue + derived state"]
        WK["RQ worker (app.queue.worker)</br>pops tasks, claims phases, chains embed"]
        RAGEN["/v1/rag/generate</br>extract | openai provider + guardrails"]
    end

    subgraph Data["db/ — PostgreSQL 18 + pgvector (:5434)"]
        PG[("organizations, users,<br/>refresh_tokens")]
        PAGES[("documents, document_pages")]
        CHUNKS[("chunks + trgm index")]
        VECDB[("embeddings (dim-flexible vector,<br/>UNIQUE chunk×model)")]
        JOBS[("ingestion_jobs<br/>queued→processing→succeeded|failed<br/>attempts/max_attempts ledger")]
    end

    subgraph St["MinIO — S3-compatible (:9000/9001)"]
        OBJ[("bucket knowflow<br/>{org}/{uuid}.{ext}")]
    end

    subgraph RD["knowflow-redis — Redis 7 (:6375)"]
        RQ[("RQ queue + job registries")]
    end

    WEB -->|register / login / refresh| AUTH
    WEB -->|protected requests, Bearer JWT| HTT
    WEB -->|multipart upload| DOC
    DOC --> OBJ
    DOC --> PAGES
    DOC -.->|enqueue-on-upload (best-effort)| QUE
    HTT --> MID
    AUTH --> PG
    QUE ==>|ensure job row| JOBS
    QUE ==>|submit task| RQ
    RQ <==>|pop / ack tasks| WK
    WK ==>|claim phase + write ledger| JOBS
    PROC ==>|claimed queued PDF| PAGES
    PROC ==>|downloads object| OBJ
    PROC ==>|writes document_pages| PAGES
    PROC ==>|writes chunks in same tx| CHUNKS
    VECT ==>|reads ready doc's chunks| CHUNKS
    VECT ==>|writes vectors + ready flip, one tx| VECDB
    WK ==>|chains embed job on success| RQ
    HTT ==>|Day 15 — embeds queries| EMB
    HTT ==>|Day 15 — top-K over tenant| VECDB
    HTT ==>|Day 16 — optional sourceType / metadata / minSimilarity pre-filters| VECDB
    HTT ==>|Day 17 — retrieve, budget context| RAG
    RAG ==>|Day 17 — question + context| RAGEN
    RAGEN -.->|Day 17 — future real LLM| LLM
    EMB -.-> VECDB
    LLM -.-> VECDB
```

Painting the picture honestly, one week on: the backend and the AI service
*talk* now. Day 12's queue is the missing ductwork — an upload turns into
READY embedded vectors without a human calling `/v1/process` and
`/v1/embed`. Phase work (process/embed) is triggered by RQ workers instead
of the Day 9-11 CLI triggers, and `ingestion_jobs` (Postgres) is the durable
ledger Redis tasks are replayed from. Retrieval now reaches through it: the
backend embeds queries via the AI service and walks the HNSW index (Day 15;
optional `sourceType`/`metadata`/similarity-floor pre-filters since Day 16).
RAG is now connected (Day 17): the backend retrieves each tenant's chunks,
budgets a context, and the AI service generates an answer via a swappable
provider (extractive baseline today, real LLM via the same seam later). The
queue's edges — a sweep to heal `queued` documents whose enqueue died,
stale-run leases — remain the Day-23 work.

## 2. Component inventory

| Piece | Language/Stack | Entry point | Port | Contract |
|---|---|---|---|---|
| `backend/` | TypeScript (ESM, strict), Fastify 5, pg | `src/server.ts` | 3000 | Client-facing API |
| `ai-service/` | Python 3.12, FastAPI, httpx | `app/main.py` | 8001 (host) / 8000 (container) | OpenAI-compatible internal util |
| `db/` | PostgreSQL 18 + pgvector 0.8.6 | `db/schema.sql` + `backend/src/database/migrations/` | 5434 | relational + vector store |
| `storage/` **[Day 8]** | MinIO (`minio/minio`) via `@aws-sdk/client-s3` | container `knowflow-minio` | 9000 API / 9001 console | document object store |
| `processor` **[Day 9]** | Python (pypdf, boto3, psycopg) inside `ai-service/` | `POST /v1/process/documents/{id}` + `app/processor/cli.py` | on :8001 | PDF → `document_pages` rows |
| `chunker` **[Day 10]** | Python (`app/processor/chunkers.py`) inside `ai-service/` | same trigger, body-selected strategy | on :8001 | pages → `chunks` (strategy/section/page_range in metadata) |
| `embedder` **[Day 11]** | Python (`app/embedding/`) inside `ai-service/` | `POST /v1/embed/documents/{id}` + `app/embedding/cli.py` | on :8001 | chunks → `embeddings` (batched, retried, validated) |
| `queue worker` **[Day 12]** | Python (RQ 2.x) inside `ai-service/` | `python -m app.queue.worker` (pool of N workers) | — (consumes Redis) | pop task → claim `ingestion_jobs` phase → run processor/embedder → ledger row |
| `queue broker` **[Day 12]** | redis:7-alpine, container `knowflow-redis` | RQ broker | 6375 (host) | transient task queue + job registries; the ledger is Postgres |
| `semantic_search.py` | zero-dep Python | standalone script | — | Day-2 lesson artifact |

## 3. Architecture review

### Validated decisions (keep)

- **SQLite-pgvector over a standalone vector DB.** Vectors sit beside the
  tenant metadata so one query does "filter by org AND nearest-neighbor."
  The HNSW index (`vector_cosine_ops`, `m=16, ef_construction=64`) answers
  the Day-3 demo at sub-millisecond scale for the seed corpus.
- **Two languages on purpose.** Python for AI providers (fast to port, rich
  model ecosystem), Node for the always-hot client API (cheap streaming, one
  toolchain). They communicate by HTTP with OpenAI-compatible payloads, which
  makes either side replaceable.
- **Provider seams, not branches.** `ai-service/app/services/` exposes a
  `Protocol`; switching `hash` → `openai` is a config change
  (`EMBEDDING_PROVIDER`, `LLM_PROVIDER`), validated at startup by
  `services/__init__.py` fail-fast. The backend mirrors this with
  `buildApp({ config, pool })` dependency injection — every test injects a
  fake pool.
- **Auth chosen for portfolio-grade posture.** bcryptjs cost 10; access
  tokens HS256 @15 min with issuer verified (`allowedIss`); refresh tokens are
  256-bit opaque values stored only as sha256, rotated on every use so replay
  of a leaked token fails. This is genuinely above the demo-service bar.
- **Tests that talk to reality.** Both stacks run unit tests with fakes *and*
  DB-guarded integration tests (`itDb` skip pattern) so a broken schema or a
  dead container surfaces as a red test, not a runtime surprise.

### Honest gaps (not fixed, by design — tomorrow's work)

1. **The AI service is an open trust boundary.** `ai-service/:8001` answers
   anyone who can reach it; no API key, no JWT. Acceptable while it's a local
   utility, *unacceptable* the moment it is on a network. Fixes: backend-side
   API-key auth when wiring Days 15-17, or confine it to Docker's internal
   network (Day 27).
2. **Tenant isolation is convention, not enforcement.** Every tenant-owned
   table carries `organization_id` and Day-3/seed and Day-6 repos filter on it —
   but nothing *forces* a query to filter (the seed script ships a live
   cross-tenant-leak demo). Row-Level Security is Day 29's explicit task; the
   demo exists so the lesson is not forgotten.
3. **Migrations are applied by hand.** `schema.sql` (Day 3) was the de-facto
   migration 001, then `002_auth.sql`, then today `003_schema_hygiene.sql` —
   all piped into the container by hand. There is no version table and no
   runner, which will hurt the moment two environments drift. A tiny runner
   (or even a verified `entrypoint.sh`) is Day-27 deployment work.
4. **`/api/v1/echo` ships in the API.** It exists to prove strict JSON-Schema
   validation and powers tests. Keep it, but it is demo surface; gate or drop
   it before anything public.

## 4. Database schema review

### Fixed today (migration `003_schema_hygiene.sql`)

- **`updated_at` never moved.** `documents.updated_at` existed but nothing
  maintained it; a parser changing `status` queued→processing→ready would
  leave the timestamp frozen at `created_at`. Added a `touch_updated_at()`
  BEFORE-UPDATE trigger on `documents` and `users` (the users table also got
  its missing `updated_at` column — Day-6 `last_login_at` bumps were its only
  writes). Applied and verified live.

### Verified OK

- FK cascade graph is a clean tree: `org → {users, documents} → chunks →
  embeddings`, plus `users → refresh_tokens`. Deleting a tenant cannot orphan
  rows.
- `UNIQUE (organization_id, email)` matches the multi-tenant premise; slugs
  are `UNIQUE`; `chunks (document_id, seq)` is `UNIQUE`; embedding rows are
  `UNIQUE (chunk_id, model)` so multiple models can coexist per chunk.
- Indexing matches access patterns: B-trees on `organization_id`, HNSW for
  ANN, trigram GIN on `chunks.content` ready for hybrid search.
- `documents.uploaded_by` intentionally does **not** cascade (default NO
  ACTION): deleting a user that uploaded documents must fail loudly, not
  silently destroy provenance.

### Noted, not changed

- HNSW `m=16`/`ef_construction=64` are fine for a few thousand vectors; the
  ANN recall should be re-tuned when the corpus gets real (Day 25 load test).
- RLS remains the umbrella item (Day 29).
- `refresh_tokens` is append-only growth — a retention sweep (delete rows where
  `expires_at < now()`) is worth scheduling with ingestion observability.

## 5. API structure review

| Route | Service | Status |
|---|---|---|
| `GET /health` | backend | up |
| `POST /auth/register`, `/login`, `/refresh`, `/logout` | backend | up |
| `GET /api/v1/me` | backend (protected) | up |
| `POST /api/v1/documents` **[Day 8]** | backend (protected, multipart) | up |
| `GET /api/v1/documents` **[Day 8, extended Day 13]** | backend (protected; paginated `{items, pagination}`, filters `sourceType`/`status`/`embeddingStatus`/`search`) | up |
| `GET /api/v1/documents/:id` **[Day 13]** | backend (protected; detail incl. embedding lifecycle) | up |
| `DELETE /api/v1/documents/:id` **[Day 13]** | backend (protected, **owner-only**; row + object together) | up |
| `GET /api/v1/documents/:id/status` **[Day 13]** | backend (protected; derived `stage` + page/chunk/vector counts) | up |
| `POST /v1/process/documents/{id}` **[Day 9]** | ai-service (work trigger) | up |
| `POST /v1/process/documents/{id}` body `{chunk_strategy, chunk_size, overlap_tokens}` **[Day 10]** | ai-service (strategy-selected from the same trigger) | up |
| `POST /v1/embed/documents/{id}` **[Day 11]** | ai-service (embedding phase; config-selects the model) | up |
| `POST /v1/queue/documents/{id}/process` **[Day 12]** | ai-service (enqueue process; idempotent, 503 when Redis is down) | up |
| `POST /v1/queue/documents/{id}/embed` **[Day 12]** | ai-service (enqueue embed; idempotent, 503 when Redis is down) | up |
| `GET /v1/queue/documents/{id}` **[Day 12]** | ai-service (derived state UPLOADED→PROCESSING→EMBEDDING→READY/FAILED; reads Postgres only) | up |
| `POST /api/v1/echo` | backend (validation demo) | up |
| `GET /health`, `POST /v1/embeddings`, `POST /v1/chat/completions` | ai-service | up |

- **Versioning convention (set deliberately today):** system routes
  (`/health`, `/auth/*`) are unversioned; business resources live under
  `/api/v1/`. Rationale: auth health and infrastructure contracts fold into
  "platform", resources evolve with business. Documented here so later days are
  consistent rather than accidental.
- **Two envelope styles, and that is fine.** The client API returns
  `{ error: { code, message, details? } }` always; the AI service returns raw
  OpenAI shapes (`{ data: [...] }`, `{ choices: [...] }`). They serve
  different consumers — one external human/SPA contract, one internal
  drop-in-compatible model gateway. Never cross them.
- **Error codes are stable and tested** (`VALIDATION_ERROR`, `UNAUTHORIZED`,
  `INVALID_CREDENTIALS`, `INVALID_REFRESH_TOKEN`, `MULTIPLE_ACCOUNTS`,
  `EMAIL_TAKEN`, `FORBIDDEN`, `NOT_FOUND`, `INTERNAL_ERROR`). **[Day 8]** adds
  the upload family: `FILE_TOO_LARGE` (413), `UNSUPPORTED_FILE_TYPE` (415),
  `FILE_TYPE_MISMATCH` (400), `EMPTY_FILE`, `INVALID_FILENAME`,
  `FILE_REQUIRED`, `MULTIPLE_FILES` (400), plus mapping of the multipart lib's
  own `FST_REQ_FILE_TOO_LARGE` → 413 and `FST_PARTS_LIMIT_PART_FILES` → 400 so
  the client always sees the KnowFlow envelope.
- **Deferred:** CORS for a web client, OpenAPI/Swagger on the backend,
  rate-limiting on `/auth/*` (brute-force) — all Day 22/27 work.

## 6. Technical debt register

| # | Debt | Severity | Status |
|---|---|---|---|
| 1 | Backend `Dockerfile` never built | high (untested artifact) | **verified on Day 7** — `docker build` passes (see below) |
| 2 | `updated_at` trigger missing | medium (silent staleness) | **fixed** — migration `003` |
| 3 | No migration runner / version table | medium (env drift) | deferred — Day 27 |
| 4 | AI service unprotected on network | high (trust boundary) | deferred — Days 15-17 / 27 |
| 5 | No RLS / enforced tenant scoping | high (isolation) | deferred — Day 29 |
| 6 | `/api/v1/echo` demo surface | low | keep + gate later |
| 7 | `refresh_tokens` retention sweep | low | scheduled with Day 13 |
| 8 | No rate limiting on auth | medium | deferred — Day 22 |
| 9 | Pool tuning + test parallelism | low | **fixed** — vitest `fileParallelism: false` (Day-7 flake: two integration files sharing one container starved a pool-client fetch past its 2.5s timeout, making `/health` intermittently "degraded"); revisit pool sizing under real load on Day 25 |
| 10 | Upload bytes buffered in memory **[Day 8]** | medium | `limits.fileSize` caps a file at 25 MiB but the whole part is read into a Buffer before validation; fine now, wrong at real scale. Stream to disk/object-store with validation on the fly — Day 13 |
| 11 | MinIO creds are env vars **[Day 8]** | low | fine locally; secret manager until Day 28 |
| 12 | AWS SDK v3 wants node ≥22 **[Day 8]** | low | SDK prints a warning on node 20 (required from Jan 2027); pin the SDK version we validated under and plan the node 22 bump with Day-27 image work |
| 13 | Processing is trigger-based, not queued **[Day 9]** | medium | **fixed on Day 12** — the processor and embedder are now RQ worker tasks driven by the `ingestion_jobs` ledger; the CLI triggers (`python -m app.processor.cli`) remain as ops/backfill equivalents. Retry/backoff/worker-pool shipped here too, so the old "Day-13 queue item" is reduced to the sweep + stale-run leases below |
| 14 | Non-PDF parsers are pending **[Day 9]** | low | docx/md/html/txt uploads stay `queued`; under the queue the process job now **fails** with `no parser for source_type=md yet` while the document stays `queued` (state FAILED) — honest, never wedged, until the parsers land (deferred, open) |
| 15 | `get()` on a never-processed seed doc **[Day 9]** | low | Day-3 seed rows ("Feline Health Handbook" etc.) sit at `ready` with no pages or storage key — pre-pipeline artifacts; still open. Retrieval is safe from them: they have no `embeddings` (never embedded) so the Day-15 search gate (`embedding_status=ready`) simply never surfaces them |
| 16 | Day-3 `chunks`/`embeddings` v1 skeleton retired **[Day 10]** | — | migration `006` dropped the empty `chunks(seq)` + hand-seeded `embeddings` demo tables; `db/seed.py` is now a tenant-data reset. Day 11 rebuilds `embeddings` against the new `chunks(id)` for real (no synthetic vectors) |
| 17 | Paragraph detection is heuristic **[Day 10]** | low | bounding is deliberate (sentence-end / heading-like line heuristics, worst case = page-level chunk), but a smarter model (layout-aware pypdf, or blank-line-preserving renderers) improves units; re-chunking is now a cheap `POST /v1/process` re-run once the claim gate is lifted |
| 18 | A crashed run can wedge statuses **[Day 11]** | low | now *mostly* owned by the queue: the atomic `queued→processing` claim means a dead worker leaves a `processing` job row that the next worker's claim skips, so a retry can never double-run — but there is still no stale-run **lease/age-out** to auto-requeue a crashed `processing` phase. The leases + a recovery sweep (aged `processing` rows → crash-failed or re-queued) are the Day-23 item |
| 19 | Vector index — resolved **[Day 15]** | done | migration `009` builds HNSW cosine as a *partial expression index* — `USING hnsw ((embedding::vector(384)) vector_cosine_ops) WHERE model = 'knowflow-hash-384'` — because the column stays dimension-flexible; retrieval re-states the exact cast expression in `ORDER BY`, so an `EXPLAIN` shows the planner using `idx_embeddings_hnsw_cosine`. A future 1536-D real model gets its own `::vector(1536)` index instead of fighting the schema |
| 20 | Failed enqueues are visible only as `queued` rows **[Day 12]** | low | enqueue-on-upload is best-effort by design — a dead API/Redis must not fail the upload, the document just stays `queued`. Day 13's `GET /documents/:id/status` now makes that *visible* (stage + counts + lifecycle errors), but nothing **re-discovers** stranded `queued` docs yet: the requeue sweep is the Day-23 carry |
| 21 | `ingestion_jobs` is a hand-applied trigger table **[Day 12]** | low | same story as migrations: migration `008` is piped by hand (schema.sql stays synced). No new machinery — folds into the Day-27 runner item |
| 22 | Offset pagination on `GET /documents` **[Day 13]** | low | `LIMIT/OFFSET` is O(n) to a deep page and can skip/duplicate rows if inserts land mid-page. Fine at tenant library scale; a keyset (cursor) page is the Day-26/27 concern once per-tenant volume justifies it |
| 23 | `DELETE` object cleanup is best-effort **[Day 13]** | low | the row is deleted first (`DELETE … RETURNING storage_key`), then the object; a dead bucket orphans *bytes, never a row*. A storage-vs-db reconciliation sweep (list objects, drop keys with no `documents.storage_key`) belongs with the Day-27 ops tooling |
| 24 | Chunk merges keep only the first section label **[Day 16]** | low | the paragraph chunker concatenates whole section-pages into one chunk and stamps it with the **first** section only (`metadata.section`), so a wide merge erases every section it crosses. The golden-set run measured the cost: at paragraph-512 the target section survives chunking only **27%** of the time (reach), while `token 64` keeps attribution 100% of the time. Retrieval is unaffected (chunk-level search, Day 15), but metadata-filtered search inherits the erasure. Options, in order consulted: (a) **section-atomic chunking** — never merge two `metadata.section`s into one chunk (respects the existing anchor invariant, ~1-line budget check), (b) a multi-label `sections` array replaced by a distinct section-value job, out of scope here. The shared paragraph budget (default 512) bounds the fix's urgency. Candidates: Day 19 (refinement) or Day 24 (retrieval-quality regression) |
| 25 | Extractive generator is the only grounded provider **[Day 17]** | low | the `extract` provider is deterministic and faithful by construction (quotes a verbatim sentence, refuses on no overlap), and it is the production default. The `openai` provider is wired but unprompted; until a real LLM is enabled, every answer is a quote and "I don't know" is exact. Enable + prompt-engineer `openai` (Days 19-21) when a real model/key is available |

### Day-7 verification record

- `npm run typecheck` + `npm test` → **24/24 pass**
- pytest on `ai-service` → **8/8 pass**
- `docker build` of `backend/` → **passes** (multi-stage, image tagged `review`)
- migration `003` applied to the live container; trigger rollout confirmed

**[Day 8] verification record:**

- `npm run typecheck` + `npm test` → **42/42 pass (×2 consecutive runs)**
- Live HTTP smoke on :3000: real PDF upload → 201 `{pdf, application/pdf, size, queued}`; real `.docx` upload → 201 with the OOXML mime; a zip renamed `fake.pdf` → **400 FILE_TYPE_MISMATCH**; no token → 401; list → both docs returned
- Objects verified physically present in the bucket via the AWS SDK
  (`ListObjectsV2` → `{org_id}/{uuid}.pdf` 50 B, `{uuid}.docx` 72 B); the
  spoofed upload left nothing behind
- MinIO internals: `bucket was created lazily on first upload`; smoke orgs +
  objects deleted afterwards, DB back to seed state (2 orgs)

**[Day 9] verification record:**

- `python -m pytest` on `ai-service` → **35/35 pass (×2 runs)**; the three live
  integration tests now assert through a *second* connection, and one of them
  exists precisely because the writer-connection-only view hid a year-class
  bug (see §7 dev-log entry 6)
- Live HTTP smoke across both services: backend upload of a 3-page outline PDF
  → `POST /v1/process/documents/{id}` → **ready, 3 pages, sections
  Introduction/Architecture/Retrieval**, `s3://` sources present, status
  `ready`, error NULL — all confirmed by psql on a separate connection;
  truncated `corrupt.pdf` → **failed** (`unreadable PDF` in `documents.error`,
  zero pages); reprocess → **409 not_queued**
- Bucket cleaned to 0 objects after smoke; `document_pages` back to 0 rows

**[Day 10] verification record:**

- `python -m pytest` on `ai-service` → **58/58 pass (×2 runs)**, including 4
  live DB/MinIO integration tests; backend `npx tsc --noEmit` + `npx vitest
  run` → **42/42** (unchanged, upload suite untouched)
- Live HTTP smoke across both services: upload `chapter.pdf` via the backend
  → process (default body) → **ready: 6 pages, 2 paragraph chunks**; upload
  `three_pages.pdf` → process with
  `{"chunk_strategy":"overlap","chunk_size":128,"overlap_tokens":32}` →
  **ready, chunk_strategy=overlap**; upload `corrupt.pdf` → **500 failed,
  `unreadable PDF` stored**; reprocess of a ready doc → **409**
- From a separate psql connection: chunk rows carry `chunk_index` 0..n
  contiguous, `metadata.strategy` correct, `section`/`page_range` set,
  `token_count > 0`; zero chunks for the failed doc; DB back to seed state
  (2 orgs, 6 docs, 0 pages, 0 chunks), MinIO at 0 objects, no
  `idle in transaction` left behind
- First overlap-chunker draft was visibly wrong (44 tiny windows on a
  590-token doc): the hop measured one word instead of the accumulated tail.
  Caught by the invariant tests (contiguous coverage / real sharing), fixed
  with an O(1) incremental tail-length hop

**[Day 11] verification record:**

- `python -m pytest` on `ai-service` → **83/83 pass (×2 runs)**: 12 new
  embedding-pipeline unit tests (batching, per-batch retry, retry exhaustion,
  4xx-is-fatal, dimension/empty-vector validation, claim race, no-chunks
  refusal) + 4 new embed-API tests + 5 new live integration tests asserting
  committed state through a second connection
- Live HTTP smoke end to end: register org → upload `chapter.pdf` via the
  backend → `POST /v1/process/documents/{id}` → **ready: 6 pages, 2
  paragraph chunks, 587 tokens** → `POST /v1/embed/documents/{id}` →
  **embedded: 2 vectors, knowflow-hash-384, 384-D** → second embed → **409**;
  `/v1/embeddings` util endpoint unaffected
- From a separate psql connection: one `embeddings` row per chunk, `model`
  + `dimensions` recorded, `vector_dims(embedding) = 384`, `organization_id`
  correct, `embedding_status = ready`, `embedding_error` NULL; failed runs
  leave zero rows; DB cleaned to seed state (2 orgs, 6 docs, 0 pages, 0
  chunks, 0 embeddings), MinIO at 0 objects, no `idle in transaction`
- Also swept a leftover Day-10 smoke org (`doc-...` with a queued `guide`
  pdf + orphaned bucket object) that the Day-10 cleanup script's env-var bug
  stranded — DB counts are exact again after the sweep

**[Day 12] verification record:**

- `python -m pytest` on `ai-service` → **121/121 pass (×2 runs)**: 26+ new
  queue unit tests (derive_state across all five states incl. failed-job
  rules, idempotent enqueue, atomic claim/duplicate worker, retry
  budget/backoff, process→embed chaining, terminal-vs-transient classifiers,
  crash/exhaustion recovery) + queue API tests with a fake QueueService
  (unknown id → 404, Redis down → 503, READY projection) + 2 **live RQ tests**
  (real `SimpleWorker`, PG+MinIO+Redis guarded) asserting the success chain
  and the unsupported-doc failure lane through a second connection;
  `backend` `npx tsc --noEmit` + vitest → **45/45** (+3 enqueue tests: fake
  enqueuer receives the doc id; enqueuer throws → upload still 201 and object
  retained; no-op without an enqueuer)
- Live end-to-end smoke (with a real RQ worker running,
  `python -m app.queue.worker`): register org → upload `chapter.pdf` via the
  backend → **without touching anything manually**, worker processed it:
  `documents.status=ready` + `embedding_status=ready`, 2 chunks, 2
  embeddings (`knowflow-hash-384`, 384-D), both `ingestion_jobs` rows
  `succeeded` with `attempts=1` and timestamps, `GET
  /v1/queue/documents/{id}` → `READY`
- Failure lane exercised live: upload `notes.md` → process job **failed**
  (`no parser for source_type=md yet`, exhausted), document stays `queued`,
  queue status `FAILED` — parsers are the only missing piece, and leaves
  nothing falsely "ready"
- Cleanup after smoke: smoke org + both storage objects deleted, Redis
  flushed (queues + registries empty), servers + worker + queue tests' SimpleWorker
  stopped; DB back to exact seed state — 2 orgs, 2 users, 6 docs,
  **0 pages / 0 chunks / 0 embeddings / 0 ingestion_jobs**; no
  `idle in transaction` left behind; :3000/:8001 ports free

**[Day 13] verification record:**

- Backend `npx tsc --noEmit` + `npx vitest run` → **62/62 pass (×2 runs)**: 9
  new unit tests for the management service (filter/pagination SQL +
  LIKE-escape, tenant-scoped get returns null, delete composes row+object,
  seed-row delete skips the object, 404 on unknown, status bigint coercion +
  all six stage derivations) + 8 new live integration tests against Postgres
  + MinIO: detail view with embedding lifecycle, cross-tenant get/delete →
  404, malformed id → 400, status lifecycle+counts, delete removes the row
  AND the bytes (`storage.head` undefined after), member delete → 403 via a
  minted member token, pagination/filtering (`limit=1` → hasMore, `sourceType`,
  `status`, `search`), and every bad query (`sourceType=exe`, `limit=0`,
  `limit=1000`, `offset=-1`, unknown key) → 400
- Live HTTP smoke on :3000: register → upload `chapter.pdf` → `GET
  /api/v1/documents/:id` (queued / `embeddingStatus: none` / null errors) →
  `GET /:id/status` (`stage: uploaded`, counts 0/0/0) → `DELETE` → **204** →
  `GET /:id` → `404 NOT_FOUND` → filtered list (`sourceType=md&status=queued`
  → `{ items: [], pagination: { total: 0 } }`) → `sourceType=exe` →
  `VALIDATION_ERROR`
- Cleanup after smoke: smoke org deleted (cascade), the deleted document's
  MinIO object confirmed gone by the integration delete test (`HeadObject`),
  Redis flushed, backend stopped; DB back to 2 orgs / 2 users / 6 docs /
  0 pages / 0 chunks / 0 embeddings / 0 ingestion_jobs, 0 `idle in
  transaction`, :3000 free

**[Day 14] verification record:**

- `e2e/run.sh` (boots backend + ai-service + one RQ worker, each a `setsid`
  process group, trap kills the whole tree) + `e2e/test_pipeline.py`, run
  **×4**: three failures were harness bugs, then **1 passed — milestone gate
  PASSED** (~10s: register → upload → READY in ~3s)
- The full chain over public HTTP only: upload chapter.pdf → backend enqueues
  → single worker extracts (6 pages) → chunks (2) → embeds (2 × 384-D,
  `knowflow-hash-384`) → API reports `READY`; stage trace:
  `UPLOADED → PROCESSING → READY` (one run sampled a mid-commit `UPLOADED`
  after chunks existed — the derived stage re-derives from table state each
  poll, so a READY poll is the only one that matters)
- Cross-checks that passed: `/v1/queue/documents/{id}` mirrored `READY` with
  both jobs `succeeded`/attempts 1; a **second DB connection** re-read
  committed rows (docs/pages/chunks/embeddings, dims 384); pgvector
  queryability — re-embed the first chunk via `POST /v1/embeddings`, store
  reports distance ≈ 0 (deterministic hash), two distinct chunks do **not**
  collapse to one vector, and no zero/NaN 384-D vector exists; `DELETE` via
  API → 204 then `GET` → 404; org cascade + `assert_cleanup` counts **0**
  leaked rows in pages/chunks/embeddings/ingestion_jobs
- Harness gotchas the runs surfaced (fixes committed to the test, not the
  product): `l2_norm(vector)` is ambiguous under pgvector 0.8 (new `halfvec`
  overload) — vector sanity moved to Python-side parse of `embedding::text`
  (also catches 384-D + finite + nonzero); embeddings endpoint takes
  `{"text": "string"}` not a list; `embeddings` has no `document_id` (it
  references chunks) so the leak-check joins
- After the final run: DB back to seed — 2 orgs / 2 users / 6 docs / 0 pages
  / 0 chunks / 0 embeddings / 0 ingestion_jobs; :3000/:8001 free; no stray
  tsx/uvicorn/worker processes (earlier per-PID cleanup had left an orphan
  `node` on :3000 — fixed with process-group kill); guard skip verified
  (`1 skipped` when the stack is down)

**[Day 15] verification record:**

- Backend `npx tsc --noEmit` clean; `npx vitest run` → **74/74 pass** (+5
  unit, +7 integration — search additions; the +7 integration all ran
  against the real pgvector store with an injected deterministic embedder)
- **Unit** (search.service.unit.test.ts): repository SQL carries the tenant
  model + retrievability gates (`organization_id = $1`, `e.model = $3`,
  `status='ready' AND embedding_status='ready'`) and the horizontal cast
  order-by; the query vector literal is a `toFixed(6)` string so an exact
  re-embed lands at distance 0 (matches the stored precision); similarity =
  1 − cosine_distance mapping + metadata passthrough; `limit` passthrough;
  embedder failure and a missing embedder both → `503 EMBEDDING_UNAVAILABLE`
- **Integration** (integration.search.test.ts, fake embedder at cosine
  0.6/0.8): seeds a real corpus (API-uploaded docs whose chunk axes sit at
  `[1,0]` / `[0,1]`) and asserts pgvector does the math — results ordered
  best-first (0.8 then 0.6), full wire shape (chunk/similarity/document/
  page/metadata), `limit=1` trims, **a perfect-match chunk in another tenant
  never leaks**, a different-model perfect match is excluded, a document
  whose vectors `failed` is excluded, validation 400s (blank/missing query,
  limit 0/100, typo'd key) and anonymous → 401
- **Live** (e2e): `./e2e/run.sh` boots the full stack; after a real
  upload → READY, `POST /api/v1/search` over the pipeline's own corpus
  returned 200 with `model: knowflow-hash-384`, best-first ordering
  (`top: 0.254`), the uploaded document's id on every hit, and each hit's
  metadata strategy present — then the delete/cleanup steps still passed
- `db/schema.sql` syncs migration 009 (HNSW partial expression index);
  `EXPLAIN` on the live DB confirmed the planner uses
  `idx_embeddings_hnsw_cosine` for the `ORDER BY embedding::vector(384) <=> $q`
  top-K scan (matched because the query repeats the indexed cast expression
  and constrains `model = 'knowflow-hash-384'`, the partial predicate)
- After tests + e2e: DB back to seed — 2 orgs / 2 users / 6 docs / 0 chunks
  / 0 embeddings / 0 ingestion_jobs; :3000/:8001 free; no stray processes

**[Day 16] verification record:**

- **Evaluation harness**: `ai-service/evaluation/build_corpus.py` regenerates
  the 4 outline-annotated benchmark PDFs (retailer policies, platform notes,
  team handbook, service plans — each section page-aligned) into
  `ai-service/tests/fixtures/eval/`; `retrieval_quality.py` then runs 33
  golden queries (GOLD_SENTENCES embedded by the deployed hash provider,
  chunks produced by the app's own `chunk_pages` chunkers) → Tables A-E:
  Top-K, threshold, chunking, and metadata-filter sweeps, all measured on the
  real pipeline (see §16)
- **Harness correctness bug caught mid-run**: Hit/MRR best-chunk indexes were
  first computed against the *portal* pool instead of the global frames pool —
  fixed in the harness before any numbers were trusted
- **Backend** `npx tsc --noEmit` clean (config excludes `tests/`, so test
  files are only type-checked by vitest's esbuild at runtime); `npx vitest
  run` → **81/81 pass** (+3 unit, +4 integration for `minSimilarity` /
  `sourceType` / `metadata`). New unit asserts floor math becomes
  `distance <= 1 − minSimilarity` ($4 param, same toFixed(6) as the vector),
  NO floor when the knob is absent/0, and the `sourceType` ($4) +
  `metadata` ($5 jsonb) clauses. New integration (real pgvector, deterministic
  embedder): a near match at cosine 1.0 survives a 0.9 floor while a weak
  `[1,0]` axis (0.6) is cut at 0.7; the metadata section filter returns only
  the exact-section chunk and a bogus section returns empty; two relabeled
  docs (second `UPDATE`d to `source_type=md`) are gated correctly per `sourceType`;
  out-of-range knobs (bad enum, array metadata, `minSimilarity` 1.5/−0.1,
  `limit` 21) are 400s. The Day-15 limit unit test was realigned to the new
  options signature (`search(org, query, { limit: 1 })` → `{ limit: 1 }`)
- **Live e2e** `./e2e/run.sh` → **1 passed (~11.4 s)** with the banner
  reflecting Day-14's retrieval proof; search step returns `top: 0.254`;
  cleanup verified — DB back to seed (2 orgs / 2 users / 6 docs / 0 chunks /
  0 embeddings / 0 ingestion_jobs), :3000/:8001/worker free, no stray
  processes (the :5001 uvicorn is an unrelated project on the box)
- ai-service `python -m pytest` → **123/123 pass (×2 runs)**, including the
  fixture-generation suite (`test_eval_fixtures`)

**[Day 17] verification record:**

- **ai-service** `python -m pytest` → **143/143 pass (×2 runs)**
  (+10 `test_rag_api.py` API tests, +10 `test_rag_helpers.py` unit tests).
  API: `POST /v1/rag/generate` 200 with a grounded extractive answer +
  `evidence`; refusal when context is empty or too weak (with canonical
  "I don't know." and `refused: true`); out-of-range context / minScore /
  typo'd body / whitespace-only question are 422s. Helpers: `content_tokens`
  stopword filtering, sentence splitting, `overlap_ratio` thresholds,
  `format_context` reduces context costs, `is_refusal` recognizes the
  canonical string + near-variants, `_budget_context` keeps the strongest
  chunk even when it exceeds the cap
- **Backend** `npx tsc --noEmit` clean; `npx vitest run` → **95/95 pass**
  (+9 `rag.service.unit.test.ts`, +5 `integration.rag.test.ts`). Unit
  (fake search + fake generator): retrieve → best-first token-budget context
  → forward; knob passthrough (`limit`/`sourceType`/`metadata`/
  `maxContextTokens`/`minScore`); refusal maps as a 200-shaped response, not
  an error; generator failure → `503 RAG_GENERATION_UNAVAILABLE`;
  `buildContext` keeps the strongest oversized chunk, orders best-first,
  carries section metadata. Integration (real pgvector + deterministic
  embedder + injected fake generator): full pipeline returns `provider`/
  `model`/`evidence`/`usage` with best-first evidence; empty context →
  generator refusal 200; knob validations are 400s; missing/invalid token →
  401; generator down → 503 (app built with its own pool so the shared one
  survives close)
- **Live e2e** `./e2e/run.sh` → **1 passed (~11.4 s)** with RAG steps added:
  a grounded question returns `refused=False` with evidence (2 retrieved),
  and an out-of-scope question returns the refusal (not a hallucinated
  answer); cleanup verified — DB back to seed (2 orgs / 2 users / 6 docs /
  0 chunks / 0 embeddings / 0 ingestion_jobs), :3000/:8001/worker free

**[Day 18] verification record:**

- **ai-service** `python -m pytest` → **147/147 pass** (+2 `test_rag_helpers.py`
  citation units — `mark_citations` appends markers, `build_citations` maps
  markers to page/title/document, ignores out-of-range — and +2 `test_rag_api.py`
  assertions: a grounded extractive answer returns `citations[0]` = `{id, section,
  page, similarity}`; the `openai` path cites the model's referenced number).
- **Backend** `npx tsc --noEmit` clean; `npx vitest run` → **100/100 pass**
  (+5 `integration.sources.test.ts` on real Postgres: resolves a citation's
  `documentId` to source metadata + extracted page count; returns the full
  document fields incl. `createdAt`; hides foreign tenants with `404
  NOT_FOUND`; malformed id → 400; missing token → 401). `integration.rag.test.ts`
  also asserts `citations` map `id → title/page/section` through the RAG path.
- **Design** — the `[n]` marker doubles as the citation id, so mapping costs
  nothing; the page reference is the cited chunk's starting page, and the
  Source API hands the client the document facts (title/sourceType/size plus
  extracted `pages`) to render "Sources [n] Title — Page p".

**[Day 19] verification record:**

- **Backend** `npx tsc --noEmit` clean; `npx vitest run` → **123/123 pass**
  (+8 `conversations.service.unit.test.ts`, +15 `integration.conversations.test.ts`).
  Unit (mocked repository + fake RagService): create with a default title when
  none supplied; supplied title wins; `get` outside the org returns null (no
  existence leak); blank message rejected before touching the DB (400); message
  into a foreign/missing conversation → 404; **atomic** user+assistant insert
  with the assistant's citation payload (`citations[0]` = `{id, title, page}`,
  `refused`, `provider`) at positions 1 and 2; **rollback** (BEGIN without
  COMMIT) when the assistant insert fails; delete 404 on a missing row.
  Integration (real Postgres, fake embedder + injected fake generator):
  create/list pagination with most-recently-active ordering; empty transcript
  for a fresh conversation; message POST runs the RAG path and stores the
  user+assistant pair with `payload.citations`/`refused`/`provider`; a later GET
  replays the transcript with citations intact; multi-turn ordering across two
  messages (`user,assistant,user,assistant`, positions `1,2,3,4`); tenant
  isolation on list/get/message/delete (404, not 403); blank content 400;
  unknown body key 400; malformed id 400; missing token 401; delete removes the
  messages row (cascade, verified via SQL count == 0).
- **Two bugs caught** (see §19.3): pg `Pool.connect` loses `this` when
  destructured (broke the message transaction with `reading 'ending'`), and
  fast-json-stringify drops undeclared keys of a bare `{ type: "object" }`
  schema (the stored citation payload serialized as `{}` until
  `additionalProperties: true`). Both fixed and pinned in the test suite.
- **DB hygiene** — after the suite, `conversations`/`messages` have **zero**
  leaked rows; tenant orgs registered by the tests are deleted in cleanup.

**[Day 20] verification record:**

- **ai-service** `./.venv/bin/python -m pytest` → **157/157 pass**.
  +7 unit (`test_rag_helpers.py`): `referential` detects pronouns/anaphoric
  connectors and leaves standalone questions alone; `expand_referential_question`
  splices the thread anchor and clips the trailing `[n]` citation marker, passes
  standalone questions through, and is a no-op without history; `format_history`
  labels roles; the **extractive provider grounds a referential follow-up on the
  refund evidence** when history is present and **refuses the same question with
  no thread** (honest, never a guess); a recording provider confirms the
  generator facade ships `history` through. +2 API (`test_rag_api.py`): a
  follow-up with history resolves and cites over HTTP; `history` validation
  (optional, `user`/`assistant` roles only, no unknown keys, capped count → 422s).
- **Backend** `npx tsc --noEmit` clean; `npx vitest run` → **133/133 pass**.
  +5 `rag.service.unit.test.ts`: `expandQuestionForRetrieval` passes a standalone
  question through and expands a referential one with the thread anchor (no `[1]`
  residue); `RagService.generate` **searches and generates on the resolved query
  while reporting the raw question**, passes `history` to the generator, and ships
  `[]` history when none given. +4 `conversations.service.unit.test.ts`:
  `buildHistoryWindow` keeps a bounded recency window (oldest-first), drops the
  oldest turns over the token budget, excludes `system` frames, and returns empty
  for an empty transcript; `addMessage` passes the prior user+assistant turns as
  `history` to the RAG pipeline. +1 integration (`integration.conversations.test.ts`):
  the **second** message's RAG input carries the prior `{user, assistant}` turns
  and a rewritten question containing both "refund policy" and "international
  customers".
- **DB hygiene** — the multi-turn test registers a fresh org deleted in cleanup;
  `conversations`/`messages` cascade off `organizations` (ON DELETE CASCADE), so
  zero rows leak.

**[Day 21] verification record — RAG evaluation:**

The step that separates a real RAG project from a beginner portfolio: it does
not stop at "does retrieval turn up a related chunk" — it drives the **full
production RAG path** (extraction → paragraph chunking → hash-384 embedding →
cosine retrieval → token-budget context → extractive generation → citation
mapping) and grades the **answer** against a golden dataset.

- **Golden dataset** (`evaluation/rag_eval_dataset.py`): 33 rows of
  `(question, expected_answer, expected_document, expected_page)` authored
  sentence-by-sentence against the real eval corpus (retailer policies,
  platform notes, team handbook, service plans, chapter), with the 4
  outline-annotated PDFs from Day 16 (`build_corpus.py`) plus the no-outline
  `chapter.pdf`. Each `expected_page` is resolved from the **actual extractor
  output** at load time and asserted present — a golden sentence that drifts
  out of extraction fails loudly instead of grading against invented labels.
  Two cross-document distractor questions measure how the system resists
  wrong-evidence pulls.
- **Metrics** (`evaluation/rag_metrics.py`) — pure, I/O-free scoring pinned by a
  new `tests/test_eval_metrics.py` (**9 tests**): document hit@K and page hit@K
  (retrieval accuracy), Recall@K / Precision@K (denominator = all relevant
  chunks corpus-wide), answer correctness (content-token containment of the
  reference, ≥ 0.5), citation correctness (document **and** page resolve),
  and hallucination rate (non-refused answer sharing no content token with any
  context chunk — a refusal is never scored as a hallucination).
- **Harness correctness bug caught mid-run**: the evaluator builds
  `ContextItem` from raw cosine dot products, but the **production** retrieval
  emits `1 − cosine_distance` (backend `SearchService.toItem`) and the AI-service
  `ContextItem.similarity` wire contract bounds to `[0,1]`. A small cross-doc
  top-K produced a negative similarity and a pydantic `validation_error`. Fixed
  in the harness by clamping to the wire contract's `[0,1]`, matching what the
  deployed context actually delivers to generation. The metric runs are
  deterministic and repeatable.
- **Run** `.venv/bin/python evaluation/rag_evaluation.py` (self-contained,
  offline — only the pgvector SQL index and HTTP transport are delegated to the
  Day 15/16 + backend integration coverage):

```
corpus: 31 pages across 5 documents -> 9 chunks; golden set: 33 questions
RETRIEVAL  (graded against the expected document + expected page)
  k | document hit@k |  page hit@k |  recall@k | precision@k
  1 |          0.758 |       0.636 |     0.591 |       0.636
  3 |          0.970 |       0.909 |     0.879 |       0.313
  5 |          1.000 |       0.970 |     0.955 |       0.206
ANSWER QUALITY  (full RAG path: retrieval -> budget -> generate -> cite)
  answer correct                  : 0.788
  citation -> document correct    : 0.909
  citation -> page covers answer  : 0.970
  citation correct (doc + page)   : 0.879
  citation -> EXACT page match    : 0.273
  hallucination rate              : 0.000
  refused (honest 'I don't know.')     : 0.000
```
  Interpretation: retrieval is effectively perfect by top-5 (doc hit 1.000,
  page hit 0.970, recall 0.955). Answer correctness 0.788 — the extractive
  baseline, quoting verbatim, is faithful (hallucination 0.000, nothing made
  up) but occasionally latches a higher-overlap *wrong* chunk over the golden
  one; the 3 citation-document failures are exactly these cross-doc pulls and
  the per-query failure list names them. Precise page attribution is the
  weakest signal (exact-page 0.273 vs page-covers 0.970) — the known
  paragraph-512 section-merge debt (#24), not retrieval failure. These
  numbers are the honest baseline the Day 24 retrieval-quality regression and
  a future `openai` generation provider will be measured against.
- **ai-service** `./.venv/bin/python -m pytest` → **166/166 pass** (+9 new
  `test_eval_metrics.py`; the metric contract is regression-guarded).

## 7. Dev-log notes worth keeping

- **Fastify encapsulation is the #1 footgun.** Hooks registered inside a
  `register`ed plugin only apply within that plugin's encapsulated context;
  middleware meant to be app-global must wrap itself in `fastify-plugin`
  (see `backend/src/middleware/*`). Same trap bites Fastify v5's ajv: the
  default `removeAdditional: true` silently strips unknown payload fields —
  `backend/src/app.ts` sets `ajv.customOptions.removeAdditional: false` so
  `additionalProperties: false` really rejects.
- **`@fastify/jwt` v9 is fast-jwt, not jsonwebtoken.** The issuer option is
  `iss` (sign) and `allowedIss` (verify), not `issuer`. The error message
  says it reads like a type error and smells like a version bump.
- **bcryptjs was a deliberate choice** over native `bcrypt` — pure JS, no
  prebuilt binaries, cost 10 is fast enough at this scale.
- **psycopg3 writes are only real once they COMMIT to another connection's
  eyes.** A plain `conn.execute()` leaves the connection *implicitly* in a
  transaction, and `with conn.transaction():` does **not** commit at exit in
  that state — it joins the open implicit transaction and leaves the closing
  COMMIT to an outer scope that never runs. Day 9's processor looked like it
  was persisting pages and `ready` status (the API said so!) while the rest of
  the world still saw `queued`, and the connection leaked `idle in
  transaction`. The fix is boring and correct: `conn.autocommit = True`
  (statement = unit of work), then `with conn.transaction():` issues a real
  `BEGIN`/`COMMIT`. The design rule that catches regressions: DB integration
  tests must assert committed state through a *second* connection, because
  same-connection reads can always see uncommitted self-writes.
- **UUID columns come back as UUID objects** in psycopg3's `dict_row`; coerce
  at the repository boundary (`str(row["id"])`) so services can treat ids as
  strings everywhere.
- **Test invariants, not vibes — an output that "looks right" is how bugs
  commute.** Day 10's first overlap hop measured only the *last word* instead
  of the accumulated tail, so each window advanced one word: 44 degenerate
  chunks from a 590-token chapter. The suite's invariant tests (exact
  word-coverage reconstruction, genuine inter-chunk sharing) caught it
  instantly; a "surely the output is fine" glance would have shipped the bug.
- **PDF text extraction almost never yields blank lines.** pypdf/reportlab
  collapse every paragraph boundary into a single `\n`, so a paragraph
  strategy that only watches for `\n\n` silently degrades to page-level
  chunks on real documents. Day 10 added a bounded detector instead
  (sentence-end + heading-like line breaks) and kept a *renderer-dependent*
  reality in the tests: the real-PDF test asserts sentences survive, while
  `\n\n` behavior is pinned by synthetic fixtures.
- **Retry policy is a classifier, not a flag.** The embedding pipeline asks
  *why* a call failed before deciding: `429`/`5xx`/timeouts/transport errors
  are retried with exponential backoff; every `4xx` is fatal immediately.
  Licensing it this way makes the retry unit testable — the suite pins exact
  call counts per batch (2+2+1 for three single-text batches with two
  transient failures), which a boolean "retry: true" setting cannot express.
- **A run must never half-persist.** The embedder validates whole batches
  (one consistent dimension, finite values) before any insert, and the
  inserts + `embedding_status='ready'` flip share one transaction. A mid-run
  provider/model change or crash therefore leaves zero rows or a complete set
  plus an explicit `failed` reason — never 3/5 vectors and a "ready" document.
- **Claim order matters.** The embedder lists chunks *before* the claim:
  refusing a chunk-less ready document must not flip `embedding_status` at
  all. The unit test that asserted this caught the original ordering, where
  a no-chunk doc would have been wedged as `processing` forever.
- **The upload must never depend on the queue.** Day 12's
  enqueue-on-upload is strictly best-effort: a missing AI service or a dead
  Redis must not take an upload down with it, so the enqueue call has a 5s
  timeout and swallows errors — the document stays `queued` and a later sweep
  (Day 13) repairs it. Coupling the hot upload path to the queue's liveness
  would turn a convenience into an SLO.
- **Keep the durable ledger in Postgres, the transient queue in Redis.** RQ
  job registries vanish when Redis is flushed; `ingestion_jobs` rows are the
  source of truth for *what was done* and *what may retry*. The queue is
  re-drivable (re-submit a task) because every phase re-checks its `QUEUED`
  claim gate before running — enqueueing twice or replaying a task after a
  flush is a no-op, not a double-run.
- **Retry budgets live on the work item, not the broker.** RQ's built-in
  retry exhausts per *submission*, which silently loses the story across a
  re-queue. Day 12 keeps `attempts`/`max_attempts` on the `ingestion_jobs`
  row, bumps on claim, and let`exhaustion` fail the job permanently while a
  still-budgeted transient failure re-queues with RQ's `enqueue_in`
  exponential backoff (`QUEUE_RETRY_BACKOFF`, doubled per attempt). The
  budget and the reason travel with the work.
- **Retry is a classifier again.** Process/embed job failure details are
  classified terminal vs transient per phase: `not_found`/`unsupported_type`
  (process) and `not_found`/`not_embeddable` (embed) are always terminal; the
  embedder's explicit `not retryable` / `retries exhausted` reasons are
  terminal; everything else — crashes, downloads, provider hiccups — is
  transient and retried. A terminal outcome is *recorded* (job `failed`,
  reason preserved) and must not double as "please retry".
- **RQ 2.x moved the timeout off the worker.** `Worker(default_timeout=…)`
  died in the major bump; the job timeout is now a `Queue` attribute set at
  construction (`queue.default_timeout = settings.queue_worker_timeout`) in
  both build helpers (`build_api_queue_service`/`build_default_queue_service`).
  RQ tasks also need their args at submit time — `submit(func_name, delay,
  *args)` carries `document_id`, since an arg-less enqueue fails with
  `run_job_*() missing 1 required positional argument`.
- **A cross-tenant miss is a 404, never a 403.** Day 13's get/delete/status
  all anchor on `WHERE id AND organization_id` — a document that exists under
  another org collapses into "not found". 403 is reserved for the *known*
  caller (a member hitting the owner-gated DELETE); 404 hides existence. The
  distinction is deliberate and both are tested.
- **Pagination honesty needs the total in the same statement.** Day 13
  computes `total` with `count(*) OVER()` next to the page rows (one query,
  no TOCTOU between a count and a page read), and `hasMore` as
  `offset + items.length < total`. A separate COUNT would safely diverge
  under concurrent uploads. PG returns that window total as a `bigint`
  (string in the pg driver) — coerce with `Number()`, and it's a good
  reminder every count column in a status/aggregate row is a string.
- **The status endpoint is a derived projection, not a column.** `stage` is
  computed on read from `status` + `embedding_status` (+ thread-safety),
  mirroring the AI service's queue `derive_state` so both stacks tell the same
  story: a document is READY exactly when its document lifecycle completed
  AND its vectors landed. No migration was needed for Day 13 — the API is a
  query-layer feature, which is what made it cheap.
- **Search the model that indexed the corpus, or search noise.** Day 15's
  backend never assumes a model name: the query vector comes from the AI
  service's `/v1/embeddings` and the *response's* `model` string is the SQL
  key (`e.model = $3`). Naively re-embedding with a hardcoded model would
  compare the query against a different eigenspace and return garbage ranks —
  and a future provider swap is free because the model travels with the
  vector.
- **pgvector HNSW needs a fixed dimension; keep the column free.** Day 15
  resolved the Day-11 deferral without giving up multi-model storage: the
  index is a **partial expression index** —
  `USING hnsw ((embedding::vector(384)) vector_cosine_ops) WHERE model='knowflow-hash-384'`.
  The gotcha that looks like a bug: the planner only matches it when the
  query repeats the *same cast expression* in `ORDER BY`
  (`embedding::vector(384) <=> $q`), which `EXPLAIN` confirmed. A bare
  `embedding <=> $q` would silently do an index-less scan. And a future
  1536-D model gets its own `::vector(1536)` index — the schema never bakes
  in a dimension.
- **A 503 beats an empty result for a dead embedder.** Day 15's search maps
  an unreachable AI service to an explicit `EMBEDDING_UNAVAILABLE` 503 (with
  the transport detail) rather than returning "no results" — an empty list
  looks like a legitimately empty corpus and hides an outage. The search
  client carries the same short timeout discipline as the enqueue client, so
  a hung service cannot hang a search either.
- **Evaluate retrieval, or you are guessing.** Day 16 built the golden set
  *first* (verbatim answer sentences as reachable ground truth) and let the
  numbers make the design calls. Top-k=5 was already right (0.986 Cov@5,
  saturating by 10); the similarity threshold everyone "feels" should filter
  noise was **wrong** for this geometry — every positive floor collapsed
  recall (hash embeddings put genuine hits low, so 0.2 ≈ half of coverage
  gone). Metadata filtering *seemed* obviously good and measured *worse*
  than none on the production chunker, then clearly better once attribution
  was intact. Without the harness the knobs shipped on vibes; with it they
  shipped with a documented operating envelope.
- **`minSimilarity` is implemented as `distance <= 1 − minSimilarity`.** The
  search body takes a *similarity* floor (0-1, user-friendly) but pgvector
  ranks by cosine *distance* and the stored vectors are L2-normalized, so the
  repository maps the floor at the parameter boundary — and reuses the same
  6-decimal literal precision as the query vector (an exact re-embed must
  still pass the floor). The unit test pins that arithmetic so the two scales
  can't drift out of sync.
- **Dynamic SQL stays list-winded.** The repository concatenates filters into
  a single parameterized statement (`$1` org, `$2` vector, `$3` model, then
  sourceType / metadata jsonb / floor as late params, LIMIT last) rather than
  switching prepared-statement shapes per predicate combination — one plan
  family, `additionalProperties:false` in the schema don't gate it, and the
  jsonb containment operator (`@>`) is what makes metadata a working exact
  filter on `chunks.metadata`.
- **Let the generation layer decide "I don't know", not the transport.** Day
  17's RAG generation is a 200 with `refused: true` and the canonical "I
  don't know." when there is nothing trustworthy to say — it is *never an
  error status*. A refusal is an ordinary, well-formed answer; treating it as
  a 4xx or 5xx would conflate "the model declines to answer" with "the
  pipeline broke". Both the backend (`RagService` → response) and the AI
  service (`generator` → answered response) preserve this shape.
- **The generation provider is a swap seam, and the default is deterministic
  and grounded.** Day 17 defaults the AI service to `extract` (not a real
  LLM): it quotes the verbatim sentence with the highest content-token
  overlap against the context and refuses when there is no overlap — so a
  wrong call is *impossible by construction* and the SLI "is the answer
  faithful" is trivially green. The `openai` provider lives behind the same
  `RagProvider` Protocol and only needs prompting, and the `ChatCompleter`
  type is a Protocol (not an import of the concrete LLM class) to dodge the
  service-import cycle. Provider/model/refusal all appear on the response so
  the caller and the logs can see *how* an answer was produced.
- **Two layers budget the context; the strongest chunk always survives.** The
  backend greedily packs top-K chunks by `tokenCount` under
  `maxContextTokens` (best-first, so it can be cut anywhere) and the AI
  service defensively re-budgets the same contract — belt and suspenders
  against a callers-tuned budget. The single rule that makes truncation
  safe: the highest-similarity chunk is always kept even if it alone exceeds
  the budget, so the answer can never fall back to an empty context.
- **A refused generation is cheaper than a real one.** The backend forwards
  `minScore`, and the generator refuses *before* doing any LLM work when the
  best evidence falls below it or the context is empty — there is nothing
  profitable to generate over empty context, so short-circuiting is both
  correct and fast.
- **Call pg's `Pool.connect` as a method, never destructured.** Day 19's
  message transaction reached for a dedicated connection via
  `const connect = this.db.connect; connect()` and got
  `Cannot read properties of undefined (reading 'ending')` — pg's pool relies
  on `this` being the pool. `this.db.connect()` preserves it; type the surface
  loosely and keep the call on the object.
- **A bare `{ type: "object" }` schema collapses in fast-json-stringify.** Day
  19 stored each assistant message's citation provenance in a free-form `jsonb`
  `payload`, and the response serialized every one as `{}` — the DB was
  correct, only the JSON schema was starving the serializer of the fields to
  emit. For a genuinely free-form field, declare `additionalProperties: true`
  (or enumerate the properties); otherwise undeclared keys are silently
  dropped, which is invisible until a client reads an empty object.
- **Referential disambiguation must happen where retrieval happens — the
  backend.** Day 20's "What about international customers?" cannot ground on
  the refund evidence unless pgvector retrieval runs on a *resolved* query, and
  retrieval runs in the backend, *before* the AI service is contacted. So the
  query rewrite lives in `RagService` (and the extractive provider defensively
  re-resolves), the resolved query drives both retrieval and generation, and
  the raw user question is preserved for persistence/reporting. Splitting
  "conversation understanding" across the two services would leave one path
  (retrieval) blind to the thread.
- **The citation marker tells you nothing about a new turn — drop it before
  reuse.** Day 20 splices the last assistant reply into the rewrite; the
  trailing `[n]` (e.g. "The refund period is 30 days. [1]") is formatting, not
  retrieval content, so the splice strips it. Keeping it would pollute the
  pgvector query with `[1]`, which no chunk contains.

## 8. Day-8 upload path (as built)

```
POST /api/v1/documents  (Bearer JWT, multipart form, single "file" part)
  │  @fastify/multipart rawBody → whole part in a Buffer (debt #10)
  │
  ▼
 FileValidationService
  │ 1. sanitizeFilename: drop ../, strip C:\ drive paths, collapse dots
  │ 2. extension allow-list: pdf | docx | md | html | txt  → 415 if not
  │ 3. magic sniff via Buffer.equals prefix (NOT includes, which false-
  │    positives anywhere in the file — a bug caught by unit test):
  │      pdf  → "$PDF-"
  │      docx → "PK\x03\x04" (zip)
  │      md/html/txt → no NUL bytes for first N bytes (no binary smuggling)
  │    extension vs bytes mismatch → 400 FILE_TYPE_MISMATCH
  │ 4. client MIME header is ignored — trust bytes, not the caller
  │
  ▼
 DocumentsService.upload (transaction-ish, compensating)
  │ 1. key = "{org_id}/{uuid}{.ext}"  → two uploads of one file = two docs
  │ 2. S3StorageClient.put (MinIO, NodeHttpHandler 4s timeouts)
  │ 3. INSERT into documents (status 'queued' — parsing is Day 9+)
  │ 4. on insert failure → S3StorageClient.delete(key)  (no orphaned bytes)
  │
  ▼
 201 → the document row (documents.updated_at trigger — migration 003 —
        now visibly ticks because uploads are real writes)
```

Design note worth keeping: the *least-trust* ordering. Sanitize → allow-list →
sniff bytes → enforce size at the transport cap → persist. Validation runs
before any write to the bucket, so spoofed files never occupy storage; the
only compensating action needed is for the DB-write failure leg.

## 9. Day-9 extraction path (as built)

```
POST /v1/process/documents/{document_id}   (ai-service, work trigger)
     └─ python -m app.processor.cli --document-id <uuid>   (ops/backfill equivalent)
  │
  ▼ ProcessorService.process
  │ 1. repo.get(id) : not_found? 404 | source_type != pdf? 422 unsupported_type
  │                     | status != queued? 409 not_queued   (all leave it alone)
  │ 2. repo.claim(id)  UPDATE documents SET status='processing'
  │                     WHERE id=$1 AND status='queued' RETURNING ...   (atomic)
  │                     → two workers cannot both grab the doc (no queue yet)
  │ 3. S3Downloader.get(storage_key)   (boto3 → MinIO, path-style, 5s connect)
  │ 4. PdfExtractor.extract(bytes)     (pypdf, strict=False, page_limit guard)
  │      · one PageRecord per page: page_number 1..N, whitespace-normalised
  │      · section ← nearest preceding PDF outline/bookmark title (real signal)
  │      · source ← "s3://{bucket}/{key}"  (citation provenance)
  │      · empty pages skipped; failed text streams → page_errors, not doom
  │      · corrupt bytes → PdfError (document-level failure)
  │ 5. tx: insert_pages → mark_ready   (single real COMMIT, autocommit=True)
  │      · any failure in 2-5 → mark_failed(reason) into documents.error
  │
  ▼ result {status, pages, page_errors, total_tokens, empty, truncated}
```

Citation ground-truth lives in `document_pages`: `document_id` + `page_number`
are the anchor, `section` is the structural heading, `source` is the object
URI. Day-10 chunking will consume these rows and carry page_range + section
into `chunks`, and Day-18 citations will cite back along that chain.

## 10. Day-10 chunking path (as built)

```
POST /v1/process/documents/{id}   body: {chunk_strategy?, chunk_size?, overlap_tokens?}
  │  chunk_strategy ∈ {fixed, token, overlap, paragraph}   (default paragraph)
  │  chunk_size 16..4096 | overlap_tokens < chunk_size | unknown strategy → 422
  ▼ ProcessorService.process(strategy=…)
  │ 1-4. unchanged Day-9 claims/download/extract (pages with section+source)
  │ 5. Charter.chunk_pages(pages, strategy, budget…)   — app/processor/chunkers.py
  │      · _split_paragraphs: blank-line runs OR sentence-end OR
  │        heading-like line (≤60 chars, no ending punct, next starts upper)
  │      · merge-until-budget; hard-split over-budget units; clamp [16, 4096]
  │      · each ChunkRecord stamps metadata {strategy, section, source,
  │        page_range} and carries page_number = first page of span
  │ 6. tx: insert_pages → insert_chunks → mark_ready  (single real COMMIT)
  │      · chunker ValueError (params) → mark_failed("chunking parameters
  │        rejected") — document fails cleanly, no worker crash
  ▼ result {status, pages, chunks, chunk_strategy, total_tokens}
```

`chunks(id, organization_id, document_id, content, chunk_index, page_number,
token_count, metadata jsonb, created_at)` with `UNIQUE(document_id,
chunk_index)` for idempotent repairs; trgm GIN for future hybrid search.
Migration `006` dropped the Day-3 v1 skeleton (`chunks(seq)` +
hand-seeded `embeddings`); `db/seed.py` stopped shipping fake vectors and is
now a tenant-data reset. `chunks` and pages share the processor transaction,
so a `ready` document always has its chunks (Day 11 embeds `chunks.id`).

### Strategy evidence (measured, not argued)

Same 6-page chapter.pdf (~2,336 chars, ~590 tokens) at budget 128, run on the
live DB, re-reconstructed from the actual inserted rows:

| strategy | chunks | tokens | redundancy | trade-off |
|---|---|---|---|---|
| fixed | 19 | 589 | ~5 | byte-predictable, splits sentences mid-thought |
| token | 5 | 587 | ~3 | classic RAG baseline, word-aligned |
| overlap | 6 | 744 | +160 | keeps boundary context, pays for it |
| **paragraph** | **5** | **590** | ~6 | never splits a sentence/section — the production default |

Paragraph won on the only metric retrieval cares about later (Days 15-18):
whole, citation-safe units at token-count parity with the greedy baseline.
Overlap stays available as a per-call opt-in for boundary-sensitive workloads
and is the strategy that will inform the Day-17 embedding-window design.

## 11. Day-11 embedding path (as built)

```
POST /v1/embed/documents/{id}   (ai-service; model comes from config, not the caller)
  │  └ python -m app.embedding.cli --document-id <uuid>   (ops/backfill equivalent)
  ▼ EmbeddingPipeline.embed_document(id)   — app/embedding/service.py
  │ 1. repo.get_document(id): missing → 404 | embedding_status ready/processing
  │    → 409 (refused, not double-embedded) | status != ready → 409
  │ 2. repo.list_chunks(id) BEFORE the claim; zero chunks → 409 (never faked,
  │    never wedged — a pre-pipeline seed row stays refused)
  │ 3. repo.claim_for_embedding(id)  UPDATE documents SET embedding_status=
  │    'processing' WHERE id=$1 AND embedding_status IN ('none','failed')
  │    RETURNING id   (atomic; two workers cannot double-embed)
  │ 4. embed in slices of EMBEDDING_BATCH_SIZE (default 64)
  │      · per-slice retry with exponential backoff: timeouts, transport
  │        errors, 429, 5xx are retried up to EMBEDDING_MAX_RETRIES (default 3)
  │      · any 4xx is fatal immediately (a rejected input/key won't heal)
  │      · per-run validation: one consistent dimension + finite values
  │      · provider = existing EmbeddingService seam (hash | openai), reused
  │ 5. tx: insert_embeddings (ON CONFLICT (chunk_id, model) DO UPDATE) →
  │      mark_embedding_ready   (single real COMMIT, autocommit=True)
  │      · any un-recovered failure → mark_embedding_failed(reason) into
  │        documents.embedding_error — zero vectors or all vectors, never half
  ▼ result {status: embedded|failed|not_found|not_embeddable, chunks, vectors,
            model, dimensions}
```

`embeddings(id, organization_id, chunk_id, model, dimensions, embedding,
created_at)` with `UNIQUE(chunk_id, model)`; `embedding` is a dimension-
flexible `vector` and `dimensions` records each row's true length, so the
local 384-D hash provider and real models (1536/3072-D) coexist per chunk
(Day 3's multi-model plan, now fed by the pipeline instead of the seed
script). Retrievability is the conjunction of two statuses:
`documents.status = 'ready' AND documents.embedding_status = 'ready'` — that
is exactly the predicate Day 15's retrieval and Day 17's RAG will use.

## 12. Day-12 queue path (as built)

```
upload → POST /api/v1/documents (backend)
  └─ DocumentsService.tryEnqueue → POST /v1/queue/documents/{id}/process   (best-effort, 5s)
        upload still returns 201 no matter what; doc stays 'queued' if enqueue died

  ▼ QueueService.enqueue_process (ai-service, /v1/queue/*)
  │ 1. ensure row  INSERT INTO ingestion_jobs (document_id, kind) VALUES (…, 'process')
  │      ON CONFLICT (document_id, kind) DO NOTHING   → enqueueing twice is a no-op
  │      kind ∈ {process, embed}; policy errors (unknown doc/embed-already) → 404/409
  │ 2. submit RQ task run_job_process(document_id) to knowflow-redis (:6375)
  │      queue.default_timeout = QUEUE_WORKER_TIMEOUT   (timeout lives on the Queue, not the worker)

  ▼ RQ worker (python -m app.queue.worker, pool) pops the task →
  │   run_job_process: claim → process → chain embed
  │ 1. repo.claim_job(id, kind)  UPDATE ingestion_jobs SET status='processing',
  │      attempts=attempts+1, started_at=now() WHERE status='queued' RETURNING …
  │      (atomic → a duplicate worker task claims nothing and exits 'ignored'; a retry
  │       after a crash re-claims the same row, never double-runs)
  │ 2. resurface the failure: a previously-failed processing phase must not stay wedged,
  │      so a queued job whose document reads ready/failed already recovers the old
  │      reason (failed → the retry classifier re-queues or fails again)
  │ 3. run the Day-9/10 processor inline (claim/download/extract/chunk/store, one COMMIT)
  │ 4. on success → _chain_embed: mark job 'succeeded' + enqueue run_job_embed(document_id)
  │    on failure → classify + _settle: transient & budget left → enqueue_in(backoff²) re-run;
  │      else → job 'failed' + reason preserved; terminal business outcomes (unsupported
  │      type, corrupt PDF, exhausted unretryable) leave documents.status 'queued'/failed
  │      — never a wrong "ready", never a silent wedged state

  ▼ run_job_embed (second worker run, same dances)
  │ 1. claim ingest embed row (queued→processing), failures resurfaced
  │ 2. run the Day-11 embedder inline (chunks → vectors, validate → one COMMIT)
  │ 3. success → 'succeeded' | transient+budget → backoff re-run |
  │      terminal → 'failed' + reason (not_found / not_embeddable /
  │      "not retryable" / "retries exhausted")

  ▼ GET /v1/queue/documents/{id} → derive_state(…)
      UPLOADED (queued doc, no job) → PROCESSING (any job processing)
      → EMBEDDING (process done, embed processing) → READY (doc ready + embedded)
      | FAILED (any job failed, or doc/embedding lifecycle failed) — reason+job shown
      reads Postgres only, so queue outages never hide where a document is
```

`ingestion_jobs(document_id, kind, status, attempts, max_attempts,
enqueued_at, started_at, finished_at)` with `PRIMARY KEY(document_id, kind)`
and a `touch_updated_at` trigger. The underlying phase work (`/v1/process`,
`/v1/embed`) and the CLIs are unchanged and reusable — the queue is an
adoption layer, not a rewrite: the same claim gates (`status='queued'`,
`embedding_status IN ('none','failed')`) that made the old triggers safe now
make replayed tasks safe. Redis is disposable: flush it and re-submit; the
ledger still knows every phase's real outcome.

## 13. Day-13 document management API (as built)

```
/api/v1/documents  (all authenticated; tenant = request.user.org, never the client)
│
├─ POST /                      [owner, member] multipart upload (Day 8, unchanged)
│
├─ GET  /?limit&offset&sourceType&status&embeddingStatus&search
│     limit 1..100 (def 50), offset ≥ 0            (schema-enforced; bad/typo'd
│     filters → 400 VALIDATION_ERROR via additionalProperties:false + enums)
│   ▼ listDocumentsByOrg(db, org, opts)            [document.repository.ts]
│     · dynamic WHERE: org AND (source_type|status|embedding_status = …)
│       AND (title ILIKE $n OR filename ILIKE $n)   — LIKE wildcards escaped
│       (%/_) so a search term matches literally
│     · SELECT …, count(*) OVER() AS matching_total
│       ORDER BY created_at DESC LIMIT $ OFFSET $  — total in the same statement
│   → { items: DocumentView[], pagination: { limit, offset, total, hasMore } }
│
├─ GET  /:id                   [owner, member]
│   ▼ getDocumentByOrg(db, org, id)  WHERE id AND organization_id
│   → DocumentView (adds error/embeddingStatus/embeddingError to the upload view)
│   unknown OR other tenant → 404 NOT_FOUND (existence never leaks)
│
├─ DELETE /:id                 [owner ONLY — members 403 FORBIDDEN]
│   ▼ remove(org, id)
│     · DELETE … WHERE id AND organization_id RETURNING storage_key
│         → children cascade: document_pages → chunks → embeddings →
│                            ingestion_jobs (all FKs carry ON DELETE CASCADE)
│       returns NULL → 404 NOT_FOUND
│     · then storage.delete(storage_key) best-effort (seed rows: key NULL → skip)
│   → 204; row + bytes always vanish together; a dead bucket may orphan bytes,
│     never resurrect a row
│
└─ GET  /:id/status            [owner, member]
    ▼ getDocumentStatus(db, org, id)
      · document row + correlated counts: document_pages, chunks,
        embeddings (via chunks — one per (chunk, model))
    → { status, error, embeddingStatus, embeddingError,
        counts: { pages, chunks, embeddings }, stage }
      stage = deriveStage(status, embeddingStatus):  failed > ready > embedding >
              processing > uploaded   (failed wins; READY requires both lifecycles).
      This mirrors ai-service derive_state, so the status API and /v1/queue report
      the same five states from separately-owned queries.
```

Design notes: **(1)** no migration was needed — Day 13 is a query-layer
feature (the columns and cascade graph have existed since Days 7-12).
**(2)** Deletion is the one owner-only action; everything else is open to any
member, which is a *deliberate* authz posture for a small-team tenant rather
than a quirk. **(3)** `search` is an ILIKE over `title`/`filename` only — the
Day-15 retrieval API is where *content* search lands; this filter is
metadata management, and the schema (max length 200) keeps it honest.
**(4)** the detail view surfaces `embeddingStatus`/`embeddingError`, so
"is this retrievable yet?" (`status=ready AND embeddingStatus=ready`) is
answerable from the management layer before Day 15's search exists.

## 14. Day-14 end-to-end integration test (as built)

```
./e2e/run.sh                          # the whole stack for the test, then gone
│
├─ prerequisites: :3000/:8001 free; Postgres (:5434), Redis (127.0.0.1:6375),
│     MinIO (:9000) reachable — else exit with the reason before any boot
├─ boot backend:    setsid bash -c 'cd backend && exec npm run dev'        → :3000
├─ boot ai-service: setsid … .venv/bin/uvicorn app.main:create_app --factory … :8001
├─ boot worker:     setsid … .venv/bin/python -m app.queue.worker          (1 RQ worker)
│     each service is its OWN process GROUP (setsid) → trap kills the whole tree
│     (npm → tsx watch → node), the earlier per-PID kill orphaned node on :3000
├─ ./e2e/test_pipeline.py              (pytest; @live guard → `skip` when stack down)
│   1. register throwaway org          POST /auth/register        [public HTTP only]
│   2. upload PDF                      POST /api/v1/documents (multipart)
│   3. wait_for_ready                  GET /api/v1/documents/{id}/status
│        polls to terminal stage; asserts READY: stage/status/embeddingStatus ready,
│        errors null, counts 6/2/2 (extract → 6 pages, chunk → 2, embed → 2)
│   4. queue cross-check               GET /v1/queue/documents/{id}
│        READY, process_job + embed_job both succeeded (attempts 1)
│   5. second-connection verify        mypsycopg, row_factory=dict_row
│        rows exist for document/pages/chunks/embeddings; embeddings 384-D
│   6. verify_pgvector_queryable
│        · every stored vector: 384-D, finite, nonzero   (parse ::text in Python —
│          l2_norm() is ambiguous under pgvector 0.8, halfvec overload)
│        · two distinct chunks → different vectors (cross join, distance > 0)
│        · RE-EMBED chunk text via POST /v1/embeddings {"text": str},
│          then SELECT embedding <-> %s::vector → dist ≈ 0 (deterministic hash)
│   7. semantic search (Day 15)        POST /api/v1/search {query, limit}
│        model = knowflow-hash-384, best-first similarity, every hit's
│        document id == the uploaded doc, metadata strategy present
│   8. cleanup via the API             DELETE /api/v1/documents/{id} → 204,
│        GET /:id → 404
│   9. finally: direct-SQL org delete; assert_cleanup counts 0 leaked rows in
│        document_pages / chunks / ingestion_jobs (by document_id) and
│        embeddings (joined to chunks — it carries chunk_id, not document_id)
└─ milestone gate banner on green: "upload -> storage -> queue -> worker ->
     extraction -> chunking -> embedding -> pgvector -> READY"
```

Design notes: **(1)** the *pipeline* is exercised strictly through public HTTP
(register/upload/status/queue/embeddings/DELETE) — SQL appears only to
*verify* committed state on a second connection and to drop the throwaway org
in cleanup; the rule "upload → READY needs no manual DB" is exactly what the
test witnesses. **(2)** it is a guard-disabled live test with its own
bootstrap script, so it stays out of `pytest` runs that expect a cold-started
service and out of the backend suite entirely. **(3)** the checks that
matter most are the ones that answer *would a broken provider lie here*:
distance-to-stored-vector, distinct-chunks-not-colliding, and 0 leaked rows
after delete — retrieval (Day 15) trusts exactly these invariants.

## 15. Day-15 semantic retrieval (as built)

```
POST /api/v1/search  (Bearer JWT, any member; tenant = token org, never the client)
  │  body: { query: string 1..500 (must contain \S), limit: int 1..20 (def 5) }
  │        additionalProperties: false — a typo'd key / blank query is a 400
  ▼
 SearchService.search(org, query.trim(), limit)          [search.service.ts]
  ├─ HttpQueryEmbedder.embed(query)   POST {AI}/v1/embeddings {text}   (5s timeout)
  │     → { vector, model }            — model string from the AI service's
  │       response keys the search: "embed in the model that indexed the corpus"
  │     failure → AppError EMBEDDING_UNAVAILABLE, 503 (never a silent empty list)
  ▼
 searchChunksByOrg(db, org, model, vector, limit)        [search.repository.ts]
  SELECT c.id, c.chunk_index, c.page_number, c.token_count, c.content, c.metadata,
         d.id, d.title, d.filename, d.mime_type, d.source_type,
         (e.embedding::vector(384) <=> $2::vector)::float8 AS cosine_distance
  FROM embeddings e
  JOIN chunks c    ON c.id = e.chunk_id
  JOIN documents d ON d.id = c.document_id
  WHERE d.organization_id = $1            -- tenant, server-side
    AND e.model = $3                      -- the query's own model
    AND d.status = 'ready'                -- retrievability gates
    AND d.embedding_status = 'ready'
  ORDER BY e.embedding::vector(384) <=> $2::vector ASC
  LIMIT $4
  → each hit: { chunk{id,chunkIndex,pageNumber,tokenCount,content},
                similarity: 1 - cosine_distance,
                document{id,title,filename,mimeType,sourceType},
                page, metadata }           — the full Day-15 wire contract
```

Search = 4 clauses, but each carries policy: the **tenant** filter makes
cross-org chunks unreachable in SQL (the 404-not-403 posture, applied to
vectors); the **model** filter makes retrieval immune to provider swaps (the
embedder's model string is the key, nothing is hardcoded); the **ready**
gates make search trustworthy only as soon as the pipelines actually finished
(`status` + `embedding_status`, the Day-11 retrievability predicate);
the **cast order-by** makes the index usable.

The ANN index (migration `009`) is a **partial expression index** because
`embeddings.embedding` is dimension-flexible on purpose:

```
CREATE INDEX idx_embeddings_hnsw_cosine
    ON embeddings USING hnsw ((embedding::vector(384)) vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE model = 'knowflow-hash-384';
```

(expression = HNSW needs a fixed dimension but the store hosts many; partial
= only the model-of-record, so a future 1536-D model gets its own index;
`EXPLAIN` confirmed the `ORDER BY embedding::vector(384) <=> $q` top-K uses
`idx_embeddings_hnsw_cosine`.) Design notes: **(1)** the backend never embeds
— it delegates to the AI service and takes the model name from the response,
mirroring the Day-12 queue-forward relationship. **(2)** search is a read and
is open to any member, consistent with the management API. **(3)** similarity
is 1 − cosine distance (the stored vectors are L2-normalized by the hash
provider, so cosine behaves as expected); the query vector literal is kept at
6 decimals to match stored precision, so an exact re-embed lands at distance
≈ 0 — verified in the e2e plus unit-test pinned.
## 16. Day-16 retrieval quality (as built)

Day 16's premise: don't tune retrieval by taste — build a labeled benchmark,
run the *real* pipeline over it, and let the tables pick the defaults. The
harness (below) appends two files to `ai-service/evaluation/` and the
product change they justified is three optional fields on the Day-15 search
body. Nothing retrofits the ingestion path: the knobs are *post-index*
filters + a ranking floor, exactly as the experiment scoped.

### 16.1 The benchmark

- **Corpus**: `build_corpus.py` renders four outline-annotated PDFs with
  reportlab — retailer policies (7 sections), platform notes (6), team
  handbook (6), service plans (6) — each section starting on its own page so
  the extractor's page-granular outline resolution yields clean section
  labels (same convention as the `three_pages.pdf` fixture). The Day-9
  `chapter.pdf` is loaded unchanged: *no* outline, so `section=None` and no
  result depends on section luck. 5 docs, 31 pages.
- **Queries**: 33 hand-authored questions, each with a ground-truth answer
  sentence (a verbatim golden string that exists in the corpus).
- **Scoring**: every chunk and query is embedded by the deployed
  `knowflow-hash-384` provider (the same one that indexed the store — the
  hash model is deliberately the measurement ceiling); chunks come from the
  app's own `chunk_pages` chunkers, not a toy reimplementation. Metrics:
  `Coverage@k` (gold-token coverage across the top-k context — "can RAG
  answer from this cut?"), `Hit@k` (is the most answer-dense chunk in the
  cut), `MRR`, and mean best-chunk **page-span** (how many physical pages an
  answer's best unit spans — wide spans inflate coverage but approximate
  citations). A similarity floor, when requested, is applied *after*
  ranking (the product contract), and the threshold curves count `kept` =
  mean surviving hits.

### 16.2 The tables

TABLE A — chunking strategies (top-k=5, threshold 0):

| chunk config | #chunks | Cov@5 | Hit@5 | MRR | best span |
|---|---|---|---|---|---|
| **paragraph 512 (prod default)** | 9 | 0.986 | 0.970 | 0.745 | 5.21 |
| paragraph 256 | 15 | 0.912 | 0.818 | 0.620 | 3.00 |
| paragraph 128 | 27 | 0.905 | 0.818 | 0.642 | 1.67 |
| token 256 | 14 | 0.927 | 0.818 | 0.643 | 3.12 |
| token 128 | 26 | 0.898 | 0.818 | 0.647 | 2.06 |
| token 64 | 49 | 0.822 | 0.727 | 0.651 | 1.58 |
| overlap 256/128 | 21 | 0.902 | 0.697 | 0.502 | 3.27 |
| overlap 256/32 | 15 | 0.934 | 0.818 | 0.569 | 3.48 |
| overlap 128/32 | 31 | 0.932 | 0.818 | 0.665 | 2.12 |
| fixed 512 chars | 26 | 0.898 | 0.818 | 0.647 | 2.06 |
| fixed 1024 chars | 14 | 0.927 | 0.818 | 0.643 | 3.12 |

TABLE B — top-k effect (paragraph 512, threshold 0):

| top-k | Cov@k | Hit@k | MRR |
|---|---|---|---|
| 1 | 0.750 | 0.606 | 0.745 |
| 3 | 0.932 | 0.879 | 0.745 |
| **5** | **0.986** | **0.970** | **0.745** |
| 10 | 1.000 | 1.000 | 0.745 |

TABLE C — similarity threshold curve (paragraph 512, top-k=5):

| min_sim | Cov@5 | Hit@5 | MRR | kept |
|---|---|---|---|---|
| 0.0 | 0.986 | 0.970 | 0.745 | 8.4 |
| 0.1 | 0.892 | 0.879 | 0.670 | 6.0 |
| 0.2 | 0.676 | 0.636 | 0.517 | 3.5 |
| 0.3 | 0.375 | 0.364 | 0.304 | 1.8 |
| 0.4 | 0.214 | 0.212 | 0.153 | 0.9 |
| 0.5 | 0.043 | 0.030 | 0.015 | 0.2 |

TABLE D — metadata (section) filter, section-bearing queries only (n=30),
top-k=3, threshold 0. `reach` = fraction of queries whose target section
still exists in the pool *after* chunking (wide merges keep only the first
section label, so they erase the sections they cross):

| chunk config | reach | filter | Cov@3 | Hit@3 | MRR | kept | top1 sec-ok |
|---|---|---|---|---|---|---|---|
| paragraph 512 | 0.267 | none | 0.925 | 0.867 | 0.759 | 8.5 | – |
| paragraph 512 | 0.267 | section | 0.184 | 0.233 | 0.233 | 0.2 | 0.233 |
| paragraph 128 | 0.833 | none | 0.781 | 0.633 | 0.622 | 25.5 | – |
| paragraph 128 | 0.833 | section | 0.625 | 0.833 | 0.817 | 0.9 | 0.833 |
| **token 64** | **1.000** | none | 0.803 | 0.733 | 0.647 | 45.8 | – |
| **token 64** | **1.000** | section | 0.580 | 1.000 | 0.933 | 1.5 | 1.000 |

TABLE E — per-query misses at the production config: none (33/33 hit at
top-k=5, threshold 0); the only errors are MRR ordering, not recall.

### 16.3 Decisions the tables justify

- **Top-k stays 5.** k=1 leaves 25% of answers out of context; 5 covers 98.6%
  with 97% Hit@5, and 10 adds nothing but latency surface (an ML reranker
  later changes this trade-off — then the harness rerun arbitrates).
- **The similarity floor defaults to 0 and ships only as `minSimilarity` (a
  precision-after-recall opt-in).** Table C is the honest cost of "filter
  noise": 0.1 sheds ~10% coverage, 0.2 sheds half. Hash-model geometry puts
  genuine hits low; a real provider's distribution may differ — the knob is
  exposed, and this table is the documented baseline to compare a real model
  against (Day 24).
- **Paragraph 512 (the Day-10 production default) is right for entity-RAG
  coverage** (bulk answers bundled into one wide chunk), and paragraph 128
  is the citation-friendly alternative (−8% coverage for 1.7-page units).
- **Metadata filtering works exactly as well as section attribution.**
  Table D is the A/B: the same `metadata.section` filter *hurts* at
  paragraph-512 (MRR 0.76→0.23, reach 0.27) and *helps* once attribution is
  intact (token 64: MRR 0.65→0.93, reach 1.00). The knob ships anyway — it
  is correct for attribution-intact configurations and becomes fully
  effective when debt #24 (section-atomic chunking) lands. `sourceType`
  filters are provenance-safe at every config (no cross-page merges affect
  the document-level value).

### 16.4 What shipped

- `ai-service/evaluation/build_corpus.py` + `retrieval_quality.py` (run from
  `ai-service/`, prints Tables A-E; fixtures land in
  `ai-service/tests/fixtures/eval/`).
- `POST /api/v1/search` body gains optional `minSimilarity` (float 0-1),
  `sourceType` (`pdf|docx|md|html|txt`), and `metadata` (object; keys ≤64
  chars, ≤20 keys; values string|number|boolean). `additionalProperties:
  false` still rejects typos; validation is 400.
- Repository translates knobs into one parameterized statement: `$1` org,
  `$2` query-vector literal (6 decimals), `$3` model; then `source_type = $N`
  and/or `metadata @> $N::jsonb`; then `(embedding::vector(384) <=> $2)::float8
  <= $N` iff `minSimilarity > 0`; `LIMIT` last. The floor is mapped at the
  boundary exactly as §16.3 documents; no new index (HNSW still serves the
  sort, filters are applied on the joined rows).

## 17. Day-17 RAG generation (as built)

`POST /api/v1/rag/generate` answers a question from the tenant's own corpus,
end to end:

```
question ──► backend: RAG service (retrieve via /search, budget context)
                │  best-first top-K chunks by tokenCount, ≤ maxContextTokens
                ▼
         backend POST /v1/rag/generate  {question, context, minScore, ...}
                │  ai-service: provider engine + guardrails
                ▼
         answer + evidence + usage  (or the canonical refusal, still HTTP 200)
```

### 17.1 Who owns what

- **Backend** (`RagService` + `HttpRagGenerator` bridge +
  `POST /api/v1/rag/generate` route) owns **tenant-scoped retrieval and
  context assembly**. It reuses the Day-15/16 `SearchService` (same
  `POST /api/v1/search` knobs: `limit`, `sourceType`, `metadata`, and the
  Day-16 `minSimilarity` floor), then `buildContext()` greedily packs the
  best-first chunks by stored `tokenCount` under `maxContextTokens`. The
  **strongest chunk always survives** even if alone it exceeds the budget, so
  truncation can never leave an empty context. The backend's JSON-schema
  (`rag.schema.ts`) validates the request (question non-blank, `limit` 1-20,
  `maxContextTokens` 50-2000, `minScore` 0-1, `sourceType`/`metadata` same as
  search) and the response envelope.
- **AI service** (`/v1/rag/generate`) owns **generation and the "I don't
  know" guardrails**. It does *not* re-trust the backend blindly: it
  re-budgets the context (`_budget_context`), and **refuses when the best
  evidence similarity < `minScore` or the context is empty** — refusing is
  cheap (no LLM call) and correct (nothing profitable to say). A refusal is
  a 200 with `refused: true` and the canonical string.

### 17.2 The generation providers (a swap seam)

The service is provider-driven behind the `RagProvider` Protocol (and the
`ChatCompleter` Protocol for the LLM path — defined as a Protocol, not an
import of the concrete class, to avoid a service-import cycle):

| provider | behavior | when |
|---|---|---|
| `extract` (default, `knowflow-extract-1`) | deterministic & faithful-by-construction: quotes the verbatim sentence with the highest **stopword-filtered content-token overlap** (tokens ≥ 3 chars); refuses when no overlap | local/offline default — answers are provably grounded |
| `openai` | prompt-engineered chat completion over `prompting.py`'s `DEFAULT_SYSTEM_PROMPT` (never fabricate; refuse with the canonical string; cite context) behind the `ChatCompleter` seam | swapped in when a real model/key is configured (Days 19-21) |

`prompting.py` is the shared prompt/content toolkit (used by both paths):
`content_tokens`, `sentences`, `overlap_ratio`, `format_context` (bundles
context items into a cost-estimated block), `is_refusal` (recognizes the
canonical "I don't know." plus near-variants so a refusal is never presented
as an answer).

### 17.3 The wire contract

`POST /api/v1/rag/generate` with `{ question, limit?, maxContextTokens?,
minScore?, sourceType?, metadata? }` returns:

```jsonc
{
  "answer": "...",            // generated answer, or the canonical "I don't know."
  "refused": false,           // true = declined (weak/empty context), NOT an error
  "provider": "extract",
  "model": "knowflow-extract-1",
  "retrieval": { "query": "...", "model": "knowflow-hash-384", "retrieved": 2 },
  "evidence": [               // citation chain for the answer (Day-18 plumbing)
    { "index": 0, "chunkId": "...", "documentId": "...",
      "documentTitle": "...", "section": "...", "page": 1, "similarity": 0.8 }
  ],
  "usage": { "promptTokens": 10, "completionTokens": 3, "totalTokens": 13 }
}
```

### 17.4 Guardrails, in order

1. Empty context → refuse (no LLM call).
2. Best evidence similarity `< minScore` → refuse (no LLM call).
3. Otherwise generate; a refusal surfacing from the provider is *preserved*
   as an honest `refused: true` answer — never a crash, never a 4xx/5xx.
4. Only the generator failure (unreachable AI service) is a real error:
   `503 RAG_GENERATION_UNAVAILABLE` (mirroring Day 15's
   `EMBEDDING_UNAVAILABLE` — an explicit outage, never a silent empty answer).

### 17.5 Config

| setting | default | meaning |
|---|---|---|
| `AI_RAG_TIMEOUT_MS` (backend) | 15000 | bound on the backend→generator call (real LLM can be slow; still bounded) |
| `AI_RAG_MAX_CONTEXT_TOKENS` (backend) | 1200 | default context budget (request can override) |
| `rag_provider` (ai-service) | `extract` | active generation provider |
| `rag_model` (ai-service) | `knowflow-extract-1` | reported on the response |
| `rag_default_system_prompt` (ai-service) | unset | optional override of `DEFAULT_SYSTEM_PROMPT` |
| `rag_max_context_tokens` (ai-service) | 1200 | defensive re-budget on the provider path |

### 17.6 Verification

See the **[Day 17] verification record**: ai-service **143/143**, backend
**95/95**, and the live `e2e/run.sh` green with both a grounded answer and an
out-of-scope refusal. The `extract` provider makes "is the answer faithful"
trivially correct today (it *is* a quote); the faithful *free-form* answer
arrives with an enabled real LLM (debt #25), at which point the Day-20
evaluation harness measures it properly.

## 18. Day-18 citations (as built)

Day 17 delivered a RAG answer plus an `evidence` chain; Day 18 makes that
chain part of the **product** — the answer literally points at its sources, and
a Source API resolves each pointer to the document it came from. The promised
shape, end to end:

```
"annual leave is 20 working days. [1]"
Sources
  [1] Employee Handbook — Page 14
```

The pieces, and where each lives:

### 18.1 Source metadata + page references

Every retrieved context item already carried `document_title`, `section`,
`page` and `similarity` (Day 17). Day 18 re-ships those as per-citation fields
so the client can render the Sources block with **no second lookup**. The page
reference is the page the cited chunk *starts on* — the page a reader
physically turns to, matching the Day-9/10 citation chain
(`chunks.page_number`).

### 18.2 Citation IDs — the inline marker is the citation id

The context formatter (`format_context`, `prompting.py`) numbers the items it
passes to the provider `[1]..[n]`. A citation's `id` **is** that number, so the
`[id]` marker that appears inline in `answer` maps 1:1 to its source:

- `mark_citations(answer, ids)` appends the `[n]` markers to the claim (e.g.
  `"annual leave is 20 working days." + [1]`);
- `citation_ids(text)` parses the `[n]` markers a provider already emitted,
  distinct and in order of first appearance;
- `_CITATION_RE`/`citation_ids` ignore out-of-range numbers (`[0]`, a model's
  off-by-one) so a sloppy marker can never corrupt the citation list.

### 18.3 Citation mapping — marker → source chunk

`build_citations(items, ids)` maps each inline marker back to the context item
it numbers, producing a `Citation(id, title, section, page, chunk_id,
document_id, similarity)`. Each provider cites honestly:

| provider | how it cites |
|---|---|
| `extract` | cites the **one** chunk it quotes verbatim — `[n]` where `n` = its position in the context — deterministic and always grounded |
| `openai` | honors the `[n]` references the model already emits; falls back deterministically to the strongest (index 0) so a grounded answer is *never* left uncited |

Both carry `citations[]` on the `POST /v1/rag/generate` response (and the
backend mirrors it through `HttpRagGenerator` + the `rag.schema.ts` 200
envelope: `required: [..., "citations", ...]`).

### 18.4 Source API — citation → document

`GET /api/v1/sources/:documentId` resolves one citation's `documentId` back to
its source document:

```jsonc
{
  "document": {
    "id": "...", "title": "Employee Handbook",
    "filename": "handbook.pdf", "sourceType": "pdf",
    "size": 12345, "createdAt": "..." },
  "pages": 14
}
```

`SourcesService.get` reads `getDocumentByOrg` + `getDocumentStatus` — the page
count comes from the extracted `document_pages`, so "— Page 14" is grounded in
real extraction. **Tenant-scoped like every read**: `organization_id` comes from
the token, and an unknown *or foreign* id returns `404 NOT_FOUND`, never 403,
so cross-tenant existence never leaks (identical to Day 13's stance).

### 18.5 Verification

See the **[Day 18] verification record**: ai-service **147/147**, backend
**100/100**, `npx tsc --noEmit` clean.

## 19. Day-19 conversations (as built)

Day 18 made a single answer cite its sources; Day 19 makes the **session** the
unit of the Q&A experience. The chat layer is two tables and a REST surface
under `/api/v1/conversations`, and the message endpoint runs the Day-17 RAG
pipeline so every persisted answer arrives with its citations.

```
POST /conversations            start a chat (title optional)
GET  /conversations            paginated list, most-recently-active first
GET  /conversations/:id        full transcript, exact turn order
POST /conversations/:id/messages   ask: run RAG, store user + assistant
DELETE /conversations/:id      delete session + messages (cascade)
```

### 19.1 The data model

Migration `010` adds `conversations` and `messages`:

- `conversations(id, organization_id, title, created_by, created_at,
  updated_at)` — a tenant's chat session. The Day-7 `touch_updated_at()`
  trigger fires on the `updated_at` bump so the list sorts by real activity.
- `messages(id, organization_id, conversation_id, position, role, content,
  payload, created_at)` — the append-only turn log. `role` is
  `user | assistant | system`; `position` is a **per-conversation monotonic
  ordinal** so order is exact and stable even when a user+assistant pair
  shares one `created_at` instant. The assistant message's `payload` is
  free-form `jsonb` holding its citation provenance — `citations`, `refused`,
  `provider`, `model`, `usage` — so the Sources block (`[1] Employee Handbook
  — Page 14`) is renderable straight off the stored turn, no second lookup.

Both tables carry `organization_id` and every path scopes on it — the same
schema+index tenancy rule as every other table (RLS lands on the hardening
day), and the conversation/message ownership is enforced *and* existence is
hidden: a foreign id answers `404 NOT_FOUND`, never 403.

### 19.2 The message endpoint (the feature)

`ConversationsService.addMessage` runs the Day-17 `RagService.generate`
(forwarding the same `limit` / `maxContextTokens` / `minScore` / `sourceType` /
`metadata` knobs), then persists the **user question + assistant answer as one
atomic unit** on a dedicated connection:

```
BEGIN
  next = MAX(position)+1 for the conversation
  INSERT messages (user,  position=next)
  INSERT messages (assistant, position=next+1, payload=citations/refused/provider/model/usage)
  UPDATE conversations SET updated_at=now() WHERE id AND organization_id
COMMIT
```

Then the transcript is re-read from the **released** pool connection (never the
transaction client) so the returned messages carry their full `jsonb` payload
exactly as a later `GET` would. On any failure the transaction rolls back — a
turn is all-or-nothing.

### 19.3 Two bugs the wiring exposed (worth keeping)

- **pg `Pool.connect` must be called as a method.** Destructuring
  `const connect = this.db.connect; connect()` loses `this`, and pg's pool
  throws `Cannot read properties of undefined (reading 'ending')`. Call
  `this.db.connect()` so `this` is the pool instance.
- **`{ type: "object" }` collapses in fast-json-stringify.** A bare object
  schema (no `properties`) drops the payload's undeclared keys — the DB stored
  the full citation provenance but the response serialized every turn's
  `payload` as `{}`. The schema needs `additionalProperties: true` so the
  jsonb keys actually serialize.

### 19.4 Verification

See the **[Day 19] verification record**: backend **123/123** (`npx tsc
--noEmit` clean), covering create/list/get/message/delete plus tenant
isolation, atomicity, ordering, and validation.

## 20. Day-20 conversation memory (as built)

Day 19 made a chat session persist turns with citations; Day 20 makes that
session **shape the answer**. The job is the follow-up: after "What is the
refund policy?" → "The refund period is 30 days.", a user asking "What about
international customers?" means international customers *of the refund policy*
— and the system must ground the answer on the refund evidence, not refuse or
drift to shipping.

```
turn 1: user "What is the refund policy?"
        user "The refund period is 30 days."   <- newest prior user+assistant
turn 2: user "What about international customers?"   <- REFERENTIAL
        -> rewritten retrieval/generation query:
           "<refund policy> — <refund period is 30 days> — What about international customers?"
        -> grounds on the refund chunk, cites it
```

### 20.1 The window: context management on the backend

`ConversationsService.addMessage` loads the conversation's prior messages and
runs them through `buildHistoryWindow` before RAG:

- **Message-count cap** (`maxHistoryMessages`, default 20) — keep only the most
  recent turns, recency-first.
- **Token budget** (`maxHistoryTokens`, default 800) — if the window still
  exceeds it, drop the **oldest** turns until it fits. A follow-up is about the
  latest exchange, so recency wins.
- **Role filtering** — only `user`/`assistant` frames are carried; a `system`
  note never leaks into user-facing conversation memory.

The result (oldest-first) is the `history` shipped to the RAG pipeline.

### 20.2 The rewrite: retrieval must happen on a resolved query

Retrieval (pgvector) runs in the **backend**, before the AI service is ever
called — so referential disambiguation must happen there too.
`RagService.generate(org, question, { history })` computes
`expandQuestionForRetrieval(history, question)`:

- **Referential** turns (a pronoun — `it/that/these/those/them/they` — or an
  anaphoric connector like `what about`/`and the`/`is that`) get expanded with
  the **last user question** plus the **factual heart of the last assistant
  reply** (the trailing `[n]` citation marker stripped — it carries no
  retrieval meaning). The spliced form becomes the pgvector query.
- **Standalone** questions pass through unchanged.
- The **raw user question** is still what `addMessage` persists and what
  `RagResponse.question` reports; only retrieval/generation use the resolved
  query (observable as `retrieval.query`).

The resolved query also becomes the generation `question`, and the bounded
`history` is sent alongside it, so both providers understand the conversation.

### 20.3 Generation: both providers understand the thread

- **Extractive baseline** — resolves a referential follow-up itself via the
  same `expand_referential_question` helper (defense-in-depth even if a caller
  sends a raw question with history), then matches evidence by content-token
  overlap against the *resolved* query. This is what turns the Day-20 story
  into a green test: without history, "What about international customers?"
  shares zero content tokens with "The refund period is 30 days." and refuses;
  with history, it quotes the refund sentence and cites `[1]`.
- **OpenAI provider** — prepends `format_history(history)` (labeled
  `User:`/`Assistant:` lines) before the context/question block, so a real model
  gets the conversation as context on top of the rewritten question.

The "I don't know." guardrail is untouched: ambiguity with no thread still
refuses honestly rather than guessing.

### 20.4 Validation and defense

The AI service re-bounds the window defensively in its schema
(`RagGenerationRequest.history`): `MAX_HISTORY_MESSAGES` cap, per-message
length bound, roles restricted to `user`/`assistant`, and `extra="forbid"` so a
wrong-shape message is a 422, never silently corrupting the prompt.

### 20.5 Verification

See the **[Day 20] verification record**: ai-service **157/157**, backend
**133/133** (`npx tsc --noEmit` clean).

## 21. Day-22 organizations (as built)

Day 21 shipped the RAG evaluation harness (see `ai-service/evaluation/`); Day 22
turns the multi-tenant root into a **team** resource. The org, its members, and
its growth path live under `/api/v1/organizations`, grounded in migration `011`
(the `invitations` table + a widened four-role `users.role`):

```
OWNER > ADMIN > MEMBER > VIEWER        (middleware/roles.ts: ROLE_RANK + atLeast)
users.role  = the role of one membership   (one user row = one org membership)
invitations = how a team grows without a fresh per-person registration
```

### 21.1 The role hierarchy and the gate

`roles.ts` pins the four roles and `atLeast(role, minimum)`; `auth-guard.ts`
adds `requireAtLeast(minimum)` so a route says "admin or stronger" once. The
hierarchy lives here, not in SQL — the schema only constrains the *set* of valid
roles, the backend decides who may do what. Owners clear every level.

### 21.2 Routes and guardrails (OrganizationService)

```
GET    /organizations                             [any member]  profile + memberCount
GET    /organizations/members                     [any member]  the org's users as memberships
PATCH  /organizations/members/:id/role            [>= ADMIN]    role change
GET    /organizations/invitations                 [>= ADMIN]    pending/accepted/revoked
POST   /organizations/invitations                 [>= ADMIN]    mint {email, role} -> 201 {…, token}
DELETE /organizations/invitations/:id             [>= ADMIN]    revoke (pending only)
POST   /auth/invitations/accept                   [PUBLIC]      token+password -> membership
```

`updateMemberRole` enforces three guardrails regardless of how high the caller
is: the **owner can never be reassigned** (protects the org root), **you cannot
change your own role** (no self-promotion), and **you can only grant a role at
or below your own rank** (an admin cannot mint an owner). Reads stay open to any
member; invites and role changes need ADMIN.

### 21.3 Invitations: how a team grows

- **Mint** (`POST /invitations`): an admin/owner invites an `email` with a role.
  Storage holds the token **only as sha256** (`hashToken`, same convention as
  refresh tokens) with an `expires_at`; a duplicate **pending** invite for the
  same `(organization, email)` is 409 by schema (`idx_invitations_org_email_pending`).
- **Accept** (`POST /auth/invitations/accept`, public — the bearer knows the
  token): verify hash + pending + unexpired, then create the invitee's `users`
  row (with the invited role and a freshly hashed password) **and** spend the
  invitation in **one transaction** — a person is never half-created or
  double-accepted. Accepting into an email the org already owns is 409
  `EMAIL_TAKEN`, not a silent duplicate. Unknown token 404, spent 409, expired 410.
- **Revoke** (`DELETE`): only a pending invite can be revoked; revoking twice is
  409. `revokeInvitation` reads the affected id off a `RETURNING id` (the shared
  `DatabaseClient` interface exposes `rows`, not `rowCount`).

Tenant scoping is unchanged: every lookup filters `organization_id` taken from
the token, and cross-tenant/unknown ids answer 404/403 — existence never leaks.

### 21.4 Verification

Backend **175/175** (`npx tsc --noEmit` clean). Tests flushed out two as-built
defects: `acceptInvitation` returned the spent invitation with its stale
`pending` status (now `accepted`), and `revokeInvitation` never typechecked
against the un-exposed `rowCount`. The `integration.sources` teardown also
double-ended its shared pool ("Called end on pool more than once") and now runs
the app on its own pool.

### 21.5 Day 23 — per-user private documents (authorization)

Day 22 gave every user an *org* role, but within an org every member still read
every document: the tenant was the only privacy boundary. Day 23 adds the second
axis — **ownership**. Every document-facing query now filters on
`uploaded_by = caller` **and** `organization_id = tenant`, both taken from the
token (`request.user.sub` / `request.user.org`), never from the client.

```
POST   /api/v1/documents/{id}            -> uploadedBy = request.user.sub  (unchanged owner)
GET    /api/v1/documents                 WHERE organization_id AND uploaded_by = caller
GET    /api/v1/documents/:id             WHERE organization_id AND uploaded_by = caller
GET    /api/v1/documents/:id/status       "        "        "
DELETE /api/v1/documents/:id             WHERE organization_id AND uploaded_by = caller  (+ owner role)
POST   /api/v1/search                    JOIN documents d ON d.uploaded_by = caller
POST   /api/v1/rag/generate              retrieval scoped by caller (via search)
POST   /conversations/:id/messages       RAG grounded on the caller's documents
GET    /api/v1/sources/:documentId       WHERE organization_id AND uploaded_by = caller
```

`request.user.sub` is threaded route → service → repository for
`getDocumentByOrg`, `listDocumentsByOrg` (`DocumentListOptions.uploadedBy`),
`getDocumentStatus`, `deleteDocument`, and `searchChunksByOrg` (a join
`d.uploaded_by = $3`). A cross-user miss — like a cross-tenant miss — is a
**404 NOT_FOUND**, never 403, so neither tenant nor ownership existence leaks.
Deletion keeps the owner-role gate (members 403) as a second layer over the
ownership scoping.

The strongest proof is the dedicated integration suite
(`integration.authz-peruser.test.ts`): within one org, an owner uploads a
document and marks it retrievable with a *perfect* vector match; a colleague
(member) then gets **404** for detail/status/sources, an empty list
(`total: 0`), and **zero** search results for a query that lands exactly on the
chunk — while the owner still sees it all. Compatibility: the existing
documents/search/rag/conversations/sources tests were updated to the
two-arg `(organizationId, userId)` service signature.

**Verified:** `npx tsc --noEmit` clean; backend **177** tests (was 175), incl.
the +2 per-user privacy integration tests; DB back to seed.

## Day 27 — Streaming RAG + the Q&A UI

Streaming makes the "answer" a first-class event rather than one blocking
round-trip, and it is a three-layer contract: model → AI service → backend →
SPA. Each layer streams tokens and only the leaf buffers.

### SSE wire contract (the artifact everything else conforms to)

The AI service's `POST /v1/rag/generate/stream` emits, in order:

```
data: {"delta": "<text token stream>"}\n\n
...
data: {"done": {"question", "answer", "refused", "provider", "model",
                 "retrieval", "evidence", "citations", "usage"}}\n\n
data: [DONE]\n\n
```

`[DONE]` is the final frame. The backend **proxies this verbatim** through
`reply.raw` (`HttpRagGenerator.generateStream` → async generator →
`RagService.generateStream` → the `/api/v1/rag/generate/stream` route), so the
browser receives exactly the deltas the model produced. Refusal is *not* an
error: an empty-context / below-min-score question still returns HTTP 200 with
a `delta` stream, a terminal `refused: true` `"I don't know."`, and `[DONE]` —
the same honest guardrail as the non-streaming Day 17 path.

### The provider seam, extended

`complete_stream()` on `OpenAILLMProvider` (httpx `stream` / chat-completions
SSE) and `MockLLMProvider` behind `SimplifiedLLMProvider`; `RagProvider.`
`generate_stream()` default + `OpenAiRagProvider.generate_stream()` override;
`RAGGenerator.generate_stream(body)` applies the identical token-budget /
empty-context / min-score guardrails as the sync path, so streamed and
unstreamed answers can never disagree on *whether* to answer.

### The SPA

`frontend/` rebuilds `Chat.tsx`: a conversation sidebar (new / select /
delete), streaming answers, `react-markdown` rendering with inline `[n]`
citation badges, a clickable Sources list, and loading/error/empty states with
a Stop button. `ragApi.generateStream()` is a fetch read-loop over the SSE
response keyed by an `AbortController`, returning `{ read, abort }` — the Stop
button aborts the loop, not the whole page.

**Verified:** ai-service **166** tests green (incl. 34 rag), backend **96**
(93 unit + 3 new streaming integration tests in `tests/integration.rag.test.ts`),
frontend `npx tsc --noEmit` + `vite build` clean (442 KiB / 136 KiB gzipped).

## Day 28 — Docker / one-command deploy

`docker-compose.yml` at the repo root declaratively boots the entire system
with `docker compose up --build`:

| Service | Image | Role |
|---|---|---|
| `postgres` | `pgvector/pgvector:pg18` | relational + vector store; `db/schema.sql` into initdb; volume `pgdata:/var/lib/postgresql` (PG18 wants a single mount) |
| `redis` | `redis:7-alpine` | RQ broker / job registries |
| `minio` (+ `minio-init`) | `minio/minio` + `minio/mc` | object store; `minio-init` creates the `knowflow` bucket once |
| `ai-service` | `knowflow-ai-service` (from `ai-service/Dockerfile`) | FastAPI: `/health`, `/v1/*`, `/v1/rag/generate/stream`; offline-first (`LLM_PROVIDER=mock`, `RAG_PROVIDER=extract`) |
| `worker` | reuses the ai-service image, `python -m app.queue.worker` | RQ loop: parse → chunk → embed |
| `backend` | `knowflow-backend` (multi-stage node:22-slim) | client API on :3000; `expose` only (no host port) |
| `frontend` | `knowflow-frontend` (node → nginx:1.27-alpine) | SPA + `proxy` to backend for `/api`, `/auth`, and SSE with `proxy_buffering off` |

Healthchecks gate the graph: `pg_isready`, `redis-cli ping`, `mc ready`,
python `urllib` → `/health`, node `fetch` → `/health`; `backend`/`ai-service`/
`worker` start only after their stores are healthy, and `frontend` only after
`backend` is healthy. `minio-init` runs once (`service_completed_successfully`).

### Real issues fixed while wiring it (all four are worth keeping)

1. **The frontend production build had never run.** `npm run build` =
   `tsc -b && vite build`; `tsc -b` (project references) caught what
   `tsc --noEmit` + `vite build` separately always missed — `baseUrl`
   deprecation (TS5101), `erasableSyntaxOnly` blocking `ApiError`'s parameter
   properties, a non-`forwardRef` `Textarea`, an unexported `Citation` type,
   `ReactMarkdown`'s unsupported `className` prop, and a stream-closure `done`
   used-before-assigned. At least one of these is exactly the kind of error
   `tsc --noEmit` never sees under a solution-style `files: []` tsconfig.
2. **nginx `502` on `/api`.** Variable-based `proxy_pass` demands a `resolver`
   directive at request time ("no resolver defined to resolve backend"). Static
   `proxy_pass http://backend:3000` is used instead — safe because compose's
   `depends_on: backend: service_healthy` guarantees `backend` resolves when
   nginx starts.
3. **Postgres 18 crash on init.** PG18 images store data under a
   major-version subdirectory and refuse a bind/volume at `/var/lib/postgresql/data`;
   the volume now mounts at `/var/lib/postgresql`.
4. **Backend `unhealthy`.** The production config validator rejects a
   `JWT_SECRET` < 32 chars (`knowflow-dev-secret-change-me` was 27). Compose
   ships a real ≥32-char dev secret.

**Verified end to end against the running stack** (not just `compose config`):
register `smoke@example.com` → login → multipart upload of a generated PDF →
`queued → processing → ready` (worker extracted + embedded, `ingestion_jobs`
succeeded) → `POST /api/v1/rag/generate/stream` through nginx → backend →
ai-service answers with `delta`/`done`/`[DONE]` flushed (a `refused: true`
`"I don't know."` confirms the mock-LLM guardrail fires rather than a crash).
`docker compose config` is valid; all 8 services report healthy.

## Day 29 — Testing + Security

Folded the attack surface into the suite: rate limiting (429 envelope,
proxy-aware keys, tighter auth window), strict input/file validation, JWT
hardening (`alg`/`none`, expiry, secret ≥ 32 char in prod), allow-listed CORS,
SQL-injection-proof bound queries, prompt-injection-resistant RAG (the extract
provider is verbatim-quote-only; the OpenAI provider walls untrusted context
into the user message and refuses even an injected "answer anyway"). The
Day-21 golden corpus is wired *as tests* so a stale label breaks CI, not just
the eval script. New: `backend/tests/security.test.ts` (15), `ai-service/tests/
test_prompt_injection.py` (5), `test_eval_dataset.py` (2). backend **195**,
ai-service **173**.

## Day 30 — CI/CD + Monitoring + Deployment

`observability/` adds Prometheus (scrapes itself, backend, ai-service, and the
worker's own `:8001` endpoint) and Grafana (provisioned datasource + dashboard,
anonymous read) — 10 compose services. Backend emits `http_requests_total` /
`http_errors_total` / `http_request_duration_seconds` + `ai_request_duration_seconds`
/ `ai_tokens_total`; the AI service adds `ai_generation_duration_seconds`,
`ai_tokens_total{operation,provider,kind}`, and the worker's
`queue_jobs_total`/`queue_job_duration_seconds` per outcome — recorded through
RQ's **`SimpleWorker`** (in-process, no fork → counters are real, not COW).
`x-request-id` joins one request across all three layers in structured logs.
`.github/workflows/ci.yml`: lint → tests → build → docker (config/boot/smoke)
→ deploy (main-only, opt-in SSH + rsync + compose). Live proof: 10/10
healthy, 4/4 scrape targets up, and real register → search → RAG traffic plus
a genuinely enqueued ingestion produced HTTP/AI/token/queue series. backend
**199**, ai-service **180**.

## Day 31 — Architecture documentation

The system written down as focused, interview-ready files under `docs/`:
`architecture.md`, `api.md`, `database.md`, `rag.md`, `security.md`,
`decisions.md` (12 ADRs), `evaluation.md`. This file was renamed via `git mv`
from `docs/architecture.md` to `docs/build-log.md` so the clean docs own the
name. Every fact in them was verified against the source (routes, migration
numbers, HNSW SQL, chunker budgets, provider guardrails, metric arithmetic,
the 199/180 test counts).

## Day 32 — Final architecture diagram

One as-built diagram (React → Node/Fastify API → PostgreSQL+pgvector /
Redis / MinIO → RQ worker → Python AI service → embeddings + LLM, with
pgvector retrieval feeding the answer path) now heads the README and
`docs/architecture.md` §6. The week-1 "Planned Tech Stack" table is labeled
as the plan it was, with as-built deltas called out.

## Day 33 — Performance: measure, fix, re-measure

Measured every leg of the path against a **fixed 4-worker RQ fleet** (the
pre-Day-33 numbers were single-worker, a worker-name bug), rate limits lifted
for the bench. Host: 4 CPU / 7.7 GB, shared with the user's session and a
separate nexus stack; load averaged 1–15 across runs, so absolute latencies
swing with contention — record `loadavg` as a covariate.

**Baselines** (`benchmarks/benchmark.py`, `benchmarks/results/*.json`):
- `ingest_10` → 316 docs/min; `ingest_20` → 231; `ingest_100` → 224;
  `ingest_1000` → 124 (queue_wait p50 ~194 s — workers are CPU-bound). 'large'
  190-page PDF: process 62 ms/page (11.7 s), total 15.0 s to READY.
- `search` (8 concurrent, 1000-doc corpus): p50 ~52 ms. `users` (8×2×3):
  search p50 204 ms, RAG p50 248 ms, 0 failed. RAG generate mean 35.5 ms.

**Per-doc profiling** pinned the bottleneck: pypdf `extract_text` ~90 ms/page
~0.5 s/doc is the #1 CPU cost; 4 workers on 4 cores cap throughput at
~2.1 docs/s → queue_wait balloons at batch scale.

**Fixes shipped:**
1. `/metrics` — Fastify async-handler race: an async handler that calls
   `reply.send()` without *returning* the reply re-runs the async `onSend`
   chain (requestId / securityHeaders), double-writing headers on real
   sockets ("Reply was already sent" → `ERR_HTTP_HEADERS_SENT`, every scrape).
   Fix: `return reply.type(...).send(await metricsText())`. Verified over a
   real socket (repeated `/metrics` + `/health`, all 200, zero errors),
   Prometheus `http_errors_total{route="/metrics"}` now empty, `up=1`.
2. Large-PDF extraction parallelised (`ai-service/app/processor/pdf.py`):
   docs ≥32 pages (auto = 8× CPUs, min 8) extract pages across up to 4
   `ProcessPoolExecutor` workers, slicing indices into ≥2 slices. Output is
   byte-identical to the inline path (asserted in tests). Isolated: ~1.3×
   (container, shared load) to ~1.75× (quiet host) faster extraction; inline
   path untouched for small docs.

**Finding:** the box is CPU-bound. Parallel extraction helps a quiet host but
provides only parity under the real single-job-with-3-siblings workload; the
honest cork is cores/SLO, not pypdf. End-to-end 190-page large job under load:
14.0 s process / 73.8 ms per page / 17.2 s to READY (vs baseline 11.7 s /
62 ms / 15.0 s at a quieter host moment) — same order of magnitude, no
regression, modest win only when the host idles.

**Re-measurement (after optimization, DB + Redis wiped, `loadavg` covariate):**
- `ingest_100` → 131 docs/min (host load rose 3 → 17 during the run).
- `ingest_1000` → 118 docs/min (load 2.5 → 12) — flat vs baseline 124,
  confirming the ceiling is cores, not the extractor.
- `search` (8 concurrent, 100-doc corpus) → p50 100 ms / 8.1 qps at load 17.5
  (vs p50 52 ms / 14 qps at quiet load): latency scales with host saturation,
  no code regression.
- `large` 190-page → process 25.6 s / 134 ms per page when started at load 12+
  (saturated), vs 14.0 s / 73.8 ms at load 2 — pure contention spread.

Verdict: the parallel extractor ships as a quiet-host optimization with
byte-identical output and zero regression; on this permanently 2.5–4.7×-loaded
4-core box it cannot change bulk throughput, which is CPU/SLO-bound.

**Proof / state:** ai-service + backend images rebuilt and deployed; 4 workers
register uniquely, all dummy/cleanup docs removed; `ruff` clean;
backend `vitest run`: **199/199**; ai-service PDF/processor suite green
(**185/185** full suite).

## 35. Day-35 portfolio launch — repository restructure (as built)

Day 35 renamed the repo root to the platform and reorganised it so the top
level *is* the product (see the tree in `README.md`). Everything above still
shows the historical path names exactly as they were on each day; the current
canonical layout is:

```
ai-knowledge-platform/
├─ frontend/            SPA (Vite + React), served by nginx on :80 (proxies /api, /auth, /stream)
├─ backend/             Fastify API (:3000) — auth, tenancy, RAG orchestration, /metrics
├─ ai-service/          FastAPI (:8000) — extraction, chunking, hashing-embedding, RAG providers, /metrics
├─ worker/              first-class RQ-worker image (scales independently; Prometheus on :8001)
├─ infrastructure/
│  ├─ db/               schema.sql + seed.py (PostgreSQL 18 + pgvector)
│  └─ observability/    Prometheus config + provisioned Grafana dashboard
├─ evaluation/          golden corpus + retrieval/RAG metric harness (offline scripts)
├─ tests/e2e/           live-demo + offline eval + full-pipeline integration test
├─ benchmarks/          load harness + results/ (ingest/search throughput records)
├─ docs/                architecture, database, api, rag, security, evaluation, decisions, build-log
├─ docker-compose.yml   one-command stack
└─ .github/workflows/ci.yml, LICENSE, .dockerignore
```

Path moves and the new first-class `worker/` image are described in the
[Day-35 worker notes](../../worker/README.md) — every consumer
(docker-compose, CI lint/build/test/deploy, docs links) now targets this
layout. The Day-34 demo/eval work moved to `tests/e2e/` and `evaluation/`
unchanged; offline eval still **14/14 PASS**.

**Launch polish added after the restructure:** the README media slots are now
filled with real captures — `docs/screenshots/capture_playwright.py` drives a
fresh tenant → 10-doc corpus → READY → cited RAG → guardrail refusal and
writes the seven `*.png`s embedded in the README. To make the refusal
reachable from the UI (not only via the `minScore` API knob the demo uses),
the chat page now ships a **"Refuse weak matches"** toggle that sends the
documented 0.20 floor (`frontend/src/pages/Chat.tsx`); default remains
unchanged (recall-first). Re-verified at launch: backend **199/199**,
ai-service **185/185**, offline eval **14/14**, live demo **14/14**.

