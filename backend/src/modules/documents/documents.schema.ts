/** Document param — ids must look like UUIDs (a malformed id is a validation
 * error, not a SQL probe). Pattern is validation-only; the repository still
 * scopes every lookup by organization. */
const documentParams = {
  type: "object",
  additionalProperties: false,
  required: ["id"],
  properties: {
    id: {
      type: "string",
      pattern: "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    },
  },
} as const;

const documentObject = {
  type: "object",
  required: [
    "id",
    "organizationId",
    "uploadedBy",
    "title",
    "filename",
    "mimeType",
    "size",
    "sourceType",
    "storageKey",
    "status",
    "error",
    "embeddingStatus",
    "embeddingError",
    "createdAt",
    "updatedAt",
  ],
  properties: {
    id: { type: "string" },
    organizationId: { type: "string" },
    uploadedBy: { type: "string" },
    title: { type: "string" },
    filename: { type: "string" },
    mimeType: { type: "string" },
    size: { type: "integer" },
    sourceType: { type: "string" },
    storageKey: { type: ["string", "null"] },
    status: { type: "string" },
    error: { type: ["string", "null"] },
    embeddingStatus: { type: "string" },
    embeddingError: { type: ["string", "null"] },
    createdAt: { type: "string" },
    updatedAt: { type: "string" },
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

/** Query filters are optional, enum-checked, and unknown keys are rejected
 * (additionalProperties: false) so a typo'd filter surfaces as a 400, not a
 * silently ignored one. Fastify coerces limit/offset strings to integers. */
export const listDocumentsQuery = {
  type: "object",
  additionalProperties: false,
  properties: {
    limit: { type: "integer", minimum: 1, maximum: 100 },
    offset: { type: "integer", minimum: 0 },
    sourceType: {
      type: "string",
      enum: ["pdf", "docx", "md", "html", "txt"],
    },
    status: {
      type: "string",
      enum: ["queued", "processing", "ready", "failed"],
    },
    embeddingStatus: {
      type: "string",
      enum: ["none", "processing", "ready", "failed"],
    },
    search: { type: "string", maxLength: 200, minLength: 1 },
  },
} as const;

export const uploadDocumentSchema = {
  response: { 201: documentObject },
} as const;

export const listDocumentsSchema = {
  querystring: listDocumentsQuery,
  response: {
    200: {
      type: "object",
      required: ["items", "pagination"],
      properties: {
        items: { type: "array", items: documentObject },
        pagination: paginationObject,
      },
    },
  },
} as const;

export const getDocumentSchema = {
  params: documentParams,
  response: { 200: documentObject },
} as const;

export const deleteDocumentSchema = {
  params: documentParams,
} as const;

export const documentStatusSchema = {
  params: documentParams,
  response: {
    200: {
      type: "object",
      required: [
        "id",
        "organizationId",
        "filename",
        "sourceType",
        "status",
        "error",
        "embeddingStatus",
        "embeddingError",
        "counts",
        "stage",
      ],
      properties: {
        id: { type: "string" },
        organizationId: { type: "string" },
        filename: { type: "string" },
        sourceType: { type: "string" },
        status: { type: "string" },
        error: { type: ["string", "null"] },
        embeddingStatus: { type: "string" },
        embeddingError: { type: ["string", "null"] },
        counts: {
          type: "object",
          required: ["pages", "chunks", "embeddings"],
          properties: {
            pages: { type: "integer" },
            chunks: { type: "integer" },
            embeddings: { type: "integer" },
          },
        },
        stage: {
          type: "string",
          enum: ["uploaded", "processing", "embedding", "ready", "failed"],
        },
      },
    },
  },
} as const;

export interface ListDocumentsQueryInput {
  limit?: number | string;
  offset?: number | string;
  sourceType?: string;
  status?: string;
  embeddingStatus?: string;
  search?: string;
}