import type { FastifyPluginAsync } from "fastify";

import { requireAuth } from "../../middleware/auth-guard.js";
import { AppError } from "../../utils/errors.js";
import { getSourceSchema } from "./sources.schema.js";

/** Day 18 — Source API under /api/v1/sources. Resolves one of a RAG answer's
 * citations (`citations[].documentId`) back to the source document. Tenant is
 * taken from the authenticated token, never the client; an unknown or foreign
 * id answers NOT_FOUND like every other read. */
export const sourceRoutes: FastifyPluginAsync = async (app) => {
  app.get(
    "/sources/:documentId",
    { schema: getSourceSchema, preHandler: [requireAuth] },
    async (request, reply) => {
      const { documentId } = request.params as { documentId: string };
      const source = await app.sourcesService.get(request.user.org, request.user.sub, documentId);
      if (!source) {
        throw new AppError("NOT_FOUND", "source document not found", 404);
      }
      return reply.code(200).send(source);
    }
  );
};
