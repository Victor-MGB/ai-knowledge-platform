# KnowFlow — Retrieval-Augmented Generation (RAG)

The money flow: a user asks a question; the platform retrieves **their own
tenant's** chunks most similar to that question, feeds a token-budgeted
context to a generation provider, and returns an answer whose every factual
claim maps back to an inline `[n]` citation resolvable to a source document.

Pipeline shape: `chunk → embed → retrieve → assemble context → generate →
refuse-or-answer`. This document covers chunking, retrieval, generation, and
the honesty guardrails. Why each piece was chosen: [decisions.md](decisions.md).

---

## 1. Chunking (the retrieval unit is the chunk)

Extraction turns a PDF into ordered pages (`document_pages`); chunking turns
pages into `chunks` that preserve the citation chain: `page_number` (its
start page), `chunk_index`, and `metadata` carrying `strategy`, `section`,
`source` (s3:// URL), and `page_range`.

Token math is deterministic (`TOKENS_PER_CHAR = 0.25`, same estimate as
extraction), so a budget is roughly a real token budget for any model. Four
swap-in strategies:

| Strategy | Unit | Default budget | Character |
|---|---|---|---|
| `fixed` | characters | 2048 | hard budget; splits at word boundaries |
| `token` | estimated tokens | 512 | greedy token budget (word-aligned) |
| `overlap` | tokens | 512, **+128 overlap** | sliding tail kept between consecutive chunks |
| `paragraph` | blank-line paragraphs | 512 | semantic units; merges to budget, hard-splits only an overlong single paragraph |

**Production default: `paragraph`.** Measured on the same 6-page fixture
(Day 10): `fixed` → 19 chunks (+~5 redundant), `token` → 5, `overlap` → 6
(**+160 redundant tokens**), `paragraph` → **5 chunks / ~590 tokens, ~6
redundant**. Paragraph wins because it produces as few chunks as token with
near-zero redundancy *and never splits a sentence or section* — which is
exactly what citation-bearing answers need later. (One measured caveat: wide
paragraph merges keep only the **first** section label, so `metadata.section`
pins are sound only when the section matches the paragraph start.)

All strategies: hard bounds (`MIN_CHUNK_SIZE = 16`, `MAX_CHUNK_TOKENS =
4096`), per-request override (`chunk_strategy`, `chunk_size`,
`overlap_tokens`), and invalid combinations → `422` before any work, never a
worker crash.

---

## 2. Embedding (the query side is symmetric)

Chunks are vectorized per `(chunk, model)`; retrieval embeds the **question
with the same provider pipeline** used for chunks, so both sides live in the
same space. Model-of-record today: `knowflow-hash-384` (384-D, L2-normalized,
deterministic, offline). `RAG_PROVIDER`/`EMBEDDING_PROVIDER=openai` swaps in
real models behind an unchanged seam. Batching (64), retries (3, exponential),
dimension/finiteness validation, and same-transaction flips are described in
[database.md](database.md) §2 and [decisions.md](decisions.md) §Embedding.

---

## 3. Retrieval

`POST /search` and the RAG endpoint both pull top-K over the **tenant's**
retrievable chunks (`status='ready' AND embedding_status='ready'` after the
Day-11 two-phase model) ordered by cosine similarity:

```sql
ORDER BY e.embedding::vector(384) <=> $q  LIMIT $k
```

Optional pre-filters applied *before* ranking: `sourceType` (document-level)
and `metadata` (exact JSONB containment on chunk metadata), plus a
`minSimilarity` cosine floor for precision-critical callers. The Day-16
evaluation tuned defaults: coverage/hit-rate saturates by `k=5`; any positive
similarity floor trades recall away fast, so the floor is off by default.
See [evaluation.md](evaluation.md).

Retrieval answers three shapes of "is this useful": document-level hit,
page-level hit, and ranked recall/precision at every K — the eval harness
measures all of them.

---

## 4. Context assembly

The backend `RagService` collects retrieved evidence (strongest first:
`evidence[0]`), strips to a token budget (`maxContextTokens`, bounded 50–2000;
default 1200), attaches source labels (document title, section, page), and
sends the AI service a package of `{ question, context[ ], history? }`. The
generation layer owns prompt assembly and guardrails — the backend never
prompts.

---

## 5. Generation

Two providers behind one contract (`answer`, `refused`, `citations`,
`evidence`, `provider`, `model`) — the extract-output shape is identical, so
a real model can power the same endpoint and the same eval.

### `extract` (default) — faithful by construction
- No prompt to inject against. It **quotes a verbatim sentence** from the
  strongest evidence that actually addresses the question.
- "Addresses the question" = a content-token overlap between the (optionally
  referentially expanded) question and the evidence sentence, using a small
  stopword list so common words can't fake a match.
- No overlap → canonical refusal: **`refused: true`, answer `"I don't know."`**
  (`DONT_KNOW`). This is the "no confident evidence" guardrail, live and
  test-covered.

### `openai` — packaged honesty
- `RAG_PROVIDER=openai` prompt-packages the same context into an OpenAI
  chat completion. The honesty contract lives in the **system**
  message ("use ONLY the context … cite inline [n] … reply exactly
  'I don't know.'"); untrusted context is **confined to the user message**,
  so a hostile document is data, never a command.
- Responses are **post-checked**: if the model did not comply (no answer,
  refusal missing where required, or a reply that ignores the guardrail), the
  provider forces the canonical "I don't know." This double-guarding — prompt
  isolation *and* deterministic post-check — is the prompt-injection defense.
- Mentions `[1]..[n]` are normalized into real `citations` (id, title, page,
  section) by the shared `mark_citations`/`build_citations` helpers.

### Chat history
`history` turns are formatted and passed for the second generation, so
follow-ups can be referential ("expand on that") — resolved via
`expand_referential_question` before matching so the extract provider can
still judge overlap.

---

## 6. Streaming

`POST /api/v1/rag/generate/stream` (SSE):

```
data: {"delta":"The "}               ── token/grapheme chunks as generated
data: {"done":{"provider":"extract","model":"…","refused":false,"usage":{…}}}
data: [DONE]
```

The ai-service streams `delta` events, terminates with `done` (carrying
provider/model/usage), then `[DONE]`. The **backend proxies verbatim via
`reply.raw`** — no buffering, no re-parse — and nginx flushes. Client-side the
SPA reads it with an abortable fetch ("Stop" button). Mock provider streams
word-by-word; OpenAI streams the upstream SSE tokens.

---

## 7. Citations & the answer contract

- The answer text embeds inline `[n]` markers; a numbered `citations` list
  resolves each to `documentId`, `title`, `page`, `section`.
- `evidence` still carries the retrieval-side numbers (`index`, `chunkId`,
  `similarity`) so callers can see *why* the answer was grounded.
- `GET /api/v1/sources/:documentId` turns a citation into a clickable source
  panel in the SPA.
- **Eval contract** (see [evaluation.md](evaluation.md)): an answer is "correct"
  if it carries ≥ threshold overlap with the expected evidence; a citation is
  "correct" only if it points at the expected **document and page**; an answer
  is "hallucinated" if its text is not grounded in the supplied context. The
  extract provider must score 1.0 on faithfulness by construction; the eval
  suite pins this so a regression (or a provider swap) breaks tests, not the
  user.