import { afterAll, beforeAll, describe, expect, it } from "vitest";

import type { FastifyInstance } from "fastify";
import type { TokenPair, AuthUserView } from "../src/services/auth.service.js";

import { createPool } from "../src/database/postgres.js";
import { loadConfig } from "../src/config/config.js";
import { buildApp } from "../src/app.js";

/** End-to-end auth against the real Day-3 pgvector container. Skipped when the
 * database is unreachable. Every created organization is tracked and deleted
 * afterwards (FK cascade cleans users, refresh tokens and documents). */

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

let app: FastifyInstance;
const createdOrgIds: string[] = [];

beforeAll(async () => {
  app = await buildApp();
});

afterAll(async () => {
  const pool = createPool(loadConfig());
  try {
    for (const id of createdOrgIds) {
      await pool.query("DELETE FROM organizations WHERE id = $1::uuid", [id]);
    }
  } finally {
    await pool.end();
    await app.close();
  }
});

function uniqueEmail(): string {
  return `auth-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
}

interface RegisterBody {
  user: AuthUserView;
  organization: { id: string; name: string; slug: string };
  tokens: TokenPair;
}

async function register(email: string): Promise<RegisterBody> {
  const response = await app.inject({
    method: "POST",
    url: "/auth/register",
    payload: { email, password: "Sup3rSecret!" },
  });
  expect(response.statusCode).toBe(201);
  const body = response.json() as RegisterBody;
  createdOrgIds.push(body.organization.id);
  return body;
}

itDb("register creates a tenant + owner and the access token reaches /me", async () => {
  const email = uniqueEmail();
  const reg = await register(email);

  expect(reg.user.email).toBe(email);
  expect(reg.user.role).toBe("owner");
  expect(reg.user.organizationId).toBe(reg.organization.id);
  expect(reg.organization.slug).toMatch(/^auth-[a-z0-9-]+-[0-9a-f]{6}$/);

  const me = await app.inject({
    method: "GET",
    url: "/api/v1/me",
    headers: { authorization: `Bearer ${reg.tokens.accessToken}` },
  });
  expect(me.statusCode).toBe(200);
  expect((me.json() as AuthUserView).email).toBe(email);
});

itDb("email uniqueness is enforced per (organization, email) by the schema", async () => {
  const email = uniqueEmail();
  const org = (await register(email)).organization;

  // register always spawns a fresh org, so this code path is not reachable
  // through the public API yet - assert the DB constraint it guards against,
  // which future endpoints (e.g. inviting a user into an existing org) rely on.
  // register already placed (org, email), so one more insert into the same
  // org must hit the unique constraint
  const pool = createPool(loadConfig());
  try {
    await expect(
      pool.query(
        "INSERT INTO users (organization_id, email, password_hash, role) VALUES ($1::uuid, $2, $3, 'member')",
        [org.id, email, "irrelevant-hash"]
      )
    ).rejects.toMatchObject({ code: "23505" });
  } finally {
    await pool.end();
  }
});

itDb("login rejects unknown email and wrong password with the same error", async () => {
  const email = uniqueEmail();
  await register(email);

  const wrongPassword = await app.inject({
    method: "POST",
    url: "/auth/login",
    payload: { email, password: "TotallyWrong123" },
  });
  expect(wrongPassword.statusCode).toBe(401);
  expect(wrongPassword.json().error.code).toBe("INVALID_CREDENTIALS");

  const unknownEmail = await app.inject({
    method: "POST",
    url: "/auth/login",
    payload: { email: `nope-${uniqueEmail()}`, password: "Sup3rSecret!" },
  });
  expect(unknownEmail.statusCode).toBe(401);
  expect(unknownEmail.json().error.code).toBe("INVALID_CREDENTIALS");
});

itDb("login succeeds and disambiguates the same email across tenants", async () => {
  const email = uniqueEmail();
  const orgA = await register(email);
  const orgB = await register(email);

  const ambiguous = await app.inject({
    method: "POST",
    url: "/auth/login",
    payload: { email, password: "Sup3rSecret!" },
  });
  expect(ambiguous.statusCode).toBe(422);
  expect(ambiguous.json().error.code).toBe("MULTIPLE_ACCOUNTS");

  const scoped = await app.inject({
    method: "POST",
    url: "/auth/login",
    payload: { email, password: "Sup3rSecret!", organization: orgA.organization.slug },
  });
  expect(scoped.statusCode).toBe(200);
  const body = scoped.json() as { user: AuthUserView };
  expect(body.user.organizationId).toBe(orgA.organization.id);
  expect(body.user.organizationId).not.toBe(orgB.organization.id);
});

itDb("refresh rotates tokens and rejects replay of a rotated one", async () => {
  const reg = await register(uniqueEmail());

  const first = await app.inject({
    method: "POST",
    url: "/auth/refresh",
    payload: { refreshToken: reg.tokens.refreshToken },
  });
  expect(first.statusCode).toBe(200);
  const newPair = first.json() as TokenPair;
  expect(newPair.accessToken).toBeTruthy();

  const replay = await app.inject({
    method: "POST",
    url: "/auth/refresh",
    payload: { refreshToken: reg.tokens.refreshToken },
  });
  expect(replay.statusCode).toBe(401);
  expect(replay.json().error.code).toBe("INVALID_REFRESH_TOKEN");

  const rotateAgain = await app.inject({
    method: "POST",
    url: "/auth/refresh",
    payload: { refreshToken: newPair.refreshToken },
  });
  expect(rotateAgain.statusCode).toBe(200);
});

itDb("logout revokes the refresh token", async () => {
  const reg = await register(uniqueEmail());

  const logout = await app.inject({
    method: "POST",
    url: "/auth/logout",
    payload: { refreshToken: reg.tokens.refreshToken },
  });
  expect(logout.statusCode).toBe(204);

  const reuse = await app.inject({
    method: "POST",
    url: "/auth/refresh",
    payload: { refreshToken: reg.tokens.refreshToken },
  });
  expect(reuse.statusCode).toBe(401);
});

itDb("protected routes reject missing, malformed and foreign tokens", async () => {
  const reg = await register(uniqueEmail());

  const missing = await app.inject({ method: "GET", url: "/api/v1/me" });
  expect(missing.statusCode).toBe(401);
  expect(missing.json().error.code).toBe("UNAUTHORIZED");

  const malformed = await app.inject({
    method: "GET",
    url: "/api/v1/me",
    headers: { authorization: "Bearer not-a-real-token" },
  });
  expect(malformed.statusCode).toBe(401);

  // a token signed with a different secret must be rejected
  const otherApp = await buildApp({
    config: loadConfig({ JWT_SECRET: "z".repeat(48) }),
  });
  try {
    const foreign = await otherApp.inject({
      method: "GET",
      url: "/api/v1/me",
      headers: { authorization: `Bearer ${reg.tokens.accessToken}` },
    });
    expect(foreign.statusCode).toBe(401);
  } finally {
    await otherApp.close();
  }
});