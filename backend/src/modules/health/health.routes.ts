import type { FastifyPluginAsync } from "fastify";

import type { HealthReport } from "../../services/health.service.js";

export const healthRoutes: FastifyPluginAsync = async (app) => {
  app.get<{ Reply: HealthReport }>(
    "/health",
    {
      schema: {
        response: {
          200: {
            type: "object",
            required: ["status", "uptimeSeconds", "db"],
            properties: {
              status: { type: "string", enum: ["healthy", "degraded"] },
              uptimeSeconds: { type: "number" },
              db: {
                type: "object",
                required: ["reachable", "latencyMs", "version"],
                properties: {
                  reachable: { type: "boolean" },
                  latencyMs: { type: ["number", "null"] },
                  version: { type: ["string", "null"] },
                  error: { type: ["string", "null"] },
                },
              },
            },
          },
        },
      },
    },
    async () => app.healthService.check()
  );
};