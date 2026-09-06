import type { FastifyPluginAsync } from "fastify";
import fp from "fastify-plugin";
import fastifyCors from "@fastify/cors";

/**
 * Day 29 — strict, allow-listed CORS.
 *
 * The SPA is same-origin in both dev (vite `server.proxy`) and docker (nginx
 * `/api` + `/auth`), so cross-origin browser clients are the *exception*. CORS
 * stays off by default; when `CORS_ORIGINS` is set it is an explicit
 * comma-separated allow-list — never a wildcard — and the allowed methods and
 * headers are pinned to what the API actually needs (Bearer auth, JSON bodies,
 * the SSE stream).
 */
export const cors = fp(async (app) => {
  const raw = app.config.CORS_ORIGINS;
  if (!raw) {
    app.log.info("CORS disabled: CORS_ORIGINS is empty (same-origin SPA)");
    return;
  }

  const origins = raw
    .split(",")
    .map((origin) => origin.trim())
    .filter(Boolean);

  if (origins.length === 0) {
    app.log.info("CORS disabled: CORS_ORIGINS is empty (same-origin SPA)");
    return;
  }

  await app.register(fastifyCors, {
    origin: origins,
    methods: ["GET", "POST", "DELETE", "OPTIONS"],
    allowedHeaders: ["Content-Type", "Authorization"],
    credentials: false, // Bearer tokens travel in headers, never cookies
    maxAge: 600,
  });
  app.log.info(`CORS enabled for allow-list: ${JSON.stringify(origins)}`);
});