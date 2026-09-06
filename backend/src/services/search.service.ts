import type { DatabaseClient } from "../database/postgres.js";
import {
  searchChunksByOrg,
  type SearchChunkRow,
  type SearchOptions as RepositorySearchOptions,
} from "../database/repositories/search.repository.js";
import type { QueryEmbedder, QueryEmbedding } from "./embedding-client.service.js";
import { AppError } from "../utils/errors.js";
import { observeAiCall } from "../middleware/metrics.js";

export interface SearchOptions extends RepositorySearchOptions {
  /** top-K; default 5 (Day-16 evaluation: coverage/hit saturate by k=5). */
  limit?: number;
}

export interface SearchChunkView {
  id: string;
  chunkIndex: number;
  pageNumber: number;
  tokenCount: number;
  content: string;
}

export interface SearchResultItem {
  /** The atomic retrieval unit: the chunk that matched, with its citation
   * chain (page) so a later RAG layer can cite where an answer came from. */
  chunk: SearchChunkView;
  /** Cosine similarity in [0, 1] — 1 - cosine distance against the query. */
  similarity: number;
  /** The owning document, so results can link back to the library. */
  document: {
    id: string;
    title: string;
    filename: string;
    mimeType: string;
    sourceType: string;
  };
  /** The page the chunk starts on (also inside chunk; surfaced so the wire
   * contract reads: chunk, similarity, document, page, metadata). */
  page: number;
  /** The chunk's stored metadata (strategy, section, source, page_range). */
  metadata: Record<string, unknown>;
}

export interface SearchResponse {
  query: string;
  model: string;
  results: SearchResultItem[];
}

const round = (value: number): number =>
  Math.round(value * 1_000_000) / 1_000_000;

function toItem(row: SearchChunkRow): SearchResultItem {
  return {
    chunk: {
      id: row.chunk_id,
      chunkIndex: row.chunk_index,
      pageNumber: row.page_number,
      tokenCount: row.token_count,
      content: row.content,
    },
    similarity: round(1 - row.cosine_distance),
    document: {
      id: row.document_id,
      title: row.title,
      filename: row.filename,
      mimeType: row.mime_type,
      sourceType: row.source_type,
    },
    page: row.page_number,
    metadata: row.metadata ?? {},
  };
}

/** Day 15 semantic retrieval: embed the question, then top-K over the caller's
 * retrievable chunks. The query vector is always embedded in the model that
 * indexed the corpus (returned by the embedder), and the repository re-scopes
 * by organization AND ownership (`uploaded_by = caller`) — cross-tenant and
 * cross-user rows are unreachable because both filters are server-side, as
 * with every other read in the backend. */
export class SearchService {
  constructor(
    private readonly db: DatabaseClient,
    private readonly embedder?: QueryEmbedder,
    private readonly defaultLimit = 5
  ) {}

  async search(
    organizationId: string,
    userId: string,
    query: string,
    options: SearchOptions = {}
  ): Promise<SearchResponse> {
    const { vector, model } = await this.embed(query);
    const rows = await searchChunksByOrg(this.db, organizationId, userId, model, vector, {
      limit: options.limit ?? this.defaultLimit,
      minSimilarity: options.minSimilarity,
      sourceType: options.sourceType,
      metadata: options.metadata,
    });
    return {
      query,
      model,
      results: rows.map(toItem),
    };
  }

  private async embed(query: string): Promise<QueryEmbedding> {
    if (!this.embedder) {
      throw new AppError(
        "EMBEDDING_UNAVAILABLE",
        "query embedding is not configured",
        503
      );
    }
    const startedAt = performance.now();
    try {
      return await this.embedder.embed(query);
    } catch (error) {
      observeAiCall("embedding", (performance.now() - startedAt) / 1000);
      // Search is only as good as the query embedder; a dead AI service must
      // surface as an explicit failure, not an empty result set.
      throw new AppError(
        "EMBEDDING_UNAVAILABLE",
        `could not embed the query: ${error instanceof Error ? error.message : String(error)}`,
        503,
        { detail: error instanceof Error ? error.message : undefined }
      );
    } finally {
      observeAiCall("embedding", (performance.now() - startedAt) / 1000);
    }
  }
}