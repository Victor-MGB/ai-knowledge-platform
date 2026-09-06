/** Day 12: hand an uploaded document to the AI service's ingestion queue.

 * The backend's only queue duty is "tell the worker pool there is work": the
 * AI service owns the durable ledger, the retries and the states. Enqueueing
 * is strictly best-effort — a dead queue or a slow service must never fail an
 * upload that already succeeded — so the facade is tiny, injected into
 * DocumentsService, and errors are swallowed at the call site.
 */

export interface EnqueueService {
  /** Ask the AI service to process an uploaded document (idempotent). */
  enqueue(documentId: string): Promise<void>;
}

export interface HttpEnqueueOptions {
  baseUrl: string;
  timeoutMs?: number;
}

export class HttpEnqueueService implements EnqueueService {
  private readonly timeoutMs: number;

  constructor(private readonly options: HttpEnqueueOptions) {
    this.timeoutMs = options.timeoutMs ?? 5000;
  }

  async enqueue(documentId: string): Promise<void> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    let response: Response;
    try {
      response = await fetch(
        `${this.options.baseUrl}/v1/queue/documents/${documentId}/process`,
        { method: "POST", signal: controller.signal }
      );
    } catch (error) {
      throw new Error(
        `enqueue failed for ${documentId}: ${error instanceof Error ? error.message : String(error)}`
      );
    } finally {
      clearTimeout(timer);
    }
    if (!response.ok) {
      throw new Error(`enqueue failed for ${documentId}: HTTP ${response.status}`);
    }
  }
}