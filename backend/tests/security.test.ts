import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { createHmac, randomUUID } from "node:crypto";

import type { FastifyInstance } from "fastify";

import { createPool, type DatabaseClient } from "../src/database/postgres.js";
import { loadConfig } from "../src/config/config.js";
import { buildApp } from "../src/app.js";
import type { QueryEmbedder } from "../src/services/embedding-client.service.js";

/** Day 29 — transport-level security controls, exercised through real routes:
 * rate limiting (429 envelope, proxy-aware keying, the tighter auth window),
 * allow-listed CORS, JWT hardening (alg:none, expired, missing-subject, and
 * opaque refresh tokens used as access tokens), strict input validation
 * (unknown-key rejection, no privilege escalation via payload), and SQL
 * injection (injection-shaped input is data, parameter-bound, never an error
 * and never a dropped table). DB-dependent cases skip when Postgres is away. */

async function databaseIsReachable(): Promise<boolean> {
  const pool = createPool(loadConfig());
  try {
    const { rows } = await pool.query("SELECT 1 AS one");
    return (rows[0] as { one?: number })?.one === 1;
  } catch {
    return false;
  } finally {
    await pool.end();
  }
}

const reachable = await databaseIsReachable();
const itDb = reachable ? it : it.skip;

const CLEAN_LOG = { LOG_LEVEL: "silent" } as NodeJS.ProcessEnv;

function appWith(env: NodeJS.ProcessEnv): Promise<FastifyInstance> {
  return buildApp({ config: loadConfig({ ...process.env, ...CLEAN_LOG, ...env }) });
}

describe("rate limiting", () => {
  it("answers with the structured 429 envelope, not a 500, once the window is exhausted", async () => {
    const app = await appWith({
      NODE_ENV: "development",
      RATE_LIMIT_MAX: "3",
      RATE_LIMIT_TIME_WINDOW_MS: "60000",
    });
    try {
      for (let i = 0; i < 3; i++) {
        const ok = await app.inject({ method: "GET", url: "/health" });
        expect(ok.statusCode).toBe(200);
      }
      const limited = await app.inject({ method: "GET", url: "/health" });
      expect(limited.statusCode).toBe(429);
      const body = limited.json() as { error: { code: string; message: string } };
      expect(body.error.code).toBe("RATE_LIMITED");
      expect(body.error.message).toMatch(/rate limit exceeded/);
    } finally {
      await app.close();
    }
  });

  it("keys callers by x-forwarded-for so clients behind one proxy stay distinct", async () => {
    const app = await appWith({
      NODE_ENV: "development",
      RATE_LIMIT_MAX: "2",
      RATE_LIMIT_TIME_WINDOW_MS: "60000",
    });
    try {
      const hit = (ip: string) =>
        app.inject({
          method: "GET",
          url: "/health",
          headers: { "x-forwarded-for": ip },
        });

      // 2 requests are inside Alice's budget; the 3rd is refused.
      await hit("203.0.113.10");
      await hit("203.0.113.10");
      const aliceExhausted = await hit("203.0.113.10");
      expect(aliceExhausted.statusCode).toBe(429);

      // Bob starts with a fresh budget despite sharing the proxy.
      const bobOne = await hit("203.0.113.99");
      const bobTwo = await hit("203.0.113.99");
      expect(bobOne.statusCode).toBe(200);
      expect(bobTwo.statusCode).toBe(200);
    } finally {
      await app.close();
    }
  });

  itDb("tightens the public auth surface below the global window", async () => {
    const app = await appWith({
      NODE_ENV: "development",
      RATE_LIMIT_MAX: "100",
      RATE_LIMIT_TIME_WINDOW_MS: "60000",
      AUTH_RATE_LIMIT_MAX: "2",
      AUTH_RATE_LIMIT_WINDOW_MS: "60000",
    });
    try {
      const attempt = () =>
        app.inject({
          method: "POST",
          url: "/auth/login",
          payload: { email: "nobody@example.com", password: "Sup3rSecret!" },
        });
      expect((await attempt()).statusCode).toBe(401); // unknown account
      expect((await attempt()).statusCode).toBe(401);
      const limited = await attempt();
      expect(limited.statusCode).toBe(429);
      expect((limited.json() as { error: { code: string } }).error.code).toBe(
        "RATE_LIMITED"
      );
    } finally {
      await app.close();
    }
  });
});

describe("CORS", () => {
  it("allow-lists exact origins and never answers with a wildcard", async () => {
    const app = await appWith({
      NODE_ENV: "development",
      CORS_ORIGINS: "https://app.example.com",
    });
    try {
      const preflight = await app.inject({
        method: "OPTIONS",
        url: "/api/v1/search",
        headers: {
          origin: "https://app.example.com",
          "access-control-request-method": "POST",
        },
      });
      expect(preflight.headers["access-control-allow-origin"]).toBe(
        "https://app.example.com"
      );
      expect(preflight.headers["access-control-allow-origin"]).not.toBe("*");

      const actual = await app.inject({
        method: "GET",
        url: "/health",
        headers: { origin: "https://app.example.com" },
      });
      expect(actual.headers["access-control-allow-origin"]).toBe(
        "https://app.example.com"
      );
    } finally {
      await app.close();
    }
  });

  it("withholds CORS headers from origins outside the allow-list", async () => {
    const app = await appWith({
      NODE_ENV: "development",
      CORS_ORIGINS: "https://app.example.com",
    });
    try {
      const evil = await app.inject({
        method: "GET",
        url: "/health",
        headers: { origin: "https://evil.example.com" },
      });
      expect(evil.headers["access-control-allow-origin"]).toBeUndefined();
    } finally {
      await app.close();
    }
  });

  it("serves no CORS headers while CORS_ORIGINS is unset (same-origin SPA)", async () => {
    const app = await appWith({ NODE_ENV: "development", CORS_ORIGINS: "" });
    try {
      const response = await app.inject({
        method: "GET",
        url: "/health",
        headers: { origin: "https://app.example.com" },
      });
      expect(response.headers["access-control-allow-origin"]).toBeUndefined();
    } finally {
      await app.close();
    }
  });
});

let app: FastifyInstance;
let pool: DatabaseClient;
const cleanup: Array<() => Promise<void>> = [];

const embedder: QueryEmbedder = {
  embed: async () => ({ vector: [0.6, 0.8, ...new Array(382).fill(0)], model: "knowflow-hash-384" }),
};

beforeAll(async () => {
  pool = createPool(loadConfig());
  app = await buildApp({ embedder });
});

afterAll(async () => {
  for (const fn of cleanup) {
    await fn();
  }
  await pool.end();
  await app.close();
});

function uniqueEmail(): string {
  return `sec-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
}

async function registerOrg(): Promise<{
  accessToken: string;
  refreshToken: string;
  userId: string;
  orgId: string;
}> {
  const email = uniqueEmail();
  const response = await app.inject({
    method: "POST",
    url: "/auth/register",
    payload: { email, password: "Sup3rSecret!" },
  });
  expect(response.statusCode).toBe(201);
  const body = response.json() as {
    user: { id: string };
    organization: { id: string };
    tokens: { accessToken: string; refreshToken: string };
  };
  cleanup.push(async () => {
    await pool.query("DELETE FROM organizations WHERE id = $1::uuid", [body.organization.id]);
  });
  return {
    accessToken: body.tokens.accessToken,
    refreshToken: body.tokens.refreshToken,
    userId: body.user.id,
    orgId: body.organization.id,
  };
}

function base64url(data: unknown): string {
  return Buffer.from(JSON.stringify(data)).toString("base64url");
}

/** A header + claims JWT with an empty signature and `alg: none`. */
function algNoneToken(claims: Record<string, unknown>): string {
  return `${base64url({ alg: "none", typ: "JWT" })}.${base64url(claims)}.`;
}

/** HS256 JWT signed with the app's own secret but already past its `exp`,
 * proving expiry is enforced server-side even for correctly-signed tokens. */
function expiredToken(app: FastifyInstance, claims: Record<string, unknown>): string {
  const now = Math.floor(Date.now() / 1000);
  const header = base64url({ alg: "HS256", typ: "JWT" });
  const payload = base64url({ iat: now - 300, exp: now - 60, jti: randomUUID(), ...claims });
  const signature = createHmac("sha256", app.config.JWT_SECRET)
    .update(`${header}.${payload}`)
    .digest("base64url");
  return `${header}.${payload}.${signature}`;
}

describe("JWT hardening", () => {
  itDb("rejects an 'alg: none' token", async () => {
    const owner = await registerOrg();
    const none = algNoneToken({
      sub: owner.userId,
      org: owner.orgId,
      role: "owner",
      typ: "access",
    });
    const response = await app.inject({
      method: "GET",
      url: "/api/v1/me",
      headers: { authorization: `Bearer ${none}` },
    });
    expect(response.statusCode).toBe(401);
    expect((response.json() as { error: { code: string } }).error.code).toBe("UNAUTHORIZED");
  });

  itDb("rejects an access token signed with a different secret", async () => {
    const owner = await registerOrg();
    const forgedApp = await appWith({
      NODE_ENV: "test",
      JWT_SECRET: "a-different-secret-that-the-real-app-never-uses",
    });
    try {
      const forged = forgedApp.jwt.sign({
        sub: owner.userId,
        org: owner.orgId,
        role: "owner",
        typ: "access",
        jti: randomUUID(),
      });
      const response = await app.inject({
        method: "GET",
        url: "/api/v1/me",
        headers: { authorization: `Bearer ${forged}` },
      });
      expect(response.statusCode).toBe(401);
      expect((response.json() as { error: { code: string } }).error.code).toBe(
        "UNAUTHORIZED"
      );
    } finally {
      await forgedApp.close();
    }
  });

  itDb("rejects an access token that has already expired", async () => {
    const owner = await registerOrg();
    const expired = expiredToken(app, {
      sub: owner.userId,
      org: owner.orgId,
      role: "owner",
      typ: "access",
    });
    const response = await app.inject({
      method: "GET",
      url: "/api/v1/me",
      headers: { authorization: `Bearer ${expired}` },
    });
    expect(response.statusCode).toBe(401);
  });

  itDb("rejects access tokens that carry no subject", async () => {
    const owner = await registerOrg();
    const noSub = app.jwt.sign({
      org: owner.orgId,
      role: "owner",
      typ: "access",
      jti: randomUUID(),
    });
    const response = await app.inject({
      method: "GET",
      url: "/api/v1/me",
      headers: { authorization: `Bearer ${noSub}` },
    });
    expect(response.statusCode).toBe(401);
    expect((response.json() as { error: { code: string } }).error.code).toBe("UNAUTHORIZED");
  });

  itDb("refuses an opaque refresh token presented as an access token", async () => {
    const owner = await registerOrg();
    const response = await app.inject({
      method: "GET",
      url: "/api/v1/me",
      headers: { authorization: `Bearer ${owner.refreshToken}` },
    });
    expect(response.statusCode).toBe(401);
  });
});

describe("input validation", () => {
  it("rejects an attempted privilege escalation in the register payload", async () => {
    const appNoDb = await appWith({ NODE_ENV: "test" });
    try {
      const response = await appNoDb.inject({
        method: "POST",
        url: "/auth/register",
        payload: {
          email: uniqueEmail(),
          password: "Sup3rSecret!",
          role: "owner",
        },
      });
      expect(response.statusCode).toBe(400);
      expect((response.json() as { error: { code: string } }).error.code).toBe(
        "VALIDATION_ERROR"
      );
    } finally {
      await appNoDb.close();
    }
  });

  it("rejects oversized login payloads before they touch the handler", async () => {
    const appNoDb = await appWith({ NODE_ENV: "test" });
    try {
      const response = await appNoDb.inject({
        method: "POST",
        url: "/auth/login",
        payload: {
          email: `${"a".repeat(321)}@example.com`,
          password: "Sup3rSecret!",
        },
      });
      expect(response.statusCode).toBe(400);
    } finally {
      await appNoDb.close();
    }
  });
});

describe("SQL injection resistance", () => {
  itDb("treats injection-shaped search queries as data and leaves the database intact", async () => {
    const owner = await registerOrg();
    const query = "x'); DROP TABLE users; --";
    const response = await app.inject({
      method: "POST",
      url: "/api/v1/search",
      headers: { authorization: `Bearer ${owner.accessToken}` },
      payload: { query },
    });
    // The string is bound as a parameter: never a 5xx, and never a dropped table.
    expect(response.statusCode).toBe(200);
    const body = response.json() as { query: string; results: unknown[] };
    expect(body.query).toBe(query);
    expect(Array.isArray(body.results)).toBe(true);

    const { rows } = await pool.query("SELECT count(*)::int AS n FROM users");
    expect((rows[0] as { n: number }).n).toBeGreaterThan(0);
  });

  itDb("rejects SQL-shaped login emails at validation, before any query runs", async () => {
    const response = await app.inject({
      method: "POST",
      url: "/auth/login",
      payload: { email: "admin' OR '1'='1", password: "Sup3rSecret!" },
    });
    expect(response.statusCode).toBe(400);
    expect((response.json() as { error: { code: string } }).error.code).toBe(
      "VALIDATION_ERROR"
    );
  });
});