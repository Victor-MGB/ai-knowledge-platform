/** Day 30 — Prometheus metrics for the backend.

 * Everything this process does gets exported on `GET /metrics` (scraped by
 * the compose Prometheus, no auth, never published to the host). HTTP request
 * latency / error rate come from a global onResponse hook; outbound AI calls
 * are timed by the services that make them (RagService, SearchService).
 */

import type { FastifyPluginAsync } from "fastify";
import fp from "fastify-plugin";
import { Counter, Histogram, Registry, collectDefaultMetrics } from "prom-client";

const registry = new Registry();
// process-level scrapers (cpu, memory, event loop, handles) for free
collectDefaultMetrics({ register: registry });

export const httpRequestsTotal = new Counter({
  name: "http_requests_total",
  help: "Total HTTP requests handled.",
  labelNames: ["method", "route", "status"],
  registers: [registry],
});

export const httpErrorsTotal = new Counter({
  name: "http_errors_total",
  help: "HTTP responses with a client/server error status (>= 400).",
  labelNames: ["method", "route", "status"],
  registers: [registry],
});

export const httpRequestDurationSeconds = new Histogram({
  name: "http_request_duration_seconds",
  help: "HTTP request latency in seconds.",
  labelNames: ["method", "route"],
  buckets: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
  registers: [registry],
});

/** Outbound AI-service calls (RAG generation, query embedding, enqueueing). */
export const aiRequestDurationSeconds = new Histogram({
  name: "ai_request_duration_seconds",
  help: "Latency of outbound calls to the AI service, by operation.",
  labelNames: ["operation"],
  buckets: [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60],
  registers: [registry],
});

/** Tokens consumed by AI calls, by operation and kind (prompt/completion/total). */
export const aiTokensTotal = new Counter({
  name: "ai_tokens_total",
  help: "Tokens consumed by outbound AI calls, by operation and kind.",
  labelNames: ["operation", "kind"],
  registers: [registry],
});

export interface AiCallUsage {
  prompt: number;
  completion: number;
  total: number;
}

export function observeAiCall(
  operation: string,
  durationSeconds: number,
  usage?: AiCallUsage
): void {
  aiRequestDurationSeconds.labels(operation).observe(durationSeconds);
  if (!usage) return;
  aiTokensTotal.labels(operation, "prompt").inc(usage.prompt);
  aiTokensTotal.labels(operation, "completion").inc(usage.completion);
  aiTokensTotal.labels(operation, "total").inc(usage.total);
}

export async function metricsText(): Promise<string> {
  return registry.metrics();
}

export const metricsContentType = registry.contentType;

export const metrics: FastifyPluginAsync = fp(async (app) => {
  app.addHook("onResponse", async (request, reply) => {
    const route = request.routeOptions?.url ?? "unmatched";
    const method = request.method;
    const status = String(reply.statusCode);
    httpRequestsTotal.labels(method, route, status).inc();
    httpRequestDurationSeconds
      .labels(method, route)
      .observe(reply.elapsedTime / 1000);
    if (reply.statusCode >= 400) {
      httpErrorsTotal.labels(method, route, status).inc();
    }
  });

  app.get(
    "/metrics",
    { config: { rateLimit: false } },
    async (_request, reply) => {
      // async handler + async onSend hooks (requestId, securityHeaders)
      // require returning the reply, not just calling send(): otherwise
      // Fastify can run the onSend pipeline twice on a real socket
      // ("Reply was already sent" + ERR_HTTP_HEADERS_SENT per scrape).
      return reply.type(metricsContentType).send(await metricsText());
    }
  );
});