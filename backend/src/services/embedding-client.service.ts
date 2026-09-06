/** Day 15: the backend's bridge to the AI service's embedding endpoint.

 * Search needs a query vector in the SAME model that indexed the chunks, and
 * the embedding provider lives in the AI service (it also owns ingestion).
 * This client is the retrieval counterpart of HttpEnqueueService (Day 12) —
 * transport only, no policy: the SearchService decides what a failure means.
 */

export interface QueryEmbedding {
  vector: number[];
  model: string;
}

export interface QueryEmbedder {
  embed(text: string): Promise<QueryEmbedding>;
}

export interface HttpQueryEmbedderOptions {
  baseUrl: string;
  timeoutMs?: number;
}

export class HttpQueryEmbedder implements QueryEmbedder {
  private readonly timeoutMs: number;

  constructor(private readonly options: HttpQueryEmbedderOptions) {
    this.timeoutMs = options.timeoutMs ?? 5000;
  }

  async embed(text: string): Promise<QueryEmbedding> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    let response: Response;
    try {
      response = await fetch(
        `${this.options.baseUrl}/v1/embeddings`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ text }),
          signal: controller.signal,
        }
      );
    } catch (error) {
      throw new Error(
        `query embedding request failed: ${error instanceof Error ? error.message : String(error)}`
      );
    } finally {
      clearTimeout(timer);
    }
    if (!response.ok) {
      throw new Error(`query embedding failed: HTTP ${response.status}`);
    }
    const body = (await response.json()) as {
      data?: Array<{ embedding?: unknown }>;
      model?: unknown;
    };
    const vector = body.data?.[0]?.embedding;
    if (!Array.isArray(vector) || typeof body.model !== "string") {
      throw new Error("query embedding returned an unusable response shape");
    }
    return { vector: vector as number[], model: body.model };
  }
}