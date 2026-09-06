/** Day 18 — Source API response schema.
 *
 * `GET /api/v1/sources/:documentId` resolves one citation back to the source
 * document it came from. Tenant-scoped like every other read: an id that is
 * unknown OR under another organization answers 404 NOT_FOUND, so cross-tenant
 * existence is never leaked. `pages` is the extracted page count of the
 * document, so a client can render "Sources [1] Employee Handbook — Page 14".
 */
const sourceResponse = {
  type: "object",
  required: ["document", "pages"],
  properties: {
    document: {
      type: "object",
      required: ["id", "title", "filename", "sourceType", "size"],
      properties: {
        id: { type: "string" },
        title: { type: "string" },
        filename: { type: "string" },
        sourceType: { type: "string" },
        size: { type: "integer" },
        createdAt: { type: "string" },
      },
    },
    pages: { type: "integer" },
  },
} as const;

export const getSourceSchema = {
  params: {
    type: "object",
    required: ["documentId"],
    properties: {
      documentId: { type: "string", pattern: "^[0-9a-fA-F-]{36}$" },
    },
  },
  response: {
    200: sourceResponse,
  },
} as const;
