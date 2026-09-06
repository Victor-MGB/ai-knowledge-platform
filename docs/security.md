# KnowFlow — Security

Threat-model-driven controls, each one **verified by an outside-in test**
(real HTTP, real routes, real Postgres) — not assumed. Test counts and names
in [evaluation.md](evaluation.md); the durable decisions in
[decisions.md](decisions.md).

---

## 1. Threat model (in scope)

- Attacker is an anonymous internet caller and/or a hostile tenant member.
- Targets: other tenants' documents (`/documents`, `/search`, `/rag`,
  `/sources`, `/conversations`), privilege routes (org admin/member/role),
  credential stores, and the LLM/RAG layer (prompt injection, data leak).
- Out of scope by design: physical/logical access, RLS hardening (see ADR),
  supply-chain of third-party packages.

---

## 2. Authentication

- **Passwords**: bcrypt-hashed at rest; never logged or returned.
- **Access tokens**: JWT HS256 with `iss`/`aud`, `jti`, `exp`, and
  `typ: "access"`. Short-lived (~15 min). Fastify's JWT verified on every
  protected route (`request.jwtVerify()`).
- **Refresh tokens**: opaque 256-bit random; stored **only as SHA-256
  hashes** (a DB leak leaks no usable token); single-use with **rotation** on
  every `POST /auth/refresh`; revoked on `logout`; expired rows swept.
- **Token-shape defense**: routes reject `alg: none`, expired, and
  subject-less tokens, and **refresh tokens presented as access tokens** (the
  `typ` claim is enforced — a 256-bit secret you can't submit to `/api` is a
  boundary, not a bug).
- `requireAuth` (any signed-in user), `requireRole` (exact), `requireAtLeast`
  (hierarchy `owner > admin > member > viewer`).

## 3. Authorization (tenancy + privacy)

- **Tenant scope comes from the token, never the request body/query.** The
  client cannot name an org.
- Every tenant table filters on `organization_id`; every document read/list/
  search/source also filters `uploaded_by = caller` — **User A cannot read,
  list, search, or source User B's documents** even inside the same tenant
  (Day-23 cross-user integration suite proves exactly this).
- Org-management routes are gated by `requireAtLeast("admin")`.
- Foreign resources answer `404 NOT_FOUND`, not `403` — non-existence is not
  broadcast (avoiding classic enumeration).

## 4. Rate limiting

- Global window (`RATE_LIMIT_*`) + a tighter **auth window**
  (`AUTH_RATE_LIMIT_*`, default 10/60s) on `/auth/*` and
  `/auth/invitations/accept`.
- Keys honor `x-forwarded-for` so callers behind nginx stay **distinct** —
  verified by exhausting one proxy IP while a neighbour's budget still passes.
- Enforcement is per-route-config; `/metrics` is exempt.
- The error handler honors the limiter's `statusCode`, so the envelope is a
  structured `429 RATE_LIMITED` (a naive handler collapsed 429→500 — caught
  by test).

## 5. Input & file validation

- **Every schema**: `additionalProperties: false` is actually enforced
  (`removeAdditional: false`), so `role` escalation smuggled into
  `/auth/register` or oversized logins → `400 VALIDATION_ERROR`.
- **Uploads**:
  - extension allow-list (`pdf docx md html txt`);
  - **magic-byte sniffing** (PDF header, ZIP central-directory marker for
    docx/xlsx) — the client's declared content-type is never trusted;
  - NUL-byte scan for text files;
  - filename path-traversal stripped;
  - PDF page limit (default 1000) and size bounds — hostile-upload guards.
- Rejected uploads fail before any handler/Pipeline work (no partial S3
  objects).

## 6. CORS

- Origin **allow-list** (never `*`), pinned methods/headers,
  `credentials: false`.
- Same-origin SPA (dev vite / docker nginx) means CORS is **off by default**
  (`CORS_ORIGINS=""`); turning it on exposes only the configured origins.
- Tests: allowed preflight, blocked evil origin, and the disabled default.

## 7. SQL injection

- All repositories use parameterized queries (`$1…`), including JSONB
  containment and the pgvector literal (number-derived, never user text).
- A test probes `/api/v1/search` with `x'); DROP TABLE users; --` and a login
  email of `OR '1'='1` — both treated as data, structured responses, `users`
  table intact.

## 8. Secrets & configuration

- Everything via environment / `.env` (`zod`-validated in `config.ts`).
- **Production refuses to boot** on the dev default or a `JWT_SECRET`
  shorter than 32 chars — a weak secret is a config error, not a runtime
  surprise. Compose ships a ≥32-char secret.
- MinIO/S3, Postgres, and AI-provider keys all environment-injected; test
  configs use throwaway values.

## 9. Prompt injection & the AI boundary

Two-layer defense (detail: [rag.md](rag.md) §5, tests in `test_prompt_injection.py`):

1. **`extract` provider (default)** has *no prompt to attack* — it quotes a
   verbatim sentence when content-token overlap says the evidence addresses
   the question, else refuses `"I don't know."` Untrusted text is data, never
   a command.
2. **`openai` provider** confines untrusted context to the **user message**
   with the honesty contract isolated in the **system message**, and then
   **deterministically post-checks** the response — a reply that ignores
   guardrails (or dodges an empty-context refusal) is forced to the canonical
   "I don't know." The empty-context rule refuses even an injected
   "answer anyway" demand.

## 10. Observability of attacks

- Structured, request-scoped logs: every request carries an `x-request-id`
  joined across backend ↔ ai-service ↔ worker; access lines include
  `org`/`user`/`status`/`durationMs`.
- Rate-limit hits, validation failures, and 4xx/5xx all count toward
  Prometheus error-rate metrics, so abuse is visible on the Grafana error-rate
  panel before it is a problem.

## 11. Test coverage summary

| Area | Where |
|---|---|
| JWT/refresh hardening (alg:none, expired, foreign secret, sub-less, typ) | `backend/tests/security.test.ts` |
| Rate limiting + 429 envelope + proxy-aware keys | `security.test.ts` |
| CORS allow-list / disabled default | `security.test.ts` |
| SQL injection probes | `security.test.ts` |
| Cross-user privacy, org roles | `integration.authz-peruser.test.ts` |
| Prompt injection (extract + openai + empty-context) | `ai-service/tests/test_prompt_injection.py` |

backend **199** tests, ai-service **180** tests — all green.