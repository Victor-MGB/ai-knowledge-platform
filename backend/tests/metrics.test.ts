import { describe, expect, it } from "vitest";

import { buildApp } from "../src/app.js";
import type { FastifyInstance } from "fastify";
import {
  metricsText,
  observeAiCall,
} from "../src/middleware/metrics.js";

function fakePool(): {
  query: () => Promise<{ rows: unknown[] }>;
  end: () => Promise<void>;
} {
  return {
    query: async () => ({ rows: [{ version: "PostgreSQL 18.6 (fake)" }] }),
    end: async () => {},
  };
}

describe("Prometheus /metrics", () => {
  it("exposes the scrape endpoint with prometheus content type", async () => {
    const app = await buildApp({ pool: fakePool() });
    try {
      const response = await app.inject({ method: "GET", url: "/metrics" });
      expect(response.statusCode).toBe(200);
      expect(response.headers["content-type"]).toContain("text/plain");
      expect(response.body).toContain("# HELP http_requests_total");
    } finally {
      await app.close();
    }
  });

  it("labels matched requests with status and latency", async () => {
    const app = await buildApp({ pool: fakePool() });
    try {
      await app.inject({ method: "GET", url: "/health" });
      const text = await metricsText();
      expect(text).toContain('http_requests_total{method="GET",route="/health",status="200"} 1');
      expect(text).toContain('http_request_duration_seconds_bucket{le="0.005",method="GET",route="/health"');
    } finally {
      await app.close();
    }
  });

  it("counts error responses toward the error-rate counter", async () => {
    const app = await buildApp({ pool: fakePool() });
    try {
      const missing = await app.inject({ method: "GET", url: "/api/v1/does-not-exist" });
      expect(missing.statusCode).toBe(404);
      const text = await metricsText();
      expect(text).toContain('http_errors_total{method="GET",route="unmatched",status="404"} 1');
    } finally {
      await app.close();
    }
  });

  it("observes outbound AI latency and token usage", async () => {
    observeAiCall("rag.generate", 0.125, { prompt: 40, completion: 15, total: 55 });
    const text = await metricsText();
    expect(text).toContain('ai_request_duration_seconds_bucket{le="0.01",operation="rag.generate"');
    expect(text).toContain('ai_tokens_total{operation="rag.generate",kind="prompt"} 40');
    expect(text).toContain('ai_tokens_total{operation="rag.generate",kind="completion"} 15');
    expect(text).toContain('ai_tokens_total{operation="rag.generate",kind="total"} 55');
  });
});