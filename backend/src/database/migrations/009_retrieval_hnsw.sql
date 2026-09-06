-- Day 15: retrieval index — HNSW over the model-of-record's vectors.
--
-- Day 11 deferred the ANN index until the model-of-record dimension was
-- chosen at retrieval time; that model is the local hash vectorizer,
-- `knowflow-hash-384` (384-D, L2-normalized, cosine-comparable).
--
-- `embeddings.embedding` is dimension-flexible (`vector`) on purpose so
-- models coexist per chunk (UNIQUE(chunk_id, model)); HNSW needs a fixed
-- dimension, so the index is an EXPRESSION index over the cast to the chosen
-- dimension, PLUS a partial predicate on the model. Retrieval orders by the
-- same cast expression (`e.embedding::vector(384) <=> $q`), which is exactly
-- how pgvector matches the index. A future 1536-D real model gets its own
-- index on `::vector(1536)`, so the store never assumed a dimension.
--
-- m/ef_construction are HNSW build parameters; defaults are fine for dev
-- corpora, tune before production-scale loads.

CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw_cosine
    ON embeddings USING hnsw ((embedding::vector(384)) vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
    WHERE model = 'knowflow-hash-384';