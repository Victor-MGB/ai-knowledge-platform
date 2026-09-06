import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { randomUUID } from "node:crypto";

import type { FastifyInstance } from "fastify";

import { createPool } from "../src/database/postgres.js";
import { loadConfig } from "../src/config/config.js";
import { buildApp } from "../src/app.js";
import { S3StorageClient } from "../src/services/storage.service.js";

/** Upload lifecycle against the real Day-8 MinIO container + Day-3 postgres.
 * Skipped unless both are reachable. Created orgs and their storage objects
 * are deleted afterwards (FK cascade removes the DB rows). */

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

async function storageIsReachable(): Promise<boolean> {
  const config = loadConfig();
  const storage = new S3StorageClient({
    endpoint: config.S3_ENDPOINT,
    region: config.S3_REGION,
    bucket: config.S3_BUCKET,
    accessKey: config.S3_ACCESS_KEY,
    secretKey: config.S3_SECRET_KEY,
    forcePathStyle: config.S3_FORCE_PATH_STYLE,
  });
  return (await storage.reachable()).reachable;
}

const reachable =
  (await databaseIsReachable()) && (await storageIsReachable());
const itDb = reachable ? it : it.skip;

let app: FastifyInstance;
const cleanup: Array<() => Promise<void>> = [];

beforeAll(async () => {
  app = await buildApp();
});
afterAll(async () => {
  for (const fn of cleanup) {
    await fn();
  }
  await app.close();
});

function uniqueEmail(): string {
  return `doc-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
}

function multipart(
  filename: string,
  data: Buffer,
  contentType = "application/octet-stream"
): { payload: Buffer; headers: Record<string, string> } {
  const boundary = "----knowflow-integration";
  const head = Buffer.from(
    `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${filename}"\r\nContent-Type: ${contentType}\r\n\r\n`
  );
  const tail = Buffer.from(`\r\n--${boundary}--\r\n`);
  return {
    payload: Buffer.concat([head, data, tail]),
    headers: { "content-type": `multipart/form-data; boundary=${boundary}` },
  };
}

const PDF = Buffer.from("%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n");
const DOCX = Buffer.concat([Buffer.from([0x50, 0x4b, 0x03, 0x04, 0x0a, 0x00]), Buffer.alloc(64)]);

async function registerOrg(): Promise<{ accessToken: string; orgId: string }> {
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
    tokens: { accessToken: string };
  };
  cleanup.push(async () => {
    const pool = createPool(loadConfig());
    try {
      await pool.query("DELETE FROM organizations WHERE id = $1::uuid", [
        body.organization.id,
      ]);
    } finally {
      await pool.end();
    }
  });
  return { accessToken: body.tokens.accessToken, orgId: body.organization.id };
}

itDb("uploads a pdf, stores the object in MinIO and lists it", async () => {
  const { accessToken, orgId } = await registerOrg();

  const upload = await app.inject({
    method: "POST",
    url: "/api/v1/documents",
    headers: { authorization: `Bearer ${accessToken}`, ...multipart("guide.pdf", PDF).headers },
    payload: multipart("guide.pdf", PDF).payload,
  });
  expect(upload.statusCode).toBe(201);
  const doc = upload.json() as {
    id: string;
    filename: string;
    mimeType: string;
    sourceType: string;
    status: string;
    storageKey: string;
    size: number;
    organizationId: string;
  };
  expect(doc.organizationId).toBe(orgId);
  expect(doc.filename).toBe("guide.pdf");
  expect(doc.mimeType).toBe("application/pdf");
  expect(doc.sourceType).toBe("pdf");
  expect(doc.status).toBe("queued");
  expect(doc.storageKey).toMatch(new RegExp(`^${orgId}/[0-9a-f-]{36}\\.pdf$`));

  // the bytes physically landed in MinIO under the returned key
  const config = loadConfig();
  const storage = new S3StorageClient({
    endpoint: config.S3_ENDPOINT,
    region: config.S3_REGION,
    bucket: config.S3_BUCKET,
    accessKey: config.S3_ACCESS_KEY,
    secretKey: config.S3_SECRET_KEY,
    forcePathStyle: config.S3_FORCE_PATH_STYLE,
  });
  const head = await storage.head(doc.storageKey);
  expect(head?.size).toBe(PDF.length);
  cleanup.push(() => storage.delete(doc.storageKey));

  const list = await app.inject({
    method: "GET",
    url: "/api/v1/documents",
    headers: { authorization: `Bearer ${accessToken}` },
  });
  expect(list.statusCode).toBe(200);
  const listBody = list.json() as {
    items: Array<{ id: string }>;
    pagination: { limit: number; offset: number; total: number; hasMore: boolean };
  };
  expect(listBody.items.some((d) => d.id === doc.id)).toBe(true);
  expect(listBody.pagination.total).toBeGreaterThan(0);
  expect(listBody.pagination.hasMore).toBe(false); // this org holds one doc
});

itDb("uploads a docx with its ooxml mime", async () => {
  const { accessToken } = await registerOrg();
  const upload = await app.inject({
    method: "POST",
    url: "/api/v1/documents",
    headers: { authorization: `Bearer ${accessToken}`, ...multipart("report.docx", DOCX).headers },
    payload: multipart("report.docx", DOCX).payload,
  });
  const config = loadConfig();
  const storage = new S3StorageClient({
    endpoint: config.S3_ENDPOINT,
    region: config.S3_REGION,
    bucket: config.S3_BUCKET,
    accessKey: config.S3_ACCESS_KEY,
    secretKey: config.S3_SECRET_KEY,
    forcePathStyle: config.S3_FORCE_PATH_STYLE,
  });
  expect(upload.statusCode).toBe(201);
  const body = upload.json() as { sourceType: string; mimeType: string; storageKey: string };
  expect(body.sourceType).toBe("docx");
  expect(body.mimeType).toContain("officedocument");
  cleanup.push(() => storage.delete(body.storageKey));
});

itDb("rejects a spoofed file: zip bytes named .pdf", async () => {
  const { accessToken } = await registerOrg();
  const upload = await app.inject({
    method: "POST",
    url: "/api/v1/documents",
    headers: { authorization: `Bearer ${accessToken}`, ...multipart("fake.pdf", DOCX).headers },
    payload: multipart("fake.pdf", DOCX).payload,
  });
  expect(upload.statusCode).toBe(400);
  expect(upload.json().error.code).toBe("FILE_TYPE_MISMATCH");
});

itDb("rejects an upload over the configured size limit with 413", async () => {
  const { accessToken } = await registerOrg();
  const smallLimitApp = await buildApp({
    config: loadConfig({ MAX_UPLOAD_BYTES: "2048" }),
  });
  try {
    const upload = await smallLimitApp.inject({
      method: "POST",
      url: "/api/v1/documents",
      headers: {
        authorization: `Bearer ${accessToken}`, // reuse a token: issuer+secret match
        ...multipart("big.pdf", Buffer.alloc(4096, 0x25)).headers,
      },
      payload: multipart("big.pdf", Buffer.alloc(4096, 0x25)).payload,
    });
    expect(upload.statusCode).toBe(413);
    expect(upload.json().error.code).toBe("FILE_TOO_LARGE");
  } finally {
    await smallLimitApp.close();
  }
});

itDb("rejects uploads without a token", async () => {
  const upload = await app.inject({
    method: "POST",
    url: "/api/v1/documents",
    headers: multipart("guide.pdf", PDF).headers,
    payload: multipart("guide.pdf", PDF).payload,
  });
  expect(upload.statusCode).toBe(401);
});

itDb("rejects a multipart request with no file part", async () => {
  const { accessToken } = await registerOrg();
  const boundary = "----knowflow-integration";
  const upload = await app.inject({
    method: "POST",
    url: "/api/v1/documents",
    headers: {
      authorization: `Bearer ${accessToken}`,
      "content-type": `multipart/form-data; boundary=${boundary}`,
    },
    payload: Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="note"\r\n\r\nhi!\r\n--${boundary}--\r\n`),
  });
  expect(upload.statusCode).toBe(400);
  expect(upload.json().error.code).toBe("FILE_REQUIRED");
});

// ---- Day 13: document management (get / delete / status / list pagination) ----

interface ManagedDoc {
  id: string;
  storageKey: string;
  status: string;
  embeddingStatus: string;
  filename: string;
  sourceType: string;
}

async function uploadDoc(accessToken: string, filename = "guide.pdf"): Promise<ManagedDoc> {
  const data = filename.endsWith(".pdf") ? PDF : Buffer.from("# hello");
  const upload = await app.inject({
    method: "POST",
    url: "/api/v1/documents",
    headers: { authorization: `Bearer ${accessToken}`, ...multipart(filename, data).headers },
    payload: multipart(filename, data).payload,
  });
  expect(upload.statusCode).toBe(201);
  return upload.json() as ManagedDoc;
}

async function memberToken(orgId: string): Promise<string> {
  // a member of the same org, minted directly so the test needs no invite UI
  const pool = createPool(loadConfig());
  let memberId: string;
  try {
    const { rows } = await pool.query(
      "INSERT INTO users (organization_id, email, password_hash, role) VALUES ($1::uuid, $2, $3, 'member') RETURNING id",
      [orgId, `member-${randomUUID()}@example.com`, "irrelevant-hash"]
    );
    memberId = (rows[0] as { id: string }).id;
  } finally {
    await pool.end();
  }
  return app.jwt.sign({
    sub: memberId,
    org: orgId,
    role: "member",
    typ: "access",
    jti: randomUUID(),
  });
}

itDb("GET /api/v1/documents/:id returns the detail view including embedding lifecycle", async () => {
  const { accessToken } = await registerOrg();
  const uploaded = await uploadDoc(accessToken);

  const response = await app.inject({
    method: "GET",
    url: `/api/v1/documents/${uploaded.id}`,
    headers: { authorization: `Bearer ${accessToken}` },
  });
  expect(response.statusCode).toBe(200);
  const body = response.json() as ManagedDoc & { size: number; error: unknown };
  expect(body.id).toBe(uploaded.id);
  expect(body.filename).toBe("guide.pdf");
  expect(body.sourceType).toBe("pdf");
  expect(body.status).toBe("queued");
  expect(body.embeddingStatus).toBe("none");
  expect(body.error).toBeNull();
});

itDb("GET /api/v1/documents/:id hides other tenants' documents (404)", async () => {
  const ownerA = await registerOrg();
  const ownerB = await registerOrg();
  const uploaded = await uploadDoc(ownerA.accessToken);

  const response = await app.inject({
    method: "GET",
    url: `/api/v1/documents/${uploaded.id}`,
    headers: { authorization: `Bearer ${ownerB.accessToken}` },
  });
  expect(response.statusCode).toBe(404);
  expect(response.json().error.code).toBe("NOT_FOUND");
});

itDb("GET /api/v1/documents/:id rejects a malformed id with 400", async () => {
  const { accessToken } = await registerOrg();
  const response = await app.inject({
    method: "GET",
    url: "/api/v1/documents/not-a-uuid",
    headers: { authorization: `Bearer ${accessToken}` },
  });
  expect(response.statusCode).toBe(400);
  expect(response.json().error.code).toBe("VALIDATION_ERROR");
});

itDb("GET /api/v1/documents/:id/status reports lifecycle, stage and counts", async () => {
  const { accessToken } = await registerOrg();
  const uploaded = await uploadDoc(accessToken);

  const response = await app.inject({
    method: "GET",
    url: `/api/v1/documents/${uploaded.id}/status`,
    headers: { authorization: `Bearer ${accessToken}` },
  });
  expect(response.statusCode).toBe(200);
  const body = response.json() as {
    status: string;
    embeddingStatus: string;
    stage: string;
    counts: { pages: number; chunks: number; embeddings: number };
  };
  expect(body.status).toBe("queued");
  expect(body.embeddingStatus).toBe("none");
  expect(body.stage).toBe("uploaded");
  expect(body.counts).toEqual({ pages: 0, chunks: 0, embeddings: 0 });
});

itDb("DELETE /api/v1/documents/:id removes the row and the stored object", async () => {
  const { accessToken } = await registerOrg();
  const uploaded = await uploadDoc(accessToken);

  const config = loadConfig();
  const storage = new S3StorageClient({
    endpoint: config.S3_ENDPOINT,
    region: config.S3_REGION,
    bucket: config.S3_BUCKET,
    accessKey: config.S3_ACCESS_KEY,
    secretKey: config.S3_SECRET_KEY,
    forcePathStyle: config.S3_FORCE_PATH_STYLE,
  });
  expect(await storage.head(uploaded.storageKey)).toBeDefined();

  const deleted = await app.inject({
    method: "DELETE",
    url: `/api/v1/documents/${uploaded.id}`,
    headers: { authorization: `Bearer ${accessToken}` },
  });
  expect(deleted.statusCode).toBe(204);

  // object bytes must not survive the document
  expect(await storage.head(uploaded.storageKey)).toBeUndefined();
  const gone = await app.inject({
    method: "GET",
    url: `/api/v1/documents/${uploaded.id}`,
    headers: { authorization: `Bearer ${accessToken}` },
  });
  expect(gone.statusCode).toBe(404);
});

itDb("DELETE blocks members (403) and other tenants (404)", async () => {
  const ownerA = await registerOrg();
  const ownerB = await registerOrg();
  const uploaded = await uploadDoc(ownerA.accessToken);

  const memberA = await memberToken(ownerA.orgId);
  const asMember = await app.inject({
    method: "DELETE",
    url: `/api/v1/documents/${uploaded.id}`,
    headers: { authorization: `Bearer ${memberA}` },
  });
  expect(asMember.statusCode).toBe(403);
  expect(asMember.json().error.code).toBe("FORBIDDEN");

  const otherTenant = await app.inject({
    method: "DELETE",
    url: `/api/v1/documents/${uploaded.id}`,
    headers: { authorization: `Bearer ${ownerB.accessToken}` },
  });
  expect(otherTenant.statusCode).toBe(404);
});

itDb("list paginates and filters (limit, sourceType, status, search)", async () => {
  const { accessToken } = await registerOrg();
  const pdf = await uploadDoc(accessToken, "guide.pdf");
  const mdNote = await uploadDoc(accessToken, "notes.md");

  const page = await app.inject({
    method: "GET",
    url: "/api/v1/documents?limit=1&offset=0",
    headers: { authorization: `Bearer ${accessToken}` },
  });
  const pageBody = page.json() as {
    items: Array<{ id: string }>;
    pagination: { total: number; hasMore: boolean; limit: number; offset: number };
  };
  expect(pageBody.items).toHaveLength(1);
  expect(pageBody.pagination.total).toBe(2);
  expect(pageBody.pagination.hasMore).toBe(true);

  const byType = await app.inject({
    method: "GET",
    url: "/api/v1/documents?sourceType=md",
    headers: { authorization: `Bearer ${accessToken}` },
  });
  const typeBody = byType.json() as { items: Array<{ id: string; sourceType: string }> };
  expect(typeBody.items.map((d) => d.id)).toEqual([mdNote.id]);
  expect(typeBody.items[0]?.sourceType).toBe("md");

  const byStatus = await app.inject({
    method: "GET",
    url: "/api/v1/documents?status=queued",
    headers: { authorization: `Bearer ${accessToken}` },
  });
  const statusBody = byStatus.json() as { items: Array<{ id: string }> };
  expect(statusBody.items.map((d) => d.id).sort()).toEqual([pdf.id, mdNote.id].sort());

  const search = await app.inject({
    method: "GET",
    url: "/api/v1/documents?search=guide",
    headers: { authorization: `Bearer ${accessToken}` },
  });
  const searchBody = search.json() as { items: Array<{ id: string }> };
  expect(searchBody.items.map((d) => d.id)).toEqual([pdf.id]);
});

itDb("list rejects invalid filters and unknown query keys with 400", async () => {
  const { accessToken } = await registerOrg();
  for (const query of [
    "sourceType=exe",
    "status=exploding",
    "limit=0",
    "limit=1000",
    "offset=-1",
    "bogus=1",
  ]) {
    const response = await app.inject({
      method: "GET",
      url: `/api/v1/documents?${query}`,
      headers: { authorization: `Bearer ${accessToken}` },
    });
    expect(response.statusCode).toBe(400);
    expect(response.json().error.code).toBe("VALIDATION_ERROR");
  }
});