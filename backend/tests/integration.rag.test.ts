import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { randomUUID } from "node:crypto";

import type { FastifyInstance } from "fastify";

import { createPool, type DatabaseClient } from "../src/database/postgres.js";
import { loadConfig } from "../src/config/config.js";
import { buildApp } from "../src/app.js";
import type { QueryEmbedder } from "../src/services/embedding-client.service.js";
import type {
  RagGenerator,
  RagGenerateInput,
  RagGeneration,
} from "../src/services/rag-generator-client.service.js";

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

const capturedInputs: RagGenerateInput[] = [];

function captureGenerator(): RagGenerator {
  const generate = vi.fn(
    async (input: RagGenerateInput): Promise<RagGeneration> => {
      capturedInputs.push(input);
      const refused = !input.context.length;
      return {
        answer: refused ? "I don't know." : "FAKE-ANSWER-refunds fast",
        refused,
        provider: "test-extract",
        model: "test-model",
        evidence: input.context.map((c, i) => ({
          index: i,
          chunkId: c.chunkId ?? null,
          documentId: c.documentId ?? null,
          documentTitle: c.documentTitle ?? null,
          section: c.section ?? null,
          page: c.page ?? null,
          similarity: c.similarity ?? null,
        })),
        citations: input.context.map((c, i) => ({
          id: i + 1,
          title: c.documentTitle ?? null,
          section: c.section ?? null,
          page: c.page ?? null,
          chunkId: c.chunkId ?? null,
          documentId: c.documentId ?? null,
          similarity: c.similarity ?? null,
        })),
        usage: { promptTokens: 10, completionTokens: 5, totalTokens: 15 },
      };
    }
  );
  const generateStream = vi.fn(
    async function* (input: RagGenerateInput) {
      capturedInputs.push(input);
      const refused = !input.context.length;
      const answer = refused ? "I don't know." : "FAKE-ANSWER-refunds fast";
      if (!refused) {
        yield { type: "delta", text: "FAKE-" };
        yield { type: "delta", text: "ANSWER" };
      }
      yield {
        type: "done",
        generation: {
          answer,
          refused,
          provider: "test-extract",
          model: "test-model",
          evidence: input.context.map((c, i) => ({
            index: i,
            chunkId: c.chunkId ?? null,
            documentId: c.documentId ?? null,
            documentTitle: c.documentTitle ?? null,
            section: c.section ?? null,
            page: c.page ?? null,
            similarity: c.similarity ?? null,
          })),
          citations: input.context.map((c, i) => ({
            id: i + 1,
            title: c.documentTitle ?? null,
            section: c.section ?? null,
            page: c.page ?? null,
            chunkId: c.chunkId ?? null,
            documentId: c.documentId ?? null,
            similarity: c.similarity ?? null,
          })),
          usage: { promptTokens: 10, completionTokens: 5, totalTokens: 15 },
        },
      };
    }
  );
  return { generate, generateStream } as unknown as RagGenerator;
}

beforeAll(async () => {
  pool = createPool(loadConfig());
  const embedder: QueryEmbedder = {
    embed: async () => ({
      vector: [0.6, 0.8, ...new Array(382).fill(0)],
      model: MODEL,
    }),
  };
  app = await buildApp({ embedder, ragGenerator: captureGenerator() });
});
afterAll(async () => {
  for (const fn of cleanup) await fn();
  await pool.end();
  await app.close();
});

function uniqueEmail(): string {
  return `rag-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
}

const PDF = Buffer.from("%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n");

function multipart(
  filename: string,
  data: Buffer
): { payload: Buffer; headers: Record<string, string> } {
  const boundary = "----knowflow-rag";
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
    headers: { authorization: `Bearer ${token}`, ...multipart("guide.pdf", PDF).headers },
    payload: multipart("guide.pdf", PDF).payload,
  });
  expect(response.statusCode).toBe(201);
  const doc = response.json() as { id: string; organizationId: string };
  return { documentId: doc.id, orgId: doc.organizationId };
}

function vec(components: number[]): string {
  const arr = new Array(384).fill(0);
  for (let i = 0; i < components.length; i++) arr[i] = components[i];
  return "[" + arr.join(",") + "]";
}

async function seedRetrievable(
  orgId: string,
  documentId: string,
  chunks: Array<{ content: string; page: number; vector: number[]; section?: string }>
): Promise<void> {
  await pool.query(
    `UPDATE documents SET status = 'ready', embedding_status = 'ready' WHERE id = $1`,
    [documentId]
  );
  for (let index = 0; index < chunks.length; index++) {
    const chunk = chunks[index]!;
    const chunkId = randomUUID();
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
      [orgId, chunkId, MODEL, vec(chunk.vector)]
    );
  }
}

async function ragGenerate(
  token: string,
  payload: Record<string, unknown>
): ReturnType<FastifyInstance["inject"]> {
  return app.inject({
    method: "POST",
    url: "/api/v1/rag/generate",
    headers: { authorization: `Bearer ${token}` },
    payload,
  });
}

describe("POST /api/v1/rag/generate", () => {
  itDb("retrieves context, budgets it best-first, and returns the generated answer with evidence", async () => {
    capturedInputs.length = 0;
    const { accessToken, orgId } = await registerOrg();
    const { documentId } = await uploadPdf(accessToken);
    await seedRetrievable(orgId, documentId, [
      { content: "the refund policy is generous", page: 1, vector: [1, 0], section: "policy" },
      { content: "cancellations are handled quickly", page: 2, vector: [0, 1], section: "policy" },
    ]);

    const response = await ragGenerate(accessToken, { question: "how fast are refunds" });
    expect(response.statusCode).toBe(200);
    const body = response.json() as {
      answer: string;
      refused: boolean;
      provider: string;
      model: string;
      retrieval: { query: string; model: string; retrieved: number };
      evidence: Array<{ index: number; similarity: number | null; chunkId: string | null }>;
      citations: Array<{
        id: number;
        title: string | null;
        section: string | null;
        page: number | null;
        documentId: string | null;
      }>;
      usage: { promptTokens: number; completionTokens: number; totalTokens: number };
    };

    expect(body.refused).toBe(false);
    expect(body.answer).toBe("FAKE-ANSWER-refunds fast");
    expect(body.provider).toBe("test-extract");
    expect(body.model).toBe("test-model");
    expect(body.retrieval.model).toBe(MODEL);
    expect(body.retrieval.retrieved).toBe(2);
    // evidence[0] = best similarity item (0.8 for [0,1] axis)
    expect(body.evidence[0]!.similarity).toBeCloseTo(0.8, 6);
    // Day 18: citations carry the source's title/section/page with their
    // [id] marker, and the documentId feeds the Source API.
    expect(body.citations.map((c) => c.id)).toEqual([1, 2]);
    expect(body.citations[0]!.title).toBe("guide");
    expect(body.citations[0]!.page).toBe(2);
    expect(body.citations[0]!.section).toBe("policy");
    expect(body.citations[0]!.documentId).toBe(documentId);

    // generator received best-first context: first item = 0.8 sim
    expect(capturedInputs.length).toBe(1);
    const input = capturedInputs[0]!;
    expect(input.context[0]!.similarity).toBeCloseTo(0.8, 6);
    expect(input.context[0]!.section).toBe("policy");
    expect(input.context[1]!.similarity).toBeCloseTo(0.6, 6);
  });

  itDb("forwards an empty context and the generator's refusal when nothing retrieves", async () => {
    capturedInputs.length = 0;
    const { accessToken } = await registerOrg();

    const response = await ragGenerate(accessToken, { question: "q" });
    expect(response.statusCode).toBe(200);
    const body = response.json() as { refused: boolean; answer: string; retrieval: { retrieved: number } };
    expect(body.refused).toBe(true);
    expect(body.answer).toBe("I don't know.");
    expect(body.retrieval.retrieved).toBe(0);
    expect(capturedInputs[0]!.context).toEqual([]);
  });

  itDb("validates knobs: bad maxContextTokens, minScore, typo, blank question, limit", async () => {
    const { accessToken } = await registerOrg();
    expect((await ragGenerate(accessToken, { question: "q", maxContextTokens: 10 })).statusCode).toBe(400);
    expect((await ragGenerate(accessToken, { question: "q", minScore: 1.5 })).statusCode).toBe(400);
    expect((await ragGenerate(accessToken, { question: "q", answerz: 1 })).statusCode).toBe(400);
    expect((await ragGenerate(accessToken, { question: "   " })).statusCode).toBe(400);
    expect((await ragGenerate(accessToken, { question: "q", limit: 21 })).statusCode).toBe(400);
  });

  itDb("requires a valid access token", async () => {
    const response = await app.inject({
      method: "POST",
      url: "/api/v1/rag/generate",
      payload: { question: "q" },
    });
    expect(response.statusCode).toBe(401);
  });

  itDb("is a 503 RAG_GENERATION_UNAVAILABLE when the generator is down", async () => {
    // build a fresh app with its own pool so closing it doesn't kill the shared pool
    const failPool = createPool(loadConfig());
    const failingApp = await buildApp({
      pool: failPool,
      embedder: { embed: async () => ({ vector: [0.6, 0.8, ...new Array(382).fill(0)], model: MODEL }) },
      ragGenerator: {
        generate: vi.fn(async () => {
          throw new Error("connection refused");
        }),
      } as unknown as RagGenerator,
    });
    const { accessToken, orgId } = await registerOrgWith(failingApp);
    try {
      const response = await failingApp.inject({
        method: "POST",
        url: "/api/v1/rag/generate",
        headers: { authorization: `Bearer ${accessToken}` },
        payload: { question: "q" },
      });
      expect(response.statusCode).toBe(503);
      const body = response.json() as { error: { code: string } };
      expect(body.error.code).toBe("RAG_GENERATION_UNAVAILABLE");
    } finally {
      // the failing app owners its own pool, so its cleanup must run against
      // that pool BEFORE close() ends it — otherwise every run leaks an org
      await failPool.query("DELETE FROM organizations WHERE id = $1::uuid", [orgId]);
      await failingApp.close();
    }
  });
});

async function registerOrgWith(target: FastifyInstance): Promise<{ accessToken: string; orgId: string }> {
  const email = `rag-fail-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
  const response = await target.inject({
    method: "POST",
    url: "/auth/register",
    payload: { email, password: "Sup3rSecret!" },
  });
  expect(response.statusCode).toBe(201);
  const body = response.json() as {
    organization: { id: string };
    tokens: { accessToken: string };
  };
  return { accessToken: body.tokens.accessToken, orgId: body.organization.id };
}

describe("POST /api/v1/rag/generate/stream", () => {
  itDb("streams answer deltas then a final done event with citations", async () => {
    capturedInputs.length = 0;
    const { accessToken, orgId } = await registerOrg();
    const { documentId } = await uploadPdf(accessToken);
    await seedRetrievable(orgId, documentId, [
      { content: "the refund policy is generous", page: 1, vector: [1, 0], section: "policy" },
    ]);

    const response = await app.inject({
      method: "POST",
      url: "/api/v1/rag/generate/stream",
      headers: { authorization: `Bearer ${accessToken}` },
      payload: { question: "how fast are refunds" },
    });

    expect(response.statusCode).toBe(200);
    expect(response.headers["content-type"]).toContain("text/event-stream");

    const body = response.body as string;

    // split into SSE events: "data: <json>" blocks separated by blank lines
    const events: unknown[] = [];
    for (const line of body.split("\n")) {
      if (line.startsWith("data: ")) {
        const payload = line.slice("data: ".length).trim();
        if (payload === "[DONE]") continue;
        events.push(JSON.parse(payload));
      }
    }

    const deltas = events
      .filter((e): e is { delta: string } => typeof (e as { delta?: string }).delta === "string")
      .map((e) => (e as { delta: string }).delta)
      .join("");
    expect(deltas).toBe("FAKE-ANSWER");

    const done = events.find(
      (e): e is { done: Record<string, unknown> } =>
        (e as { done?: Record<string, unknown> }).done !== undefined
    )?.done;
    expect(done).toBeTruthy();
    expect((done as { answer: string }).answer).toBe("FAKE-ANSWER-refunds fast");
    expect((done as { refused: boolean }).refused).toBe(false);
    const citations = (done as { citations: Array<{ id: number; documentId: string | null }> }).citations;
    expect(citations[0]!.id).toBe(1);
    expect(citations[0]!.documentId).toBe(documentId);
  });

  itDb("refuses (single done event, no deltas) when nothing retrieves", async () => {
    const { accessToken } = await registerOrg();
    const response = await app.inject({
      method: "POST",
      url: "/api/v1/rag/generate/stream",
      headers: { authorization: `Bearer ${accessToken}` },
      payload: { question: "q" },
    });
    expect(response.statusCode).toBe(200);
    expect(response.body as string).not.toContain('"delta"');
    expect(response.body as string).toContain('"done"');
    expect(response.body as string).toContain('"refused":true');
  });

  itDb("validates the body identically to /rag/generate", async () => {
    const { accessToken } = await registerOrg();
    const response = await app.inject({
      method: "POST",
      url: "/api/v1/rag/generate/stream",
      headers: { authorization: `Bearer ${accessToken}` },
      payload: { question: "q", maxContextTokens: 10 },
    });
    expect(response.statusCode).toBe(400);
  });
});
