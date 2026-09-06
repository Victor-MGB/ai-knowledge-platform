# KnowFlow — API Reference

The external surface lives on the backend (Fastify :3000), served through
nginx at `:80`. The AI service's endpoints are internal to the compose
network (see §8).

---

## 1. Conventions

- **Base paths.** Feature routes: `/api/v1/*`. Auth: `/auth/*`. Liveness:
  `/health`. Metrics: `/metrics`. nginx proxies `/api/`, `/auth/`, `/metrics`,
  and the SSE stream to the backend.
- **Auth.** `Authorization: Bearer <accessToken>` — JWT, HS256, ~15 min
  lifetime. Tokens carry `sub` (user), `org` (tenant), `role`, `typ: "access"`.
- **Tenant scoping.** Always derived from the token, never from the client.
  A foreign org's row answers `404 NOT_FOUND`, indistinguishable from absent.
- **Error envelope.** `{ "error": { "code": "…", "message": "…" } }`.
  Codes: `VALIDATION_ERROR` 400 · `UNAUTHORIZED` 401 · `FORBIDDEN` 403 ·
  `NOT_FOUND` 404 · `RATE_LIMITED` 429 · `EMBEDDING_UNAVAILABLE` 503 ·
  `RAG_GENERATION_UNAVAILABLE` 503.
- **Validation.** Every schema sets `additionalProperties: false` (enforced,
  `removeAdditional: false`) → a typoed key is a `400`, never silently ignored.
- **Request IDs.** Send/receive `x-request-id` to join logs across services.
- **Rate limits.** Global + a tighter auth window (`POST /auth/*`). Keys are
  `x-forwarded-for`-aware. Excess → `429 RATE_LIMITED`.
- **Streaming.** SSE frames of JSON, terminated by `data: [DONE]` (see §5).

---

## 2. Auth  — `/auth` (rate-limited)

| Method + path | Body | Response |
|---|---|---|
| `POST /auth/register` | `{ email, password (≥8), organization, name? }` | `201 { user, organization, tokens }` |
| `POST /auth/login` | `{ email, password }` | `200 { user, tokens }` |
| `POST /auth/refresh` | `{ refreshToken }` | `200 { user, tokens }` (rotates; old token revoked) |
| `POST /auth/logout` | `{ refreshToken }` | `204` (revokes token) |
| `POST /auth/invitations/accept` | `{ invitationToken, … }` | `201 { user, organization, tokens }` |

`tokens = { accessToken, refreshToken }`. Refresh tokens are opaque, stored
only as SHA-256 hashes, single-use (rotation) and revocable; expired ones are
swept.

---

## 3. Identity & orgs — `/api/v1`

| Method + path | Auth | Response |
|---|---|---|
| `GET /me` | required | current user profile (`id`, `email`, `role`, `organizationId`…) |
| `GET /organizations` | required | org profile (`id`, `name`, `slug`) |
| `PATCH /organizations/members/:id/role` | admin+ | change a member's role |
| `GET /organizations/invitations` | admin+ | list pending invitations |
| `POST /organizations/invitations` | admin+ | `{ email, role? }` → creates invite token |
| `DELETE /organizations/invitations/:id` | admin+ | revoke an invitation |

Role hierarchy: `owner > admin > member > viewer` (`requireAtLeast`). Any
member can read the org; growing the team is admin/owner-only.

---

## 4. Documents  — `/api/v1/documents`

| Method + path | Auth | Notes |
|---|---|---|
| `POST /documents` | required | multipart upload → `201`; enqueues processing |
| `GET /documents` | required | list (own documents) `?limit&offset` + filters |
| `GET /documents/:id` | required | detail (own only) |
| `GET /documents/:id/status` | required | derived pipeline state (see below) |
| `DELETE /documents/:id` | required | owner-only delete (object + rows) |

**Upload rules** (server-enforced, see [security.md](security.md)):
allowed extensions `pdf docx md html txt`; extension allow-list + magic-byte
sniff (PDF/FILE ZIP), NUL-scan for text; filename path-traversal stripped;
PDF page limit (default 1000) and size limits. Client content-type is never
trusted.

**Document status** values: `queued → processing → ready` and `failed`,
tracked with an `error` column. The status endpoint layers on the job
ledger and reports the *derived* state (see `/v1/queue/documents/:id` in §8).

---

## 5. Retrieval & RAG  — `/api/v1`

### `POST /search`
```json
{ "query": "…"                     // ≥1 non-space char, ≤500
, "limit": 5,                       // 1..20, default 5
  "minSimilarity": 0,               // 0..1; precision-critical callers only
  "sourceType": "pdf",              // optional document-level filter
  "metadata": { "section": "…" } } // optional exact JSONB containment on chunk metadata
```
Embeds the question (ai-service), walks the tenant's HNSW cosine index,
returns top-K chunks with document, page, section, similarity.

### `POST /rag/generate`
```json
{ "question": "…", "limit": 5, "maxContextTokens": 1200, "minScore": 0,
  "sourceType": "…", "metadata": { … } }
```
Retrieves tenant chunks → assembles a token-budgeted context (strongest first)
→ calls the AI service's generator. Response:

```json
{ "answer": "The octopus…[1]…", "refused": false,
  "provider": "extract", "model": "knowflow-extract-1",
  "citations": [ { "id": 1, "documentId": "…", "title": "…",
                   "page": 3, "section": "Social behaviour" } ],
  "evidence": [ { "index": 0, "chunkId": "…", "similarity": 0.81, … } ] }
```
`refused: true` fires the "I don't know" guardrail when no evidence clears the
confidence floor.

### `POST /rag/generate/stream` (SSE)
Same body. Frames:

```
data: {"delta":"The "}
data: {"delta":"octopus "}
…
data: {"done":{"refused":false,"provider":"extract","model":"…",
              "usage":{"prompt_tokens":…,"completion_tokens":…,"total_tokens":…}}}
data: [DONE]
```
The backend streams `reply.raw` verbatim (no buffering); nginx flushes.

### `GET /sources/:documentId`
Resolves a cited document to clickable source metadata (`title`, `pages`,
`updatedAt`, object key) — feeds the chat UI's Sources panel.

---

## 6. Conversations  — `/api/v1/conversations`

| Method + path | Auth | Notes |
|---|---|---|
| `POST /conversations` | required | `{ title? }` → `201` conversation |
| `GET /conversations` | required | list own conversations `?limit&offset` |
| `GET /conversations/:id` | required | detail |
| `POST /conversations/:id/messages` | required | `{ content, rag? }` → streams a RAG answer and appends user + assistant turns. Assistant turn carries `payload` with citation provenance |
| `DELETE /conversations/:id` | required | `204` |

---

## 7. Misc

| Method + path | Notes |
|---|---|
| `GET /health` | `{ status, uptimeSeconds, db }` — used by compose healthchecks |
| `GET /` | service banner |
| `POST /api/v1/echo` | schema-validation probe |
| `GET /metrics` | Prometheus text format (rate-limit exempt) |

---

## 8. AI-service internal surface (compose network only)

Unproxied; the backend calls these directly.

| Method + path | Notes |
|---|---|
| `GET /health`, `GET /` | health banner |
| `GET /metrics` | Prometheus scrape (own + worker-ish metrics) |
| `POST /v1/embeddings` | `{ text, sourceType? }` → vector(s); `hash` \| `openai` provider |
| `POST /v1/chat/completions` | OpenAI-compatible; `mock` \| `openai` (mock counts words as tokens) |
| `POST /v1/rag/generate` | generation + guardrails; `extract` \| `openai` |
| `POST /v1/rag/generate/stream` | SSE `delta`/`done`/`[DONE]` |
| `POST /v1/queue/documents/{id}/process` | best-effort enqueue; idempotent (`UNIQUE(document_id, kind)`) |
| `POST /v1/queue/documents/{id}/embed` | chained after a successful process run |
| `GET /v1/queue/documents/{id}` | derived state: `queued`/`processing`/`ready`/`failed`/`duplicate` |
| `POST /v1/process/documents/{id}` | run the process phase inline (claim → extract → chunk → store) |
| `POST /v1/embed/documents/{id}` | run the embed phase inline (batch → retry → validate → store) |

The phase runners enqueue-or-run under a claim, so calling them directly is
safe (idempotent, claim-protected).