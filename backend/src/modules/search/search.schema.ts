/** Search body — `query` must contain at least one non-whitespace character
 * (minLength alone would let "   " through), and unknown keys are rejected so
 * a typo'd flag is a 400, never silently ignored.
 *
 * Day-16 retrieval knobs, all optional:
 *   limit         top-K; default 5 (tuned in the Day-16 evaluation: coverage
 *                 and hit-rate saturate by k=5 on the golden corpus).
 *   minSimilarity floor on cosine similarity; default 0. The Day-16 threshold
 *                 curve shows any positive floor trades recall away quickly,
 *                 so it exists for precision-critical callers, not by default.
 *   sourceType    document-level filter (pdf/docx/md/html/txt), applied before
 *                 ranking (hybrid filter: metadata first, vector second).
 *   metadata      exact-match JSONB containment on chunk metadata (e.g. a
 *                 section pin). Note: only sound when chunking preserved the
 *                 metadata — wide paragraph merges keep just the FIRST section
 *                 label (documented in the Day-16 evaluation).
 */
const searchBody = {
  type: "object",
  additionalProperties: false,
  required: ["query"],
  properties: {
    query: { type: "string", minLength: 1, maxLength: 500, pattern: "\\S" },
    limit: { type: "integer", minimum: 1, maximum: 20 },
    minSimilarity: { type: "number", minimum: 0, maximum: 1 },
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

const chunkObject = {
  type: "object",
  required: ["id", "chunkIndex", "pageNumber", "tokenCount", "content"],
  properties: {
    id: { type: "string" },
    chunkIndex: { type: "integer" },
    pageNumber: { type: "integer" },
    tokenCount: { type: "integer" },
    content: { type: "string" },
  },
} as const;

const documentRefObject = {
  type: "object",
  required: ["id", "title", "filename", "mimeType", "sourceType"],
  properties: {
    id: { type: "string" },
    title: { type: "string" },
    filename: { type: "string" },
    mimeType: { type: "string" },
    sourceType: { type: "string" },
  },
} as const;

const searchResultObject = {
  type: "object",
  required: ["chunk", "similarity", "document", "page", "metadata"],
  properties: {
    chunk: chunkObject,
    similarity: { type: "number" },
    document: documentRefObject,
    page: { type: "integer" },
    metadata: { type: "object", additionalProperties: true },
  },
} as const;

export const searchSchema = {
  body: searchBody,
  response: {
    200: {
      type: "object",
      required: ["query", "model", "results"],
      properties: {
        query: { type: "string" },
        model: { type: "string" },
        results: { type: "array", items: searchResultObject },
      },
    },
  },
} as const;

export interface SearchBodyInput {
  query: string;
  limit?: number;
  minSimilarity?: number;
  sourceType?: "pdf" | "docx" | "md" | "html" | "txt";
  metadata?: Record<string, string | number | boolean>;
}