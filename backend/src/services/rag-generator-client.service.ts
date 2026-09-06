/** Day 17: the backend's bridge to the AI service's RAG generation endpoint.

 * RAG = question + retrieved context, and the generation provider (the LLM)
 * lives in the AI service — same split as Day-15's `HttpQueryEmbedder`: this
 * client is transport only, no policy; `RagService` decides what a failure
 * means and `-unavailable` 503s it. The AI service owns the prompt assembly,
 * the context formatting, the answer generation and the "I don't know"
 * guardrails.
 */

export interface RagContextItem {
  text: string;
  similarity?: number;
  section?: string;
  page?: number;
  chunkId: string;
  documentId: string;
  documentTitle: string;
}

export interface RagEvidence {
  index: number;
  chunkId: string | null;
  documentId: string | null;
  documentTitle: string | null;
  section: string | null;
  page: number | null;
  similarity: number | null;
}

/** Day 18 — one citation the answer's inline `[id]` marker points at,
 * carrying the source metadata (title/page/section) to render the Sources
 * block, plus the documentId the Source API resolves for the full document. */
export interface RagCitation {
  id: number;
  title: string | null;
  section: string | null;
  page: number | null;
  chunkId: string | null;
  documentId: string | null;
  similarity: number | null;
}

export interface RagUsage {
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
}

export interface RagGeneration {
  answer: string;
  refused: boolean;
  provider: string;
  model: string;
  evidence: RagEvidence[];
  citations: RagCitation[];
  usage: RagUsage;
}

export interface RagChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface RagGenerateInput {
  question: string;
  context: RagContextItem[];
  /** Day-20 conversation memory: prior turns (oldest first), oldest->newest. */
  history?: RagChatMessage[];
  /** Resolved context budget in tokens; the generator re-budgets defensively. */
  maxContextTokens: number;
  /** Similarity floor on the strongest evidence; the generator refuses below it. */
  minScore?: number;
}

export type RagStreamEvent =
  | { type: "delta"; text: string }
  | { type: "done"; generation: RagGeneration };

export interface RagGenerator {
  generate(input: RagGenerateInput): Promise<RagGeneration>;
  generateStream(input: RagGenerateInput): AsyncIterable<RagStreamEvent>;
}

export interface HttpRagGeneratorOptions {
  baseUrl: string;
  timeoutMs?: number;
}

export class HttpRagGenerator implements RagGenerator {
  private readonly timeoutMs: number;

  constructor(private readonly options: HttpRagGeneratorOptions) {
    this.timeoutMs = options.timeoutMs ?? 15000;
  }

  async generate(input: RagGenerateInput): Promise<RagGeneration> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    let response: Response;
    try {
      response = await fetch(`${this.options.baseUrl}/v1/rag/generate`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(buildRagRequestBody(input)),
        signal: controller.signal,
      });
    } catch (error) {
      throw new Error(
        `answer generation request failed: ${error instanceof Error ? error.message : String(error)}`
      );
    } finally {
      clearTimeout(timer);
    }
    if (!response.ok) {
      throw new Error(`answer generation failed: HTTP ${response.status}`);
    }
    const body = (await response.json()) as {
      answer?: unknown;
      refused?: unknown;
      provider?: unknown;
      model?: unknown;
      evidence?: Array<{
        index?: unknown;
        chunk_id?: unknown;
        document_id?: unknown;
        document_title?: unknown;
        section?: unknown;
        page?: unknown;
        similarity?: unknown;
      }>;
      usage?: {
        prompt_tokens?: unknown;
        completion_tokens?: unknown;
        total_tokens?: unknown;
      };
      citations?: Array<{
        id?: unknown;
        title?: unknown;
        section?: unknown;
        page?: unknown;
        chunk_id?: unknown;
        document_id?: unknown;
        similarity?: unknown;
      }>;
    };
    if (
      typeof body.answer !== "string" ||
      typeof body.refused !== "boolean" ||
      typeof body.provider !== "string" ||
      typeof body.model !== "string" ||
      !Array.isArray(body.evidence) ||
      !body.usage
    ) {
      throw new Error("answer generation returned an unusable response shape");
    }
    const citations = Array.isArray(body.citations)
      ? body.citations.map((item) => ({
          id: typeof item.id === "number" ? item.id : 0,
          title: item.title == null ? null : String(item.title),
          section: item.section == null ? null : String(item.section),
          page: typeof item.page === "number" ? item.page : null,
          chunkId: item.chunk_id == null ? null : String(item.chunk_id),
          documentId: item.document_id == null ? null : String(item.document_id),
          similarity:
            typeof item.similarity === "number" ? item.similarity : null,
        }))
      : [];
    return {
      answer: body.answer,
      refused: body.refused,
      provider: body.provider,
      model: body.model,
      evidence: body.evidence.map((item) => ({
        index: typeof item.index === "number" ? item.index : 0,
        chunkId: item.chunk_id == null ? null : String(item.chunk_id),
        documentId: item.document_id == null ? null : String(item.document_id),
        documentTitle:
          item.document_title == null ? null : String(item.document_title),
        section: item.section == null ? null : String(item.section),
        page: typeof item.page === "number" ? item.page : null,
        similarity:
          typeof item.similarity === "number" ? item.similarity : null,
      })),
      citations,
      usage: {
        promptTokens: Number(body.usage.prompt_tokens ?? 0),
        completionTokens: Number(body.usage.completion_tokens ?? 0),
        totalTokens: Number(body.usage.total_tokens ?? 0),
      },
    };
  }

  /** Day 27 streaming: proxy the AI service's SSE stream straight through. */
  async *generateStream(input: RagGenerateInput): AsyncIterable<RagStreamEvent> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    let response: Response;
    try {
      response = await fetch(`${this.options.baseUrl}/v1/rag/generate/stream`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(buildRagRequestBody(input)),
        signal: controller.signal,
      });
    } catch (error) {
      throw new Error(
        `answer generation stream request failed: ${error instanceof Error ? error.message : String(error)}`
      );
    } finally {
      clearTimeout(timer);
    }
    if (!response.ok) {
      throw new Error(`answer generation stream failed: HTTP ${response.status}`);
    }
    if (!response.body) {
      throw new Error("answer generation stream returned no body");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let newlineIndex: number;
        while ((newlineIndex = buffer.indexOf("\n\n")) !== -1) {
          const chunk = buffer.slice(0, newlineIndex);
          buffer = buffer.slice(newlineIndex + 2);
          const parsed = parseSseChunk(chunk);
          if (!parsed) continue;
          if (parsed.delta !== undefined) {
            yield { type: "delta", text: parsed.delta };
          } else if (parsed.done) {
            yield { type: "done", generation: parseRagGeneration(parsed.done) };
          }
        }
      }
    } finally {
      reader.releaseLock();
    }
  }
}

function buildRagRequestBody(input: RagGenerateInput): Record<string, unknown> {
  return {
    question: input.question,
    context: input.context.map((item) => ({
      text: item.text,
      similarity: item.similarity,
      section: item.section,
      page: item.page,
      chunk_id: item.chunkId,
      document_id: item.documentId,
      document_title: item.documentTitle,
    })),
    max_context_tokens: input.maxContextTokens,
    min_score: input.minScore,
    history: (input.history ?? []).map((message) => ({
      role: message.role,
      content: message.content,
    })),
  };
}

function parseSseChunk(chunk: string): {
  delta?: string;
  done?: Record<string, unknown>;
} | null {
  const line = chunk.trim();
  if (line.startsWith("data:")) {
    const payload = line.slice("data:".length).trim();
    if (payload === "[DONE]") return {};
    try {
      const parsed = JSON.parse(payload);
      if (typeof parsed === "object" && parsed !== null) {
        if (typeof parsed.delta === "string") return { delta: parsed.delta };
        if (parsed.done && typeof parsed.done === "object") {
          return { done: parsed.done };
        }
      }
    } catch {
      return null;
    }
  }
  return null;
}

function parseRagGeneration(body: Record<string, unknown>): RagGeneration {
  const evidence = Array.isArray(body.evidence)
    ? body.evidence.map((item) => {
        const e = item as Record<string, unknown>;
        return {
          index: typeof e.index === "number" ? e.index : 0,
          chunkId: e.chunk_id == null ? null : String(e.chunk_id),
          documentId: e.document_id == null ? null : String(e.document_id),
          documentTitle:
            e.document_title == null ? null : String(e.document_title),
          section: e.section == null ? null : String(e.section),
          page: typeof e.page === "number" ? e.page : null,
          similarity: typeof e.similarity === "number" ? e.similarity : null,
        };
      })
    : [];
  const citations = Array.isArray(body.citations)
    ? body.citations.map((item) => {
        const c = item as Record<string, unknown>;
        return {
          id: typeof c.id === "number" ? c.id : 0,
          title: c.title == null ? null : String(c.title),
          section: c.section == null ? null : String(c.section),
          page: typeof c.page === "number" ? c.page : null,
          chunkId: c.chunk_id == null ? null : String(c.chunk_id),
          documentId: c.document_id == null ? null : String(c.document_id),
          similarity: typeof c.similarity === "number" ? c.similarity : null,
        };
      })
    : [];
  const usage = (body.usage ?? {}) as Record<string, unknown>;
  return {
    answer: typeof body.answer === "string" ? body.answer : "",
    refused: body.refused === true,
    provider: typeof body.provider === "string" ? body.provider : "unknown",
    model: typeof body.model === "string" ? body.model : "unknown",
    evidence,
    citations,
    usage: {
      promptTokens: Number(usage.prompt_tokens ?? 0),
      completionTokens: Number(usage.completion_tokens ?? 0),
      totalTokens: Number(usage.total_tokens ?? 0),
    },
  };
}