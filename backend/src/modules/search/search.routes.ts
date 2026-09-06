import type { FastifyPluginAsync } from "fastify";

import { requireAuth } from "../../middleware/auth-guard.js";
import { type SearchBodyInput, searchSchema } from "./search.schema.js";

/** Day 15: semantic retrieval — `POST /api/v1/search`. Tenant comes from the
 * token and stays server-side; search is open to any member (same posture as
 * reads). The query vector comes from the model that indexed the corpus. */
export const searchRoutes: FastifyPluginAsync = async (app) => {
  app.post(
    "/search",
    { schema: searchSchema, preHandler: [requireAuth] },
    async (request) => {
      const body = request.body as SearchBodyInput;
      return app.searchService.search(request.user.org, request.user.sub, body.query.trim(), {
        limit: body.limit,
        minSimilarity: body.minSimilarity,
        sourceType: body.sourceType,
        metadata: body.metadata,
      });
    }
  );
};