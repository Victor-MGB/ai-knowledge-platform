import type { FastifyPluginAsync } from "fastify";
import fp from "fastify-plugin";

/** Propagate the native request id (x-request-id in, x-request-id out) so
 * clients and logs can correlate a request end to end. Global via fp(). */

export const requestId: FastifyPluginAsync = fp(async (app) => {
  app.addHook("onSend", async (request, reply) => {
    reply.header("x-request-id", request.id);
  });
});