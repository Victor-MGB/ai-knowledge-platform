/** Conversations module schemas (Day 19).
 *
 * A conversation is a tenant's chat session; its transcript is the ordered
 * `messages` log. `POST .../messages` runs the RAG pipeline and stores the
 * user turn + assistant answer (with its `payload` citation provenance) as one
 * atomic pair. All ids are UUIDs; every read is tenant-scoped server-side.
 */

const conversationIdParams = {
  type: "object",
  additionalProperties: false,
  required: ["id"],
  properties: {
    id: {
      type: "string",
      pattern:
        "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    },
  },
} as const;

const messageObject = {
  type: "object",
  required: ["id", "conversationId", "position", "role", "content", "payload", "createdAt"],
  properties: {
    id: { type: "string" },
    organizationId: { type: "string" },
    conversationId: { type: "string" },
    position: { type: "integer" },
    role: { type: "string", enum: ["user", "assistant", "system"] },
    content: { type: "string" },
    // payload is deliberately free-form jsonb (assistant citation provenance).
    // additionalProperties must be true so fast-json-stringify serializes the
    // stored keys — a bare { type: "object" } would collapse every turn to {}.
    payload: { type: "object", additionalProperties: true },
    createdAt: { type: "string" },
  },
} as const;

const conversationObject = {
  type: "object",
  required: [
    "id",
    "organizationId",
    "title",
    "createdBy",
    "createdAt",
    "updatedAt",
    "messageCount",
    "lastMessageAt",
  ],
  properties: {
    id: { type: "string" },
    organizationId: { type: "string" },
    title: { type: "string" },
    createdBy: { type: "string" },
    createdAt: { type: "string" },
    updatedAt: { type: "string" },
    messageCount: { type: "integer" },
    lastMessageAt: { type: ["string", "null"] },
    messages: { type: "array", items: messageObject },
  },
} as const;

const paginationObject = {
  type: "object",
  required: ["limit", "offset", "total", "hasMore"],
  properties: {
    limit: { type: "integer" },
    offset: { type: "integer" },
    total: { type: "integer" },
    hasMore: { type: "boolean" },
  },
} as const;

/** RAG knobs forwarded to /rag/generate when a message is asked. Same bounds
 *  as the rag module so a typo'd floor is a 400, never a silent oddity. */
const ragOptionsObject = {
  type: "object",
  additionalProperties: false,
  properties: {
    limit: { type: "integer", minimum: 1, maximum: 20 },
    maxContextTokens: { type: "integer", minimum: 50, maximum: 2000 },
    minScore: { type: "number", minimum: 0, maximum: 1 },
    sourceType: { type: "string", enum: ["pdf", "docx", "md", "html", "txt"] },
    metadata: {
      type: "object",
      maxProperties: 20,
      propertyNames: { maxLength: 64 },
      additionalProperties: { type: ["string", "number", "boolean"] },
    },
  },
} as const;

export const createConversationSchema = {
  body: {
    type: "object",
    additionalProperties: false,
    required: [],
    properties: {
      title: { type: "string", minLength: 1, maxLength: 200 },
    },
  },
  response: { 201: conversationObject },
} as const;

export const listConversationsSchema = {
  querystring: {
    type: "object",
    additionalProperties: false,
    properties: {
      limit: { type: "integer", minimum: 1, maximum: 100 },
      offset: { type: "integer", minimum: 0 },
    },
  },
  response: {
    200: {
      type: "object",
      required: ["items", "pagination"],
      properties: {
        items: { type: "array", items: conversationObject },
        pagination: paginationObject,
      },
    },
  },
} as const;

export const getConversationSchema = {
  params: conversationIdParams,
  response: { 200: conversationObject },
} as const;

/** Optional pre-generated assistant turn — the answer the caller already
 *  streamed via /rag/generate/stream, persisted verbatim so the transcript
 *  matches what the user read (no second RAG pass on the server). */
const assistantTurnObject = {
  type: "object",
  additionalProperties: false,
  required: ["content"],
  properties: {
    content: { type: "string", minLength: 1, maxLength: 8000 },
    refused: { type: "boolean" },
    provider: { type: "string" },
    model: { type: "string" },
    citations: {
      type: "array",
      items: {
        type: "object",
        required: ["id"],
        additionalProperties: false,
        properties: {
          id: { type: "integer" },
          title: { type: ["string", "null"] },
          section: { type: ["string", "null"] },
          page: { type: ["integer", "null"] },
          chunkId: { type: ["string", "null"] },
          documentId: { type: ["string", "null"] },
          similarity: { type: ["number", "null"] },
        },
      },
    },
    usage: {
      type: "object",
      additionalProperties: false,
      properties: {
        promptTokens: { type: "integer" },
        completionTokens: { type: "integer" },
        totalTokens: { type: "integer" },
      },
    },
  },
} as const;

export const addMessageSchema = {
  params: conversationIdParams,
  body: {
    type: "object",
    additionalProperties: false,
    required: ["content"],
    properties: {
      content: { type: "string", minLength: 1, maxLength: 4000, pattern: "\\S" },
      rag: ragOptionsObject,
      assistant: assistantTurnObject,
    },
  },
  response: { 201: conversationObject },
} as const;

export const deleteConversationSchema = {
  params: conversationIdParams,
  response: { 204: { type: "null" } },
} as const;

export interface CreateConversationInput {
  title?: string;
}

export interface AddMessageInput {
  content: string;
  rag?: {
    limit?: number;
    maxContextTokens?: number;
    minScore?: number;
    sourceType?: "pdf" | "docx" | "md" | "html" | "txt";
    metadata?: Record<string, string | number | boolean>;
  };
  /** Pre-generated assistant turn to persist verbatim instead of re-running
   *  RAG on the server (the streaming UI already generated the answer). */
  assistant?: {
    content: string;
    refused?: boolean;
    provider?: string;
    model?: string;
    citations?: {
      id: number;
      title?: string | null;
      section?: string | null;
      page?: number | null;
      chunkId?: string | null;
      documentId?: string | null;
      similarity?: number | null;
    }[];
    usage?: { promptTokens?: number; completionTokens?: number; totalTokens?: number };
  };
}
