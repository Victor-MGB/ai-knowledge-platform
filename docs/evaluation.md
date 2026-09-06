# KnowFlow — Evaluation

How we *know* retrieval and RAG work — a golden benchmark, measurable metrics,
and the suite that pins it so a regression breaks tests, not users.

---

## 1. The benchmark corpus

A deterministic, real, re-extractable corpus (built once by
`evaluation/build_corpus.py`, committed under `ai-service/tests/fixtures/eval/`):

- **5 documents / 31 pages** — four generated PDFs whose sections start on
  their own page with real outline bookmarks (so the extractor records the
  same `metadata.section` signal the product uses for citations), plus
  `chapter.pdf`, which intentionally has **no outline** — it measures
  retrieval that must stand without section metadata.
- **33 golden queries.** Each row is
  `(question, expected_answer, expected_document, expected_page)`:
  - `expected_answer` is the **verbatim reference sentence** from the corpus
    (authored that way, so a paraphrase can't fake a label);
  - `expected_page` is **resolved from the actually-extracted pages** at eval
    time — if the pipeline stops producing that page, the label stops
    resolving and the test fails;
  - a couple of rows are deliberate **cross-document distractors** (a question
    whose expected answer is elsewhere) to prove retrieval doesn't leak across
    tenants/documents.
- Ingestion through the real processor is part of the harness — goldens are
  verified against pages produced by the actual extractor, never against a
  hand-rolled fixture chain.

## 2. Metrics

Defined in `evaluation/rag_metrics.py`; every metric is
aggregated across the golden queries at *every* K:

| Metric | Meaning |
|---|---|
| `document_hit@k` | the expected **document** is in the top-k retrieved set |
| `page_hit@k` | the expected **page** is in the top-k |
| `recall@k` | relevant retrieved ÷ relevant in corpus (exact, cumulative) |
| `precision@k` | relevant in top-k ÷ k |
| `answer_correct` | the answer carries ≥ `threshold` (default 0.5) content-overlap with the expected evidence |
| `citation_correct` | the answer's citation points at the expected **document AND page** |
| `citation_document_correct` | citation points at the right document (page agnostic) |
| `hallucinated` | the answer's text is **not** grounded in the supplied context (token-level, after citation-stripping) |

`recall` is `None` where there are no relevant chunks (honest, not a fake
`0`); `hit`/citation checks use normalized content-token sets so whitespace
or punctuation drift cannot change a label.

## 3. What the numbers told us (and the knobs they set)

The Day-16 retrieval-quality sweep ran real chunkers + embedder over the
golden corpus:

- **Coverage and hit-rate saturate by `k=5`** → default `limit: 5`, hard cap 20.
- **Any positive similarity floor trades recall away fast** → `minSimilarity`
  defaults to `0`; the knob exists for precision-critical callers, not as a
  default.
- **Metadata/section filtering is only sound at the boundaries it was
  measured on** → wide paragraph merges keep the *first* section label, so
  section-pin filters document that assumption.
- **Chunk-size/overlap sweep** fed the Day-10 chunking choice (see
  [decisions.md](decisions.md) ADR 005): paragraph → the fewest coherent,
  citation-ready units.

## 4. RAG-layer honesty eval

The Day-20/21 eval extended retrieval metrics to the *answer*:

- `extract` provider must be faithful **by construction** → `answer_correct`
  and `hallucinated` at the boundary of verbatim quoting;
- refusal honesty: when no evidence clears the confidence floor, the answer
  **must** be the canonical `"I don't know."` and `refused: true` (not a
  bluff, not a partial quote) — verified on real engine output plus the
  prompt-injection suite (`test_prompt_injection.py`, 5 tests) covering the
  extract/OpenAI/empty-context boundary.

## 5. Wired as a suite, not a script

Evaluation is **part of CI** (`ai-service/tests/`):

- `test_eval_dataset.py` — re-extracts the real fixture PDFs and pins that
  every golden resolves to a verbatim sentence and a real page. A stale
  golden (or a prose tweak that moves a sentence) breaks tests.
- `test_eval_metrics.py` — pins the metric math (recall/precision/hit/citation/
  hallucination edge cases, including "no relevant chunks" → `None`).
- `test_rag_api.py`, `test_rag_helpers.py`, `test_prompt_injection.py` — pin
  the generation contract end to end.
- The standalone `evaluation/` scripts remain runnable for ad-hoc sweeps
  (`retrieval_quality.py`, `rag_evaluation.py`).

## 6. Overall test posture

| Suite | Count | What it proves |
|---|---|---|
| backend unit + integration | 199 | routes, auth, tenancy, upload validation, queue, rag orchestration, metrics, security |
| ai-service | 180 | processing, chunkers, embedding, queue/RQ, RAG providers, guardrails, eval correctness, metrics |
| e2e (`tests/e2e/`) | — | fresh-boot public-API proof: upload → READY → RAG, no manual steps |
| CI (`.github/workflows/ci.yml`) | — | lint → tests → build → docker → deploy runs it on every push |

Green is the baseline; the eval harness is the tripwire for retrieval and
answer quality specifically.