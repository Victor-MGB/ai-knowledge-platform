import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { randomUUID } from "node:crypto";

import type { FastifyInstance } from "fastify";

import { createPool, type DatabaseClient } from "../src/database/postgres.js";
import { loadConfig } from "../src/config/config.js";
import { buildApp } from "../src/app.js";

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
  // The app builds (and on close closes) its own DB pool; `pool` here is a
  // separate connection used only for test cleanup, so the two never fight
  // over "end on pool more than once".
  pool = createPool(loadConfig());
  app = await buildApp();
});
afterAll(async () => {
  for (const fn of cleanup) await fn();
  await app.close();
  await pool.end();
});

function uniqueEmail(): string {
  return `src-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
}

const PDF = Buffer.from("%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n");

function multipart(
  filename: string,
  data: Buffer
): { payload: Buffer; headers: Record<string, string> } {
  const boundary = "----knowflow-src";
  const head = Buffer.from(
    `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${filename}"\r\nContent-Type: application/pdf\r\n\r\n`
  );
  const tail = Buffer.from(`\r\n--${boundary}--\r\n`);
  return {
    payload: Buffer.concat([head, data, tail]),
    headers: { "content-type": `multipart/form-data; boundary=${boundary}` },
  };
}

async function registerOrg(): Promise<{ accessToken: string; orgId: string }> {
  const email = uniqueEmail();
  const response = await app.inject({
    method: "POST",
    url: "/auth/register",
    payload: { email, password: "Sup3rSecret!" },
  });
  expect(response.statusCode).toBe(201);
  const body = response.json() as {
    organization: { id: string };
    tokens: { accessToken: string };
  };
  cleanup.push(async () => {
    await pool.query("DELETE FROM organizations WHERE id = $1::uuid", [body.organization.id]);
  });
  return { accessToken: body.tokens.accessToken, orgId: body.organization.id };
}

async function uploadPdf(token: string): Promise<{ documentId: string; orgId: string }> {
  const response = await app.inject({
    method: "POST",
    url: "/api/v1/documents",
    headers: { authorization: `Bearer ${token}`, ...multipart("handbook.pdf", PDF).headers },
    payload: multipart("handbook.pdf", PDF).payload,
  });
  expect(response.statusCode).toBe(201);
  const doc = response.json() as { id: string; organizationId: string };
  return { documentId: doc.id, orgId: doc.organizationId };
}

async function seedPage(
  orgId: string,
  documentId: string,
  pageNumber: number
): Promise<void> {
  await pool.query(
    `UPDATE documents SET status = 'ready', embedding_status = 'ready' WHERE id = $1`,
    [documentId]
  );
  await pool.query(
    `INSERT INTO document_pages (document_id, organization_id, page_number, content, token_count, section, source)
     VALUES ($1, $2, $3, $4, $5, $6, $7)`,
    [
      documentId,
      orgId,
      pageNumber,
      "Annual leave is 20 working days.",
      pageNumber * 10,
      "Leave",
      "s3://x",
    ]
  );
}

describe("GET /api/v1/sources/:documentId", () => {
  itDb("resolves a RAG citation's documentId back to its source metadata and page count", async () => {
    const { accessToken, orgId } = await registerOrg();
    const { documentId } = await uploadPdf(accessToken);
    await seedPage(orgId, documentId, 14);

    const response = await app.inject({
      method: "GET",
      url: `/api/v1/sources/${documentId}`,
      headers: { authorization: `Bearer ${accessToken}` },
    });
    expect(response.statusCode).toBe(200);
    const body = response.json() as {
      document: { id: string; title: string; filename: string; sourceType: string; size: number };
      pages: number;
    };
    expect(body.document.id).toBe(documentId);
    expect(body.document.title).toBe("handbook");
    expect(body.document.filename).toBe("handbook.pdf");
    expect(body.document.sourceType).toBe("pdf");
    expect(body.pages).toBe(1);
  });

  itDb("returns id, title, filename, sourceType, size, and createdAt", async () => {
    const { accessToken, orgId } = await registerOrg();
    const { documentId } = await uploadPdf(accessToken);
    await seedPage(orgId, documentId, 1);

    const response = await app.inject({
      method: "GET",
      url: `/api/v1/sources/${documentId}`,
      headers: { authorization: `Bearer ${accessToken}` },
    });
    const body = response.json() as { document: Record<string, unknown> };
    expect(body.document).toHaveProperty("id");
    expect(body.document).toHaveProperty("title");
    expect(body.document).toHaveProperty("filename");
    expect(body.document).toHaveProperty("sourceType");
    expect(body.document).toHaveProperty("size");
    expect(body.document).toHaveProperty("createdAt");
  });

  itDb("hides other tenants' source documents (404)", async () => {
    const owner = await registerOrg();
    const stranger = await registerOrg();
    const { documentId } = await uploadPdf(owner.accessToken);

    const response = await app.inject({
      method: "GET",
      url: `/api/v1/sources/${documentId}`,
      headers: { authorization: `Bearer ${stranger.accessToken}` },
    });
    expect(response.statusCode).toBe(404);
    const body = response.json() as { error: { code: string } };
    expect(body.error.code).toBe("NOT_FOUND");
  });

  itDb("rejects a malformed id with 400", async () => {
    const { accessToken } = await registerOrg();
    const response = await app.inject({
      method: "GET",
      url: "/api/v1/sources/not-a-uuid",
      headers: { authorization: `Bearer ${accessToken}` },
    });
    expect(response.statusCode).toBe(400);
  });

  itDb("requires a valid access token", async () => {
    const response = await app.inject({
      method: "GET",
      url: `/api/v1/sources/${randomUUID()}`,
    });
    expect(response.statusCode).toBe(401);
  });
});
