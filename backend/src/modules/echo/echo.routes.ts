import type { FastifyPluginAsync } from "fastify";

const echoBodySchema = {
  type: "object",
  additionalProperties: false,
  required: ["message"],
  properties: {
    message: { type: "string", minLength: 1, maxLength: 500 },
  },
} as const;

interface EchoBody {
  message: string;
}

/** Validated echo - the skeleton's demonstration of Fastify JSON-Schema
 * request validation (reject unknown/empty payloads with a 400 envelope). */
export const echoRoutes: FastifyPluginAsync = async (app) => {
  app.post<{ Body: EchoBody }>(
    "/echo",
    {
      schema: {
        body: echoBodySchema,
        response: {
          200: {
            type: "object",
            required: ["received"],
            properties: { received: { type: "string" } },
          },
        },
      },
    },
    async (request) => ({ received: request.body.message })
  );
};