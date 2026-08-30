import { describe, expect, it } from "vitest";

import { buildApp } from "../src/app.js";
import type { FastifyInstance } from "fastify";

function fakePool(behaviour: "up" | "down"): {
  query: () => Promise<{ rows: unknown[] }>;
  end: () => Promise<void>;
} {
  if (behaviour === "up") {
    return {
      query: async () => ({ rows: [{ version: "PostgreSQL 18.6 (fake)" }] }),
      end: async () => {},
    };
  }
  return {
    query: async () => {
      throw new Error("connection refused");
    },
    end: async () => {},
  };
}

async function build(pool?: ReturnType<typeof fakePool>): Promise<FastifyInstance> {
  return buildApp({ pool: pool ?? fakePool("up") });
}

describe("health endpoint", () => {
  it("reports healthy when the database answers", async () => {
    const app = await build();
    const response = await app.inject({ method: "GET", url: "/health" });
    expect(response.statusCode).toBe(200);
    const body = response.json();
    expect(body.status).toBe("healthy");
    expect(body.db.reachable).toBe(true);
    expect(body.db.version).toContain("PostgreSQL");
    await app.close();
  });

  it("degrades (still 200) when the database is unreachable", async () => {
    const app = await build(fakePool("down"));
    const response = await app.inject({ method: "GET", url: "/health" });
    expect(response.statusCode).toBe(200);
    const body = response.json();
    expect(body.status).toBe("degraded");
    expect(body.db.reachable).toBe(false);
    expect(body.db.error).toBe("connection refused");
    await app.close();
  });
});

describe("echo endpoint validation", () => {
  it("accepts a valid message", async () => {
    const app = await build();
    const response = await app.inject({
      method: "POST",
      url: "/api/v1/echo",
      payload: { message: "hello backend" },
    });
    expect(response.statusCode).toBe(200);
    expect(response.json()).toEqual({ received: "hello backend" });
    await app.close();
  });

  it("rejects an empty message with the error envelope", async () => {
    const app = await build();
    const response = await app.inject({
      method: "POST",
      url: "/api/v1/echo",
      payload: { message: "" },
    });
    expect(response.statusCode).toBe(400);
    expect(response.json().error.code).toBe("VALIDATION_ERROR");
    await app.close();
  });

  it("rejects unknown payload fields (additionalProperties: false)", async () => {
    const app = await build();
    const response = await app.inject({
      method: "POST",
      url: "/api/v1/echo",
      payload: { message: "ok", sneaky: true },
    });
    expect(response.statusCode).toBe(400);
    await app.close();
  });
});

describe("production hygiene", () => {
  it("renders a JSON 404 envelope for unknown routes", async () => {
    const app = await build();
    const response = await app.inject({ method: "GET", url: "/nope" });
    expect(response.statusCode).toBe(404);
    expect(response.json().error.code).toBe("NOT_FOUND");
    await app.close();
  });

  it("applies security headers to all responses", async () => {
    const app = await build();
    const response = await app.inject({ method: "GET", url: "/health" });
    expect(response.headers["x-content-type-options"]).toBe("nosniff");
    expect(response.headers["x-frame-options"]).toBe("DENY");
    expect(response.headers["referrer-policy"]).toBe("no-referrer");
    await app.close();
  });

  it("echoes the request id header", async () => {
    const app = await build();
    const response = await app.inject({
      method: "GET",
      url: "/health",
      headers: { "x-request-id": "abc-123" },
    });
    expect(response.headers["x-request-id"]).toBe("abc-123");
    await app.close();
  });
});