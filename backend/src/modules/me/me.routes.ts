import type { FastifyPluginAsync } from "fastify";

import { requireAuth } from "../../middleware/auth-guard.js";
import { meSchema } from "../auth/auth.schema.js";

/** The canonical "who am I" protected route - proves the token -> user path. */
export const meRoutes: FastifyPluginAsync = async (app) => {
  app.get<{ Reply: object }>(
    "/me",
    { schema: meSchema, preHandler: [requireAuth] },
    async (request) => app.authService.profile(request.user.sub)
  );
};