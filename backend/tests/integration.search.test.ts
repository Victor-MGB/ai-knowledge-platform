import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { randomUUID } from "node:crypto";

import type { FastifyInstance } from "fastify";

import { createPool, type DatabaseClient } from "../src/database/postgres.js";
import { loadConfig } from "../src/config/config.js";
import { buildApp } from "../src/app.js";
import type { QueryEmbedder } from "../src/services/embedding-client.service.js";

/** Day 15 semantic retrieval against the real pgvector store with an injected
 * deterministic embedder (the AI service is exercised by the e2e harness, not
 * here). The corpus is seeded directly: upload a PDF via the API to get a real
 * document row, then write its chunks/vectors to prove search picks up exactly
 * what the store holds — tenant, model and retrievability gates included.
 * Skipped unless Postgres is reachable; orgs are cleaned up afterwards. */

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
      // unit-norm vector at cosine 0.6 / 0.8 to the two seeded axes below:
      // cos([.6,.8],[1,0]) = .6 -> similarity .6; cos([.6,.8],[0,1]) = .8 -> .8
      vector: [0.6, 0.8, ...new Array(382).fill(0)],
      model: MODEL,
    }),
  };
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
  return `srch-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
}

function multipart(
  filename: string,
  data: Buffer
): { payload: Buffer; headers: Record<string, string> } {
  const boundary = "----knowflow-search";
  const head = Buffer.from(
    `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${filename}"\r\nContent-Type: application/pdf\r\n\r\n`
  );
  const tail = Buffer.from(`\r\n--${boundary}--\r\n`);
  return {
    payload: Buffer.concat([head, data, tail]),
    headers: { "content-type": `multipart/form-data; boundary=${boundary}` },
  };
}

const PDF = Buffer.from("%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n");

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
    headers: { authorization: `Bearer ${token}`, ...multipart("guide.pdf", PDF).headers },
    payload: multipart("guide.pdf", PDF).payload,
  });
  expect(response.statusCode).toBe(201);
  const doc = response.json() as { id: string; organizationId: string };
  return { documentId: doc.id, orgId: doc.organizationId };
}

/** A 384-D pgvector literal with the given leading components, all else zero. */
function vec(components: number[]): string {
  const arr = new Array(384).fill(0);
  for (let i = 0; i < components.length; i++) arr[i] = components[i];
  return "[" + arr.join(",") + "]";
}

interface SeedChunk {
  content: string;
  page: number;
  model?: string;
  vector: number[];
  section?: string;
}

async function seedRetrievable(
  orgId: string,
  documentId: string,
  chunks: SeedChunk[]
): Promise<void> {
  await pool.query(
    `UPDATE documents SET status = 'ready', embedding_status = 'ready' WHERE id = $1`,
    [documentId]
  );
  for (let index = 0; index < chunks.length; index++) {
    const chunk = chunks[index]!;
    const chunkId = randomUUID();
    const model = chunk.model ?? MODEL;
    await pool.query(
      `INSERT INTO chunks (id, organization_id, document_id, content, chunk_index, page_number, token_count, metadata)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
      [
        chunkId,
        orgId,
        documentId,
        chunk.content,
        index,
        chunk.page,
        chunk.content.split(/\s+/).length,
        JSON.stringify({
          strategy: "paragraph",
          section: chunk.section ?? "policy",
          source: "s3://x",
          page_range: [chunk.page, chunk.page],
        }),
      ]
    );
    await pool.query(
      `INSERT INTO embeddings (organization_id, chunk_id, model, dimensions, embedding)
       VALUES ($1, $2, $3, 384, $4::vector)`,
      [orgId, chunkId, model, vec(chunk.vector)]
    );
  }
}

async function search(
  token: string,
  payload: Record<string, unknown>
): ReturnType<FastifyInstance["inject"]> {
  return app.inject({
    method: "POST",
    url: "/api/v1/search",
    headers: { authorization: `Bearer ${token}` },
    payload,
  });
}

describe("POST /api/v1/search", () => {
  itDb("returns top-K chunks ordered by cosine similarity, with the full result shape", async () => {
    const { accessToken, orgId } = await registerOrg();
    const { documentId } = await uploadPdf(accessToken);
    await seedRetrievable(orgId, documentId, [
      { content: "the refund policy is generous", page: 1, vector: [1, 0] },
      { content: "cancellations are handled quickly", page: 2, vector: [0, 1] },
    ]);

    const response = await search(accessToken, { query: "how fast are refunds" });
    expect(response.statusCode).toBe(200);
    const body = response.json() as {
      query: string;
      model: string;
      results: Array<{
        chunk: { id: string; chunkIndex: number; pageNumber: number; tokenCount: number; content: string };
        similarity: number;
        document: { id: string; title: string; filename: string; mimeType: string; sourceType: string };
        page: number;
        metadata: { strategy: string; section: string };
      }>;
    };
    expect(body.model).toBe(MODEL);
    expect(body.query).toBe("how fast are refunds");
    expect(body.results).toHaveLength(2);

    expect(body.results[0]!.similarity).toBeCloseTo(0.8, 6); // [0,1] axis first
    expect(body.results[0]!.chunk.content).toBe("cancellations are handled quickly");
    expect(body.results[0]!.page).toBe(2);
    expect(body.results[0]!.document.title).toBe("guide");
    expect(body.results[0]!.document.sourceType).toBe("pdf");
    expect(body.results[0]!.metadata.strategy).toBe("paragraph");

    expect(body.results[1]!.similarity).toBeCloseTo(0.6, 6);
    expect(body.results[1]!.chunk.content).toBe("the refund policy is generous");
  });

  itDb("honors limit: fewer rows, high-to-low similarity", async () => {
    const { accessToken, orgId } = await registerOrg();
    const { documentId } = await uploadPdf(accessToken);
    await seedRetrievable(orgId, documentId, [
      { content: "chunk one", page: 1, vector: [1, 0] },
      { content: "chunk two", page: 2, vector: [0, 1] },
    ]);

    const response = await search(accessToken, { query: "q", limit: 1 });
    expect(response.statusCode).toBe(200);
    const body = response.json() as { results: Array<{ chunk: { content: string } }> };
    expect(body.results).toHaveLength(1);
    expect(body.results[0]!.chunk.content).toBe("chunk two");
  });

  itDb("applies a similarity floor (minSimilarity) before returning results", async () => {
    const { accessToken, orgId } = await registerOrg();
    const { documentId } = await uploadPdf(accessToken);
    await seedRetrievable(orgId, documentId, [
      { content: "near match", page: 1, vector: [0.6, 0.8] },   // sim 1.0
      { content: "weak match", page: 2, vector: [1, 0] },       // cos([.6,.8],[1,0]) = .6
    ]);

    const floor = await search(accessToken, { query: "q", minSimilarity: 0.7 });
    expect(floor.statusCode).toBe(200);
    const body = floor.json() as { results: Array<{ chunk: { content: string } }> };
    expect(body.results.map((r) => r.chunk.content)).toEqual(["near match"]);

    // a perfect match (sim 1.0) still passes a strict floor; the weak one is gone
    const exact = await search(accessToken, { query: "q", minSimilarity: 0.9 });
    const strictBody = exact.json() as { results: Array<{ chunk: { content: string } }> };
    expect(strictBody.results.map((r) => r.chunk.content)).toEqual(["near match"]);
  });

  itDb("filters by chunk metadata and document source type before ranking", async () => {
    const { accessToken, orgId } = await registerOrg();
    const { documentId } = await uploadPdf(accessToken);
    await seedRetrievable(orgId, documentId, [
      { content: "return window text", page: 1, vector: [1, 0], section: "Return Window" },
      { content: "refund processing text", page: 2, vector: [0.3, 0.9], section: "Refund Processing" },
    ]);

    const sectionPin = await search(accessToken, {
      query: "q",
      metadata: { section: "Return Window" },
    });
    expect(sectionPin.statusCode).toBe(200);
    const sectionBody = sectionPin.json() as { results: Array<{ chunk: { content: string } }> };
    expect(sectionBody.results.map((r) => r.chunk.content)).toEqual(["return window text"]);

    // a bogus section cannot match anything -- the filter is not a scoring signal
    const missPin = await search(accessToken, {
      query: "q",
      metadata: { section: "does not exist" },
    });
    const missBody = missPin.json() as { results: Array<{ chunk: { content: string } }> };
    expect(missBody.results).toEqual([]);
  });

  itDb("applies the sourceType document gate", async () => {
    const { accessToken, orgId } = await registerOrg();
    const pdfDoc = await uploadPdf(accessToken);
    await seedRetrievable(orgId, pdfDoc.documentId, [
      { content: "a pdf source chunk", page: 1, vector: [1, 0] },
    ]);
    const mdDoc = await uploadPdf(accessToken);
    await seedRetrievable(orgId, mdDoc.documentId, [
      { content: "relabeled markdown chunk", page: 1, vector: [0.9, 0] },
    ]);
    // uploads are always PDFs right now; relabel the second to simulate a
    // future markdown ingest so the gate actually discriminates
    await pool.query("UPDATE documents SET source_type = 'md' WHERE id = $1", [mdDoc.documentId]);

    const onlyPdf = await search(accessToken, { query: "q", sourceType: "pdf" });
    const pdfBody = onlyPdf.json() as { results: Array<{ chunk: { content: string } }> };
    expect(pdfBody.results.map((r) => r.chunk.content)).toEqual(["a pdf source chunk"]);

    const onlyMd = await search(accessToken, { query: "q", sourceType: "md" });
    expect(onlyMd.statusCode).toBe(200);
    const mdBody = onlyMd.json() as { results: Array<{ chunk: { content: string } }> };
    expect(mdBody.results.map((r) => r.chunk.content)).toEqual(["relabeled markdown chunk"]);
  });

  itDb("rejects invalid knobs: bad sourceType, wrong-typed metadata, out-of-range threshold", async () => {
    const { accessToken } = await registerOrg();

    const badType = await search(accessToken, { query: "q", sourceType: "exe" });
    expect(badType.statusCode).toBe(400);

    const arrayValue = await search(accessToken, { query: "q", metadata: { section: ["a"] } });
    expect(arrayValue.statusCode).toBe(400);

    const aboveOne = await search(accessToken, { query: "q", minSimilarity: 1.5 });
    expect(aboveOne.statusCode).toBe(400);

    const negative = await search(accessToken, { query: "q", minSimilarity: -0.1 });
    expect(negative.statusCode).toBe(400);

    const limitPastMax = await search(accessToken, { query: "q", limit: 21 });
    expect(limitPastMax.statusCode).toBe(400);
  });

  itDb("never leaks another tenant's chunks", async () => {
    const { accessToken, orgId } = await registerOrg();
    const { documentId } = await uploadPdf(accessToken);
    await seedRetrievable(orgId, documentId, [
      { content: "mine", page: 1, vector: [1, 0] },
    ]);

    // a second org with a PERFECT match for the same query vector
    const foreign = await registerOrg();
    const foreignDoc = await uploadPdf(foreign.accessToken);
    await seedRetrievable(foreign.orgId, foreignDoc.documentId, [
      { content: "secret of another tenant", page: 1, vector: [0.6, 0.8] },
    ]);

    const response = await search(accessToken, { query: "q" });
    expect(response.statusCode).toBe(200);
    const body = response.json() as { results: Array<{ chunk: { content: string } }> };
    expect(body.results.map((r) => r.chunk.content)).toEqual(["mine"]);
  });

  itDb("searches only the query's model (index/knowledge gate)", async () => {
    const { accessToken, orgId } = await registerOrg();
    const { documentId } = await uploadPdf(accessToken);
    await seedRetrievable(orgId, documentId, [
      { content: "same bridge", page: 1, vector: [0.6, 0.8] },
      { content: "other model perfect match", page: 2, vector: [0.6, 0.8], model: "some-other-model" },
    ]);

    const response = await search(accessToken, { query: "q" });
    const body = response.json() as { results: Array<{ chunk: { content: string } }> };
    expect(body.results.map((r) => r.chunk.content)).toEqual(["same bridge"]);
  });

  itDb("serves only retrievable documents (status=ready + embedding_status=ready)", async () => {
    const { accessToken, orgId } = await registerOrg();
    const { documentId } = await uploadPdf(accessToken);
    await seedRetrievable(orgId, documentId, [
      { content: "retrievable", page: 1, vector: [0.6, 0.8] },
    ]);

    // a perfect-match chunk under a document whose vectors never finished
    const stalled = await registerOrg();
    const stalledDoc = await uploadPdf(stalled.accessToken);
    await seedRetrievable(stalled.orgId, stalledDoc.documentId, [
      { content: "not ready for search yet", page: 1, vector: [1, 0] },
    ]);
    await pool.query(
      `UPDATE documents SET embedding_status = 'failed', embedding_error = 'boom' WHERE id = $1`,
      [stalledDoc.documentId]
    );

    const response = await search(accessToken, { query: "q" });
    const body = response.json() as { results: Array<{ chunk: { content: string } }> };
    expect(body.results.map((r) => r.chunk.content)).toEqual(["retrievable"]);
  });

  itDb("validates: blank/absent query, bad limit and unknown keys are 400", async () => {
    const { accessToken } = await registerOrg();

    const blank = await search(accessToken, { query: "   " });
    expect(blank.statusCode).toBe(400);

    const missing = await search(accessToken, {});
    expect(missing.statusCode).toBe(400);

    const zero = await search(accessToken, { query: "q", limit: 0 });
    expect(zero.statusCode).toBe(400);

    const huge = await search(accessToken, { query: "q", limit: 100 });
    expect(huge.statusCode).toBe(400);

    const typo = await search(accessToken, { query: "q", limitt: 5 });
    expect(typo.statusCode).toBe(400);
  });

  itDb("requires a valid access token", async () => {
    const response = await app.inject({
      method: "POST",
      url: "/api/v1/search",
      payload: { query: "q" },
    });
    expect(response.statusCode).toBe(401);
  });
});