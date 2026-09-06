import type { DatabaseClient } from "../postgres.js";

export interface SearchChunkRow {
  chunk_id: string;
  chunk_index: number;
  page_number: number;
  token_count: number;
  content: string;
  metadata: Record<string, unknown>;
  document_id: string;
  title: string;
  filename: string;
  mime_type: string;
  source_type: string;
  cosine_distance: number;
}

/** p:gvector/* (pgvector) literal for the query vector — rounded to 6 decimals
 * to match the hash provider's stored precision, so an exact re-embed lands at
 * distance 0. */
function vectorLiteral(vector: number[]): string {
  return "[" + vector.map((value) => value.toFixed(6)).join(",") + "]";
}

export interface SearchOptions {
  limit?: number;
  /** Floor on cosine similarity in [0,1]; results closer than (1 - floor) to
   * the query are dropped. Default 0 (no floor). */
  minSimilarity?: number;
  /** Exact document-level source type gate, applied before ranking. */
  sourceType?: string;
  /** Exact JSONB containment filter on chunk metadata (e.g. { section: "…" });
    * combined with `@>` so several keys must ALL match. */
  metadata?: Record<string, string | number | boolean>;
}

/** Top-K semantic search over one org's *retrievable* chunks: only documents
 * that finished ingestion AND vectorization (`status=ready` + `embedding_status=ready`)
 * can be found, vectors must come from the model that produced the query
 * embedding, and the tenant filter is server-side. Ordered by cosine distance
 * ascending (closest first). The order-by repeats the model-of-record cast
 * expression (`e.embedding::vector(384)`), so pgvector's partial HNSW cosine
 * index from migration 009 serves the top-K scan for that model. Filter
 * conditions (source type, metadata containment, similarity floor) are built
 * in the same clause list and applied before ORDER/LIMIT. */
export async function searchChunksByOrg(
  db: DatabaseClient,
  organizationId: string,
  userId: string,
  model: string,
  vector: number[],
  options: SearchOptions = {}
): Promise<SearchChunkRow[]> {
  const params: unknown[] = [organizationId, vectorLiteral(vector), userId, model];
  const conditions = [
    "d.organization_id = $1",
    // Day 23: retrieval is scoped to the caller's OWN documents — a user can
    // never retrieve (and so never RAG on) a colleague's document, even inside
    // the same organization.
    "d.uploaded_by = $3",
    "e.model = $4",
    "d.status = 'ready'",
    "d.embedding_status = 'ready'",
  ];
  if (options.sourceType !== undefined) {
    params.push(options.sourceType);
    conditions.push(`d.source_type = $${params.length}`);
  }
  if (options.metadata && Object.keys(options.metadata).length > 0) {
    params.push(JSON.stringify(options.metadata));
    conditions.push(`c.metadata @> $${params.length}::jsonb`);
  }
  if (options.minSimilarity !== undefined && options.minSimilarity > 0) {
    params.push(1 - options.minSimilarity);
    conditions.push(
      `(e.embedding::vector(384) <=> $2::vector)::float8 <= $${params.length}`
    );
  }
  params.push(options.limit ?? 5);
  const sql = `SELECT c.id AS chunk_id,
            c.chunk_index,
            c.page_number,
            c.token_count,
            c.content,
            c.metadata,
            d.id AS document_id,
            d.title,
            d.filename,
            d.mime_type,
            d.source_type,
            (e.embedding::vector(384) <=> $2::vector)::float8 AS cosine_distance
     FROM embeddings e
     JOIN chunks c  ON c.id = e.chunk_id
     JOIN documents d ON d.id = c.document_id
     WHERE ${conditions.join("\n       AND ")}
     ORDER BY e.embedding::vector(384) <=> $2::vector ASC
     LIMIT $${params.length}`;
  const { rows } = await db.query(sql, params);
  return rows as SearchChunkRow[];
}