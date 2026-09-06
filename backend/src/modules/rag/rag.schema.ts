/** RAG generation body — same strictness as search: no unknown keys, a
 * question that must contain real characters, and knobs bounded so a typo'd
 * value is a 400, never a silent oddity.
 *
 *   question        the question to answer (same rules as /search query).
 *   limit           top-K retrieval; default 5.
 *   maxContextTokens  token budget for the answer's context (default config;
 *                   min 50, max 2000, matching the AI service). The context
 *                   that survives it is evidence[0] strongest-first.
 *   minScore        similarity floor on the strongest evidence; below it the
 *                   generation service answers "I don't know." (refused=true).
 *   sourceType/metadata  Day-16 retrieval filters, passed straight through.
 */
const ragBody = {
  type: "object",
  additionalProperties: false,
  required: ["question"],
  properties: {
    question: { type: "string", minLength: 1, maxLength: 500, pattern: "\\S" },
    limit: { type: "integer", minimum: 1, maximum: 20 },
    maxContextTokens: { type: "integer", minimum: 50, maximum: 2000 },
    minScore: { type: "number", minimum: 0, maximum: 1 },
    sourceType: {
      type: "string",
      enum: ["pdf", "docx", "md", "html", "txt"],
    },
    metadata: {
      type: "object",
      maxProperties: 20,
      propertyNames: { maxLength: 64 },
      additionalProperties: {
        type: ["string", "number", "boolean"],
      },
    },
  },
} as const;

const evidenceObject = {
  type: "object",
  required: ["index"],
  properties: {
    index: { type: "integer" },
    chunkId: { type: ["string", "null"] },
    documentId: { type: ["string", "null"] },
    documentTitle: { type: ["string", "null"] },
    section: { type: ["string", "null"] },
    page: { type: ["integer", "null"] },
    similarity: { type: ["number", "null"] },
  },
} as const;

/** Day 18 — a citation: the inline [id] marker in `answer`, with the source
 * metadata (title/page/section) to render the Sources block and the
 * documentId the Source API resolves to the full source document. */
const citationObject = {
  type: "object",
  required: ["id"],
  properties: {
    id: { type: "integer" },
    title: { type: ["string", "null"] },
    section: { type: ["string", "null"] },
    page: { type: ["integer", "null"] },
    chunkId: { type: ["string", "null"] },
    documentId: { type: ["string", "null"] },
    similarity: { type: ["number", "null"] },
  },
} as const;

export const ragSchema = {
  body: ragBody,
  response: {
    200: {
      type: "object",
      required: [
        "question",
        "answer",
        "refused",
        "provider",
        "model",
        "retrieval",
        "evidence",
        "citations",
        "usage",
      ],
      properties: {
        question: { type: "string" },
        answer: { type: "string" },
        refused: { type: "boolean" },
        provider: { type: "string" },
        model: { type: "string" },
        retrieval: {
          type: "object",
          required: ["query", "model", "retrieved"],
          properties: {
            query: { type: "string" },
            model: { type: "string" },
            retrieved: { type: "integer" },
          },
        },
        evidence: { type: "array", items: evidenceObject },
        citations: { type: "array", items: citationObject },
        usage: {
          type: "object",
          required: ["promptTokens", "completionTokens", "totalTokens"],
          properties: {
            promptTokens: { type: "integer" },
            completionTokens: { type: "integer" },
            totalTokens: { type: "integer" },
          },
        },
      },
    },
  },
} as const;

/** Day 27 streaming — identical request body; the response is a raw SSE
 * stream, so there is no JSON response schema to validate. */
export const ragStreamSchema = {
  body: ragBody,
} as const;

export interface RagBodyInput {
  question: string;
  limit?: number;
  maxContextTokens?: number;
  minScore?: number;
  sourceType?: "pdf" | "docx" | "md" | "html" | "txt";
  metadata?: Record<string, string | number | boolean>;
}