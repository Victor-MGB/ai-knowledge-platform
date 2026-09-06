import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { randomUUID } from "node:crypto";

import type { FastifyInstance } from "fastify";

import { createPool, type DatabaseClient } from "../src/database/postgres.js";
import { loadConfig } from "../src/config/config.js";
import { buildApp } from "../src/app.js";
import type { QueryEmbedder } from "../src/services/embedding-client.service.js";

/** Day 23 — per-user document privacy. Two users inside the SAME organization:
 * the owner uploads a document and a colleague (member) must be unable to
 * read, status, list, source, search, or delete it. This is the property the
 * whole day is about — org scoping is necessary but not sufficient; the
 * `uploaded_by` filter keeps every document private to the user who uploaded
 * it. Real pgvector + real postgres; orgs are cleaned up afterwards. */

const MODEL = "knowflow-hash-384";

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
  const embedder: QueryEmbedder = {
    embed: async () => ({
      vector: [1, 0, ...new Array(382).fill(0)],
      model: MODEL,
    }),
  };
  app = await buildApp({ embedder });
});
afterAll(async () => {
  for (const fn of cleanup) await fn();
  await pool.end();
  await app.close();
});

function uniqueEmail(): string {
  return `priv-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
}

function huge(name: string): { payload: Buffer; headers: Record<string, string> } {
  const boundary = "----knowflow-priv";
  const head = Buffer.from(
    `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${name}"\r\nContent-Type: application/pdf\r\n\r\n`
  );
  const tail = Buffer.from(`\r\n--${boundary}--\r\n`);
  const PDF = Buffer.from("%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n");
  return {
    payload: Buffer.concat([head, PDF, tail]),
    headers: { "content-type": `multipart/form-data; boundary=${boundary}` },
  };
}

async function registerOrg(): Promise<{ accessToken: string; orgId: string; userId: string }> {
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
    await pool.query("DELETE FROM organizations WHERE id = $1::uuid", [body.organization.id]);
  });
  return {
    accessToken: body.tokens.accessToken,
    orgId: body.organization.id,
    userId: body.user.id,
  };
}

/** A second user inside the same org (a member), minted directly so the test
 * needs no invite UI — the point is document ownership, not membership. */
async function colleagueToken(orgId: string): Promise<string> {
  const { rows } = await pool.query(
    "INSERT INTO users (organization_id, email, password_hash, role) VALUES ($1::uuid, $2, $3, 'member') RETURNING id",
    [orgId, `${uniqueEmail()}`, "irrelevant-hash"]
  );
  const colleagueId = (rows[0] as { id: string }).id;
  return app.jwt.sign({
    sub: colleagueId,
    org: orgId,
    role: "member",
    typ: "access",
    jti: randomUUID(),
  });
}

async function uploadDoc(token: string): Promise<{ id: string; orgId: string }> {
  const response = await app.inject({
    method: "POST",
    url: "/api/v1/documents",
    headers: { authorization: `Bearer ${token}`, ...huge("handbook.pdf").headers },
    payload: huge("handbook.pdf").payload,
  });
  expect(response.statusCode).toBe(201);
  const doc = response.json() as { id: string; organizationId: string };
  return { id: doc.id, orgId: doc.organizationId };
}

/** A 384-D pgvector literal with the given leading components, all else zero. */
function vec(components: number[]): string {
  const arr = new Array(384).fill(0);
  for (let i = 0; i < components.length; i++) arr[i] = components[i];
  return "[" + arr.join(",") + "]";
}

/** Mark the document retrievable and give it a PERFECT match for the embedder's
 * query vector — so the only reason a colleague misses it is ownership. */
async function seedRetrievable(orgId: string, documentId: string): Promise<void> {
  await pool.query(
    `UPDATE documents SET status = 'ready', embedding_status = 'ready' WHERE id = $1`,
    [documentId]
  );
  const chunkId = randomUUID();
  await pool.query(
    `INSERT INTO chunks (id, organization_id, document_id, content, chunk_index, page_number, token_count, metadata)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
    [
      chunkId,
      orgId,
      documentId,
      "secret business secret owned by user A",
      0,
      1,
      6,
      JSON.stringify({ strategy: "paragraph", section: "secret", source: "s3://x" }),
    ]
  );
  await pool.query(
    `INSERT INTO embeddings (organization_id, chunk_id, model, dimensions, embedding)
     VALUES ($1, $2, $3, 384, $4::vector)`,
    [orgId, chunkId, MODEL, vec([1, 0])]
  );
}

describe("Day 23 per-user document privacy", () => {
  itDb("a colleague in the same org cannot read, status, list, source, or search another user's document", async () => {
    const owner = await registerOrg();
    const colleague = await colleagueToken(owner.orgId);
    const { id } = await uploadDoc(owner.accessToken);
    await seedRetrievable(owner.orgId, id);

    // GET detail + status are 404 for the colleague
    const detail = await app.inject({
      method: "GET",
      url: `/api/v1/documents/${id}`,
      headers: { authorization: `Bearer ${colleague}` },
    });
    expect(detail.statusCode).toBe(404);

    const status = await app.inject({
      method: "GET",
      url: `/api/v1/documents/${id}/status`,
      headers: { authorization: `Bearer ${colleague}` },
    });
    expect(status.statusCode).toBe(404);

    // the source endpoint for a RAG citation's documentId is 404 for them too
    const source = await app.inject({
      method: "GET",
      url: `/api/v1/sources/${id}`,
      headers: { authorization: `Bearer ${colleague}` },
    });
    expect(source.statusCode).toBe(404);

    // listing does not surface the document
    const list = await app.inject({
      method: "GET",
      url: "/api/v1/documents",
      headers: { authorization: `Bearer ${colleague}` },
    });
    const listBody = list.json() as { items: Array<{ id: string }>; pagination: { total: number } };
    expect(listBody.items.map((d) => d.id)).not.toContain(id);
    expect(listBody.pagination.total).toBe(0);

    // search has a PERFECT match retrievable in the store, yet returns nothing
    // for a colleague whose query vector lands exactly on it
    const search = await app.inject({
      method: "POST",
      url: "/api/v1/search",
      headers: { authorization: `Bearer ${colleague}` },
      payload: { query: "secret business secret" },
    });
    expect(search.statusCode).toBe(200);
    const searchBody = search.json() as { results: Array<{ document: { id: string } }> };
    expect(searchBody.results).toEqual([]);

    // the owner still sees it all — proving the filter is per-user, not per-org
    const ownerDetail = await app.inject({
      method: "GET",
      url: `/api/v1/documents/${id}`,
      headers: { authorization: `Bearer ${owner.accessToken}` },
    });
    expect(ownerDetail.statusCode).toBe(200);
    const ownerList = await app.inject({
      method: "GET",
      url: "/api/v1/documents",
      headers: { authorization: `Bearer ${owner.accessToken}` },
    });
    const ownerListBody = ownerList.json() as { pagination: { total: number } };
    expect(ownerListBody.pagination.total).toBe(1);
  });

  itDb("DELETE stays owner-only AND ownership-scoped: a member of the same org gets 403", async () => {
    const owner = await registerOrg();
    const colleague = await colleagueToken(owner.orgId);
    await uploadDoc(owner.accessToken);

    // other tenant is 404 (existing posture)
    const stranger = await registerOrg();
    const doc = await app.inject({
      method: "POST",
      url: "/api/v1/documents",
      headers: { authorization: `Bearer ${owner.accessToken}`, ...huge("x.pdf").headers },
      payload: huge("x.pdf").payload,
    });
    const docId = (doc.json() as { id: string }).id;

    const asColleague = await app.inject({
      method: "DELETE",
      url: `/api/v1/documents/${docId}`,
      headers: { authorization: `Bearer ${colleague}` },
    });
    expect(asColleague.statusCode).toBe(403);

    const asStranger = await app.inject({
      method: "DELETE",
      url: `/api/v1/documents/${docId}`,
      headers: { authorization: `Bearer ${stranger.accessToken}` },
    });
    expect(asStranger.statusCode).toBe(404);
  });
});
