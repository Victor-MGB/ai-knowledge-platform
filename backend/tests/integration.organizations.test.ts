import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { randomUUID } from "node:crypto";

import type { FastifyInstance } from "fastify";

import { createPool, type DatabaseClient } from "../src/database/postgres.js";
import { loadConfig } from "../src/config/config.js";
import { buildApp } from "../src/app.js";
import { hashToken } from "../src/services/token.service.js";

/** Day 22 — organizations end-to-end against the real database: tenant
 *  profile, membership listing + role changes (OWNER>ADMIN>MEMBER>VIEWER),
 *  invitations create/list/revoke, and the public accept flow that turns a
 *  token into a real membership. Skipped when the database is unreachable. */

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
let pool: DatabaseClient;
const cleanup: Array<() => Promise<void>> = [];

beforeAll(async () => {
  pool = createPool(loadConfig());
  app = await buildApp();
  // day-22 org module depends on the invitations table from migration 011
  const { rows } = await pool.query(
    "SELECT 1 AS one FROM information_schema.tables WHERE table_name = 'invitations'"
  );
  if ((rows[0] as { one?: number } | undefined)?.one !== 1) {
    throw new Error("migration 011 (invitations) not applied; run backend/src/database/migrations/011_organizations.sql");
  }
});
afterAll(async () => {
  for (const fn of cleanup) await fn();
  await app.close();
  await pool.end();
});

function uniqueEmail(): string {
  return `org-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
}

interface OrgCtx {
  accessToken: string;
  orgId: string;
  ownerId: string;
  slug: string;
}

async function registerOrg(): Promise<OrgCtx> {
  const email = uniqueEmail();
  const response = await app.inject({
    method: "POST",
    url: "/auth/register",
    payload: { email, password: "Sup3rSecret!" },
  });
  expect(response.statusCode).toBe(201);
  const body = response.json() as {
    user: { id: string; role: string };
    organization: { id: string; slug: string };
    tokens: { accessToken: string };
  };
  expect(body.user.role).toBe("owner");
  cleanup.push(async () => {
    await pool.query("DELETE FROM organizations WHERE id = $1::uuid", [body.organization.id]);
  });
  return {
    accessToken: body.tokens.accessToken,
    orgId: body.organization.id,
    ownerId: body.user.id,
    slug: body.organization.slug,
  };
}

/** Insert a member user directly for role-based tests (register always makes
 *  an owner). Returns a login-scoped access token via the normal API. */
async function addUser(ctx: OrgCtx, role: string): Promise<{ id: string; token: string; email: string }> {
  const email = uniqueEmail();
  const passwordHash = "irrelevant-bcrypt-hash-starting-with-2b-";
  const { rows } = await pool.query(
    `INSERT INTO users (organization_id, email, password_hash, role)
     VALUES ($1::uuid, $2, $3, $4) RETURNING id`,
    [ctx.orgId, email, passwordHash, role]
  );
  const id = (rows[0] as { id: string }).id;

  // mint a real access token for that user via the JWT signer
  const accessToken = app.jwt.sign({
    sub: id,
    org: ctx.orgId,
    role,
    typ: "access",
    jti: randomUUID(),
  });
  return { id, token: accessToken, email };
}

describe("GET /api/v1/organizations (profile)", () => {
  itDb("returns the tenant profile with its member count", async () => {
    const org = await registerOrg();
    const response = await app.inject({
      method: "GET",
      url: "/api/v1/organizations",
      headers: { authorization: `Bearer ${org.accessToken}` },
    });
    expect(response.statusCode).toBe(200);
    const body = response.json() as {
      id: string;
      name: string;
      slug: string;
      memberCount: number;
    };
    expect(body.id).toBe(org.orgId);
    expect(body.slug).toBe(org.slug);
    expect(body.memberCount).toBe(1);
  });
});

describe("GET /api/v1/organizations/members", () => {
  itDb("lists the org's members with their roles", async () => {
    const org = await registerOrg();
    await addUser(org, "admin");

    const response = await app.inject({
      method: "GET",
      url: "/api/v1/organizations/members",
      headers: { authorization: `Bearer ${org.accessToken}` },
    });
    expect(response.statusCode).toBe(200);
    const body = response.json() as {
      organization: { id: string; memberCount: number };
      items: Array<{ role: string; email: string }>;
    };
    expect(body.organization.id).toBe(org.orgId);
    expect(body.organization.memberCount).toBe(2);
    expect(body.items.map((m) => m.role).sort()).toEqual(["admin", "owner"]);
  });

  itDb("hides another tenant's members (404 via foreign org never exposed)", async () => {
    const a = await registerOrg();
    const b = await registerOrg();
    // b's token only ever sees b's org; there is no cross-org member route, so
    // assert b's list is exactly b's members and excludes a's
    await addUser(a, "member");
    await addUser(b, "viewer");
    const response = await app.inject({
      method: "GET",
      url: "/api/v1/organizations/members",
      headers: { authorization: `Bearer ${b.accessToken}` },
    });
    const body = response.json() as {
      organization: { id: string; memberCount: number };
      items: Array<{ role: string }>;
    };
    expect(body.organization.id).toBe(b.orgId);
    expect(body.organization.memberCount).toBe(2);
    expect(body.items.map((m) => m.role).sort()).toEqual(["owner", "viewer"]);
  });
});

describe("PATCH /api/v1/organizations/members/:id/role", () => {
  itDb("owner promotes a member to admin", async () => {
    const org = await registerOrg();
    const admin = await addUser(org, "admin");
    const member = await addUser(org, "member");

    const response = await app.inject({
      method: "PATCH",
      url: `/api/v1/organizations/members/${member.id}/role`,
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload: { role: "admin" },
    });
    expect(response.statusCode).toBe(200);
    expect((response.json() as { role: string }).role).toBe("admin");
    // the original admin is untouched
    const { rows } = await pool.query(
      "SELECT role FROM users WHERE id = $1::uuid",
      [admin.id]
    );
    expect((rows[0] as { role: string }).role).toBe("admin");
  });

  itDb("forbids a member from granting a role above their own rank", async () => {
    const org = await registerOrg();
    const admin = await addUser(org, "admin");
    const member = await addUser(org, "member");

    const response = await app.inject({
      method: "PATCH",
      url: `/api/v1/organizations/members/${member.id}/role`,
      headers: { authorization: `Bearer ${admin.token}` },
      payload: { role: "owner" },
    });
    expect(response.statusCode).toBe(403);
    expect(response.json().error.code).toBe("FORBIDDEN");
  });

  itDb("forbids an admin from reassigning the owner", async () => {
    const org = await registerOrg();
    const admin = await addUser(org, "admin");

    const response = await app.inject({
      method: "PATCH",
      url: `/api/v1/organizations/members/${org.ownerId}/role`,
      headers: { authorization: `Bearer ${admin.token}` },
      payload: { role: "member" },
    });
    expect(response.statusCode).toBe(403);
    expect(response.json().error.code).toBe("FORBIDDEN");
  });

  itDb("rejects a viewer/member from the admin-gated route (403)", async () => {
    const org = await registerOrg();
    const member = await addUser(org, "member");
    const viewer = await addUser(org, "viewer");

    const response = await app.inject({
      method: "PATCH",
      url: `/api/v1/organizations/members/${viewer.id}/role`,
      headers: { authorization: `Bearer ${member.token}` },
      payload: { role: "viewer" },
    });
    expect(response.statusCode).toBe(403);
  });

  itDb("an unknown member id answers 404", async () => {
    const org = await registerOrg();
    const response = await app.inject({
      method: "PATCH",
      url: `/api/v1/organizations/members/${randomUUID()}/role`,
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload: { role: "member" },
    });
    expect(response.statusCode).toBe(404);
  });
});

describe("invitations", () => {
  itDb("owner creates an invitation with a role and it appears in the list", async () => {
    const org = await registerOrg();
    const inviteEmail = uniqueEmail();

    const create = await app.inject({
      method: "POST",
      url: "/api/v1/organizations/invitations",
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload: { email: inviteEmail, role: "admin" },
    });
    expect(create.statusCode).toBe(201);
    const created = create.json() as { id: string; email: string; role: string; status: string; token: string };
    expect(created.email).toBe(inviteEmail);
    expect(created.role).toBe("admin");
    expect(created.status).toBe("pending");
    expect(created.token).toMatch(/^[0-9a-f]{64}$/);

    const list = await app.inject({
      method: "GET",
      url: "/api/v1/organizations/invitations",
      headers: { authorization: `Bearer ${org.accessToken}` },
    });
    expect(list.statusCode).toBe(200);
    const body = list.json() as { organization: { id: string }; items: Array<{ id: string; email: string }> };
    expect(body.organization.id).toBe(org.orgId);
    expect(body.items.some((i) => i.id === created.id)).toBe(true);
  });

  itDb("rejects a second pending invite for the same email (409)", async () => {
    const org = await registerOrg();
    const inviteEmail = uniqueEmail();
    const payload = { email: inviteEmail, role: "member" };

    const first = await app.inject({
      method: "POST",
      url: "/api/v1/organizations/invitations",
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload,
    });
    expect(first.statusCode).toBe(201);

    const second = await app.inject({
      method: "POST",
      url: "/api/v1/organizations/invitations",
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload,
    });
    expect(second.statusCode).toBe(409);
    expect(second.json().error.code).toBe("INVITATION_EXISTS");
  });

  itDb("denies role-gated invitation endpoints to a plain member", async () => {
    const org = await registerOrg();
    const member = await addUser(org, "member");

    const create = await app.inject({
      method: "POST",
      url: "/api/v1/organizations/invitations",
      headers: { authorization: `Bearer ${member.token}` },
      payload: { email: uniqueEmail(), role: "member" },
    });
    expect(create.statusCode).toBe(403);
  });

  itDb("stores the token only sha256-hashed, never in clear", async () => {
    const org = await registerOrg();
    const create = await app.inject({
      method: "POST",
      url: "/api/v1/organizations/invitations",
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload: { email: uniqueEmail(), role: "viewer" },
    });
    const { id, token } = create.json() as { id: string; token: string };

    const { rows } = await pool.query(
      "SELECT token_hash FROM invitations WHERE id = $1::uuid",
      [id]
    );
    const stored = (rows[0] as { token_hash: string }).token_hash;
    expect(stored).toBe(hashToken(token));
    expect(stored).not.toBe(token);
  });

  itDb("revokes a pending invitation and refuses to revoke it twice", async () => {
    const org = await registerOrg();
    const create = await app.inject({
      method: "POST",
      url: "/api/v1/organizations/invitations",
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload: { email: uniqueEmail(), role: "member" },
    });
    const { id } = create.json() as { id: string };

    const revoke = await app.inject({
      method: "DELETE",
      url: `/api/v1/organizations/invitations/${id}`,
      headers: { authorization: `Bearer ${org.accessToken}` },
    });
    expect(revoke.statusCode).toBe(204);

    const revokeAgain = await app.inject({
      method: "DELETE",
      url: `/api/v1/organizations/invitations/${id}`,
      headers: { authorization: `Bearer ${org.accessToken}` },
    });
    expect(revokeAgain.statusCode).toBe(409);
  });
});

describe("POST /auth/invitations/accept (public)", () => {
  itDb("turns a valid invitation into a real membership in the inviting org", async () => {
    const org = await registerOrg();
    const inviteEmail = uniqueEmail();
    const create = await app.inject({
      method: "POST",
      url: "/api/v1/organizations/invitations",
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload: { email: inviteEmail, role: "admin" },
    });
    const { token } = create.json() as { token: string };

    const accept = await app.inject({
      method: "POST",
      url: "/auth/invitations/accept",
      payload: { token, password: "Sup3rSecret!" },
    });
    expect(accept.statusCode).toBe(201);
    const body = accept.json() as {
      invitation: { email: string; status: string };
      user: { email: string; role: string };
      organization: { id: string; memberCount: number };
    };
    expect(body.invitation.status).toBe("accepted");
    expect(body.user).toMatchObject({ email: inviteEmail, role: "admin" });
    expect(body.organization.id).toBe(org.orgId);
    expect(body.organization.memberCount).toBe(2);

    // the new member can log in and is scoped to the inviting org
    const login = await app.inject({
      method: "POST",
      url: "/auth/login",
      payload: { email: inviteEmail, password: "Sup3rSecret!" },
    });
    expect(login.statusCode).toBe(200);
    const loginBody = login.json() as { user: { organizationId: string; role: string } };
    expect(loginBody.user.organizationId).toBe(org.orgId);
    expect(loginBody.user.role).toBe("admin");
  });

  itDb("rejects a spent invitation (cannot double-accept)", async () => {
    const org = await registerOrg();
    const inviteEmail = uniqueEmail();
    const create = await app.inject({
      method: "POST",
      url: "/api/v1/organizations/invitations",
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload: { email: inviteEmail, role: "member" },
    });
    const { token } = create.json() as { token: string };

    const first = await app.inject({
      method: "POST",
      url: "/auth/invitations/accept",
      payload: { token, password: "Sup3rSecret!" },
    });
    expect(first.statusCode).toBe(201);

    const second = await app.inject({
      method: "POST",
      url: "/auth/invitations/accept",
      payload: { token, password: "Sup3rSecret!" },
    });
    expect(second.statusCode).toBe(409);
    expect(second.json().error.code).toBe("INVITATION_SPENT");
  });

  itDb("rejects an unknown token", async () => {
    const response = await app.inject({
      method: "POST",
      url: "/auth/invitations/accept",
      payload: { token: "f".repeat(64), password: "Sup3rSecret!" },
    });
    expect(response.statusCode).toBe(404);
    expect(response.json().error.code).toBe("INVITATION_NOT_FOUND");
  });

  itDb("rejects an expired invitation", async () => {
    const org = await registerOrg();
    // insert an already-expired invitation directly (its token is hashed)
    const rawToken = "e".repeat(64);
    await pool.query(
      `INSERT INTO invitations
         (organization_id, email, role, token_hash, status, expires_at, invited_by)
       VALUES ($1::uuid, $2, 'member', $3, 'pending', now() - interval '1 day', $4::uuid)`,
      [org.orgId, uniqueEmail(), hashToken(rawToken), org.ownerId]
    );
    const response = await app.inject({
      method: "POST",
      url: "/auth/invitations/accept",
      payload: { token: rawToken, password: "Sup3rSecret!" },
    });
    expect(response.statusCode).toBe(410);
    expect(response.json().error.code).toBe("INVITATION_EXPIRED");
  });

  itDb("rejects an email already taken in the org with 409 EMAIL_TAKEN", async () => {
    const org = await registerOrg();
    // the owner's own email already occupies (org, email)
    const { rows } = await pool.query(
      "SELECT email FROM users WHERE id = $1::uuid",
      [org.ownerId]
    );
    const ownerEmail = (rows[0] as { email: string }).email;

    const create = await app.inject({
      method: "POST",
      url: "/api/v1/organizations/invitations",
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload: { email: ownerEmail, role: "member" },
    });
    const { token } = create.json() as { token: string };

    const accept = await app.inject({
      method: "POST",
      url: "/auth/invitations/accept",
      payload: { token, password: "Sup3rSecret!" },
    });
    expect(accept.statusCode).toBe(409);
    expect(accept.json().error.code).toBe("EMAIL_TAKEN");
  });
});

describe("validation & auth for /api/v1/organizations", () => {
  itDb("rejects a malformed member id with 400", async () => {
    const org = await registerOrg();
    const response = await app.inject({
      method: "PATCH",
      url: "/api/v1/organizations/members/not-a-uuid/role",
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload: { role: "member" },
    });
    expect(response.statusCode).toBe(400);
  });

  itDb("rejects an invalid role value with 400", async () => {
    const org = await registerOrg();
    const response = await app.inject({
      method: "POST",
      url: "/api/v1/organizations/invitations",
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload: { email: uniqueEmail(), role: "superuser" },
    });
    expect(response.statusCode).toBe(400);
  });

  itDb("rejects an invalid email format with 400", async () => {
    const org = await registerOrg();
    const response = await app.inject({
      method: "POST",
      url: "/api/v1/organizations/invitations",
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload: { email: "not-an-email", role: "member" },
    });
    expect(response.statusCode).toBe(400);
  });

  itDb("requires a valid access token on every org route", async () => {
    for (const [method, url, payload] of [
      ["GET", "/api/v1/organizations", undefined],
      ["GET", "/api/v1/organizations/members", undefined],
      ["GET", "/api/v1/organizations/invitations", undefined],
      // a valid body so the request clears validation and reaches the auth
      // guard (an empty body would be rejected 400 before auth runs)
      ["POST", "/api/v1/organizations/invitations", { email: uniqueEmail(), role: "member" }],
    ] as const) {
      const response = await app.inject({ method, url, payload });
      expect(response.statusCode).toBe(401);
    }
  });
});
