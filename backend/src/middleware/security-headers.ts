import type { FastifyPluginAsync } from "fastify";
import fp from "fastify-plugin";

/** Apply a baseline set of hardening headers to every response.
 * Wrapped in fastify-plugin so the onSend hook applies globally, breaking
 * Fastify's plugin-encapsulation scoping. */
export const securityHeaders: FastifyPluginAsync = fp(async (app) => {
  app.addHook("onSend", async (_request, reply) => {
    reply
      .header("x-content-type-options", "nosniff")
      .header("x-frame-options", "DENY")
      .header("referrer-policy", "no-referrer")
      .header("x-xss-protection", "0");
  });
});