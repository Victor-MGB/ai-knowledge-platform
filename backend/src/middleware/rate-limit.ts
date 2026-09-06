import type { FastifyPluginAsync } from "fastify";
import fp from "fastify-plugin";
import fastifyRateLimit, { type RateLimitOptions } from "@fastify/rate-limit";
import type { Config } from "../config/config.js";

/** Route options that pin the auth surface to a tight window. Config-driven so
 * the test suite (NODE_ENV=test) runs unlimited while dev/prod keep the
 * brute-force protection. */
export const authRateLimit = (config: Config) => ({
  config: {
    rateLimit: {
      max:
        config.NODE_ENV === "test"
          ? Number.MAX_SAFE_INTEGER
          : config.AUTH_RATE_LIMIT_MAX,
      timeWindow: config.AUTH_RATE_LIMIT_WINDOW_MS,
    },
  },
});

/**
 * Day 29 — request rate limiting.
 *
 * A global baseline guards every endpoint against runaway callers, and the
 * public auth surface (register / login / refresh / invitation-accept) gets a
 * much tighter window because those are the brute-force and
 * credential-stuffing targets: no token and no persistence beyond the memory
 * window lets an attacker hammer passwords or refresh tokens freely.
 *
 * The window keys on the client address, honouring the `x-forwarded-for`
 * header that nginx injects in the docker topology so all callers behind one
 * proxy are still distinct. Overflow answers 429 in the KnowFlow envelope.
 */
export const rateLimit: FastifyPluginAsync = fp(async (app) => {
  // Test suites hammer one loopback address from a shared app instance; a
  // window that proves the feature would break unrelated tests. The feature is
  // still exercised by the dedicated security tests, which build their own app
  // with an explicit low limit.
  const inTests = app.config.NODE_ENV === "test";
  await app.register(fastifyRateLimit, {
    global: true,
    max: inTests ? Number.MAX_SAFE_INTEGER : app.config.RATE_LIMIT_MAX,
    timeWindow: app.config.RATE_LIMIT_TIME_WINDOW_MS,
    keyGenerator: (request) => {
      const forwarded = request.headers["x-forwarded-for"];
      if (typeof forwarded === "string" && forwarded.length > 0) {
        return (forwarded.split(",")[0] ?? request.ip ?? "unknown").trim();
      }
      return request.ip ?? "unknown";
    },
    errorResponseBuilder: (_request, context) => ({
      statusCode: 429,
      error: {
        code: "RATE_LIMITED",
        message: `rate limit exceeded: retry after ${context.after}`,
      },
    }),
  } as RateLimitOptions);
});