import { AppError } from "../utils/errors.js";
import { observeAiCall } from "../middleware/metrics.js";
import type {
  RagCitation,
  RagContextItem,
  RagGeneration,
  RagGenerator,
  RagStreamEvent,
} from "./rag-generator-client.service.js";
import { SearchService, type SearchResultItem } from "./search.service.js";

export interface RagChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface RagOptions {
  /** Top-K retrieval; default 5 (Day-16 tuning). */
  limit?: number;
  /** Context token budget for the answer; default config (1200). */
  maxContextTokens?: number;
  /** Similarity floor on the strongest evidence; the generator refuses below it. */
  minScore?: number;
  /** Day-16 search filters, passed through to retrieval. */
  sourceType?: string;
  metadata?: Record<string, string | number | boolean>;
  /** Day-20 conversation memory: the prior turns of this chat, oldest first.
   *  Used to rewrite a referential follow-up into a self-contained retrieval
   *  query and shipped to the generator so it can resolve pronouns too. */
  history?: RagChatMessage[];
}

export interface RagResponse {
  question: string;
  answer: string;
  /** true when the answer is the canonical "I don't know." — an honest
   * refusal, never an error status. */
  refused: boolean;
  provider: string;
  model: string;
  retrieval: {
    query: string;
    model: string;
    /** how many chunks survived the context budget (within top-K). */
    retrieved: number;
  };
  evidence: RagGeneration["evidence"];
  citations: RagCitation[];
  usage: RagGeneration["usage"];
}

/** Day 17 RAG generation: question → retrieve (Day 15/16) → budget the
 * context → ask the AI service to generate. The RAG-shaped pipeline on top
 * of semantic retrieval:
 *
 *   question ──► QueryEmbedder ──► searchChunksByOrg (tenant/model/ready gates)
 *           ──► greedy token budget (drop the weakest of the top-K) ──►
 *           POST {AI}/v1/rag/generate (context, budget, min_score) ──► answer
 *
 * Two kinds of failure are surfaced explicitly because they mean "don't trust
 * an empty reply": a dead embedder is EMBEDDING_UNAVAILABLE (like search), a
 * dead generator is RAG_GENERATION_UNAVAILABLE. "The corpus cannot answer
 * this" is NOT a failure — it comes back 200 with `refused=true` and the
 * canonical answer, decided by the generation service's guardrails.
 */
export class RagService {
  constructor(
    private readonly search: SearchService,
    private readonly generator: RagGenerator,
    private readonly defaultMaxContextTokens = 1200
  ) {}

  async generate(
    organizationId: string,
    userId: string,
    question: string,
    options: RagOptions = {}
  ): Promise<RagResponse> {
    // Day 20 — resolve a referential follow-up against the thread *before*
    // retrieval, so pgvector actually recalls the right chunk ("international
    // customers" -> the refund-policy chunk). The raw question is preserved for
    // reporting/persistence; the resolved query drives retrieval + generation.
    const retrievalQuery = expandQuestionForRetrieval(options.history ?? [], question);

    // Day 23 — retrieval runs as the caller, so only their own documents (and
    // their chunks) are ever candidates for an answer.
    const retrieval = await this.search.search(organizationId, userId, retrievalQuery, {
      limit: options.limit,
      sourceType: options.sourceType,
      metadata: options.metadata,
    });

    const budget = options.maxContextTokens ?? this.defaultMaxContextTokens;
    const context = buildContext(retrieval.results, budget);

    let generation: RagGeneration;
    const startedAt = performance.now();
    try {
      generation = await this.generator.generate({
        question: retrievalQuery,
        history: options.history ?? [],
        context,
        maxContextTokens: budget,
        minScore: options.minScore,
      });
      observeAiCall(
        "rag.generate",
        (performance.now() - startedAt) / 1000,
        {
          prompt: generation.usage.promptTokens,
          completion: generation.usage.completionTokens,
          total: generation.usage.totalTokens,
        }
      );
    } catch (error) {
      observeAiCall("rag.generate", (performance.now() - startedAt) / 1000);
      throw new AppError(
        "RAG_GENERATION_UNAVAILABLE",
        `answer generation failed: ${error instanceof Error ? error.message : String(error)}`,
        503,
        { detail: error instanceof Error ? error.message : undefined }
      );
    }

    return {
      question,
      answer: generation.answer,
      refused: generation.refused,
      provider: generation.provider,
      model: generation.model,
      retrieval: {
        query: retrieval.query,
        model: retrieval.model,
        retrieved: context.length,
      },
      evidence: generation.evidence,
      citations: generation.citations,
      usage: generation.usage,
    };
  }

  /** Day 27 streaming: same retrieval/budget pipeline as `generate`, but the
   * answer tokens come back one at a time from the AI service, ending with a
   * final event carrying the complete generation (citations/evidence/refused).
   * The caller streams the tokens to the client as they arrive. */
  async *generateStream(
    organizationId: string,
    userId: string,
    question: string,
    options: RagOptions = {}
  ): AsyncIterable<RagStreamEvent> {
    const retrievalQuery = expandQuestionForRetrieval(options.history ?? [], question);

    const retrieval = await this.search.search(organizationId, userId, retrievalQuery, {
      limit: options.limit,
      sourceType: options.sourceType,
      metadata: options.metadata,
    });

    const budget = options.maxContextTokens ?? this.defaultMaxContextTokens;
    const context = buildContext(retrieval.results, budget);

    let generation: RagGeneration | null = null;
    const startedAt = performance.now();
    try {
      const stream = this.generator.generateStream({
        question: retrievalQuery,
        history: options.history ?? [],
        context,
        maxContextTokens: budget,
        minScore: options.minScore,
      });
      for await (const event of stream) {
        if (event.type === "delta") {
          yield { type: "delta", text: event.text };
        } else {
          generation = event.generation;
          yield {
            type: "done",
            generation: event.generation,
          };
        }
      }
    } catch (error) {
      observeAiCall("rag.generateStream", (performance.now() - startedAt) / 1000);
      throw new AppError(
        "RAG_GENERATION_UNAVAILABLE",
        `answer generation failed: ${error instanceof Error ? error.message : String(error)}`,
        503,
        { detail: error instanceof Error ? error.message : undefined }
      );
    }
    observeAiCall(
      "rag.generateStream",
      (performance.now() - startedAt) / 1000,
      generation
        ? {
            prompt: generation.usage.promptTokens,
            completion: generation.usage.completionTokens,
            total: generation.usage.totalTokens,
          }
        : undefined
    );
    if (!generation) {
      yield {
        type: "done",
        generation: {
          answer: "",
          refused: true,
          provider: "unknown",
          model: "unknown",
          evidence: [],
          citations: [],
          usage: { promptTokens: 0, completionTokens: 0, totalTokens: 0 },
        },
      };
    }
  }
}

type SearchResult = SearchResultItem;

/** Best-first context that fits the token budget (soft: the strongest chunk
 * always survives). Keeps the same ordering the generator expects, so
 * evidence[0] is the strongest evidence. */
export function buildContext(
  results: SearchResult[],
  budget: number
): RagContextItem[] {
  const sorted = [...results].sort((a, b) => b.similarity - a.similarity);
  const context: RagContextItem[] = [];
  let total = 0;
  for (let index = 0; index < sorted.length; index++) {
    const result = sorted[index]!;
    const tokens = result.chunk.tokenCount;
    if (index > 0 && total + tokens > budget) continue;
    context.push({
      text: result.chunk.content,
      similarity: result.similarity,
      section:
        typeof result.metadata.section === "string"
          ? result.metadata.section
          : undefined,
      page: result.page,
      chunkId: result.chunk.id,
      documentId: result.document.id,
      documentTitle: result.document.title,
    });
    total += tokens;
  }
  return context;
}

/** Day 20 — resolve a referential question ("what about international
 *  customers?") into a self-contained retrieval query by splicing in the
 *  thread anchor (last user question) and the factual heart of the last
 *  assistant reply. Non-referential questions pass through unchanged. Mirrors
 *  the AI service's `expand_referential_question` so backend retrieval and
 *  generator-side matching stay consistent. */
export function expandQuestionForRetrieval(
  history: RagChatMessage[],
  question: string
): string {
  if (!referential(question) || history.length === 0) return question;

  let lastUser: string | null = null;
  let lastAssistant: string | null = null;
  for (const message of history) {
    if (message.role === "user") lastUser = message.content;
    else lastAssistant = message.content;
  }

  const thread: string[] = [];
  if (lastUser) thread.push(lastUser);
  const clipped = clipSentenceTail(lastAssistant);
  if (clipped) thread.push(clipped);

  const prefix = thread.join(" — ");
  if (!prefix) return question;
  return `${prefix} — ${question}`;
}

/** Words/pronouns that mark a turn as referential (points back at the thread). */
const REFERENTIAL_RE =
  /\b(what about|how about|whats with|what's with|and then|and the|and why|what is that|is that|are those|these|those|them|they|it this|it that|same thing|same rule|same policy|and|about that|on that)\b/i;
const PRONOUN_RE = /\bit\b|\bthis\b|\bthat\b|\bthose\b|\bthem\b|\bthey\b|\bthese\b/i;

function referential(question: string): boolean {
  return REFERENTIAL_RE.test(question) || PRONOUN_RE.test(question);
}

function clipSentenceTail(text: string | null, maxChars = 400): string | null {
  if (!text) return null;
  const cleaned = text.trim().replace(/\s*\[\d+\]\s*$/, "");
  if (cleaned.length <= maxChars) return cleaned;
  return cleaned.slice(0, maxChars).replace(/[ ,;]+$/, "");
}