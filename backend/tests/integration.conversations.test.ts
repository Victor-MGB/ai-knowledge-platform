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

function makeGenerator(): RagGenerator {
  return {
    generate: vi.fn(async (input: RagGenerateInput): Promise<RagGeneration> => {
      capturedInputs.push(input);
      return {
        answer: "Answer: annual leave is 20 working days [1]",
        refused: false,
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
    }),
  } as unknown as RagGenerator;
}

beforeAll(async () => {
  pool = createPool(loadConfig());
  const embedder: QueryEmbedder = {
    embed: async () => ({ vector: [0.6, 0.8, ...new Array(382).fill(0)], model: MODEL }),
  };
  app = await buildApp({ embedder, ragGenerator: makeGenerator() });
});
afterAll(async () => {
  for (const fn of cleanup) await fn();
  await pool.end();
  await app.close();
});

function uniqueEmail(): string {
  return `conv-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
}

async function registerOrg(): Promise<{
  accessToken: string;
  orgId: string;
  userId: string;
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
    tokens: { accessToken: string };
  };
  cleanup.push(async () => {
    await pool.query("DELETE FROM organizations WHERE id = $1::uuid", [
      body.organization.id,
    ]);
  });
  return {
    accessToken: body.tokens.accessToken,
    orgId: body.organization.id,
    userId: body.user.id,
  };
}

async function createConversation(
  token: string,
  title?: string
): Promise<{
  id: string;
  title: string;
  messageCount: number;
}> {
  const response = await app.inject({
    method: "POST",
    url: "/api/v1/conversations",
    headers: { authorization: `Bearer ${token}` },
    payload: title === undefined ? {} : { title },
  });
  expect(response.statusCode).toBe(201);
  return response.json() as {
    id: string;
    title: string;
    messageCount: number;
  };
}

describe("GET /api/v1/conversations", () => {
  itDb("creates and lists a tenant's conversations, newest-active first", async () => {
    const org = await registerOrg();
    await createConversation(org.accessToken, "First");
    await createConversation(org.accessToken, "Second");

    const response = await app.inject({
      method: "GET",
      url: "/api/v1/conversations",
      headers: { authorization: `Bearer ${org.accessToken}` },
    });
    expect(response.statusCode).toBe(200);
    const body = response.json() as {
      items: Array<{ id: string; title: string; messageCount: number }>;
      pagination: { total: number; hasMore: boolean };
    };
    expect(body.pagination.total).toBe(2);
    expect(body.items.map((c) => c.title)).toEqual(["Second", "First"]);
  });

  itDb("paginates with limit/offset", async () => {
    const org = await registerOrg();
    for (let i = 0; i < 3; i++) await createConversation(org.accessToken);

    const response = await app.inject({
      method: "GET",
      url: "/api/v1/conversations?limit=2&offset=1",
      headers: { authorization: `Bearer ${org.accessToken}` },
    });
    const body = response.json() as {
      items: unknown[];
      pagination: { limit: number; offset: number; total: number; hasMore: boolean };
    };
    expect(body.items).toHaveLength(2);
    expect(body.pagination).toMatchObject({ limit: 2, offset: 1, total: 3, hasMore: false });
  });

  itDb("does not leak another tenant's conversations", async () => {
    const a = await registerOrg();
    const b = await registerOrg();
    await createConversation(a.accessToken, "A-Conversation");

    const response = await app.inject({
      method: "GET",
      url: "/api/v1/conversations",
      headers: { authorization: `Bearer ${b.accessToken}` },
    });
    const body = response.json() as { items: unknown[]; pagination: { total: number } };
    expect(body.pagination.total).toBe(0);
  });
});

describe("GET /api/v1/conversations/:id", () => {
  itDb("returns an empty transcript for a fresh conversation", async () => {
    const org = await registerOrg();
    const conv = await createConversation(org.accessToken, "Handbook chat");

    const response = await app.inject({
      method: "GET",
      url: `/api/v1/conversations/${conv.id}`,
      headers: { authorization: `Bearer ${org.accessToken}` },
    });
    expect(response.statusCode).toBe(200);
    const body = response.json() as {
      id: string;
      title: string;
      messageCount: number;
      messages: unknown[];
    };
    expect(body.id).toBe(conv.id);
    expect(body.title).toBe("Handbook chat");
    expect(body.messageCount).toBe(0);
    expect(body.messages).toEqual([]);
  });

  itDb("hides other tenants' conversations (404)", async () => {
    const owner = await registerOrg();
    const stranger = await registerOrg();
    const conv = await createConversation(owner.accessToken);

    const response = await app.inject({
      method: "GET",
      url: `/api/v1/conversations/${conv.id}`,
      headers: { authorization: `Bearer ${stranger.accessToken}` },
    });
    expect(response.statusCode).toBe(404);
    expect(response.json()).toMatchObject({ error: { code: "NOT_FOUND" } });
  });
});

describe("POST /api/v1/conversations/:id/messages", () => {
  itDb("runs RAG and stores the user + assistant turn with citations", async () => {
    const org = await registerOrg();
    const conv = await createConversation(org.accessToken, "Leave chat");

    const response = await app.inject({
      method: "POST",
      url: `/api/v1/conversations/${conv.id}/messages`,
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload: { content: "How much annual leave do I get?" },
    });
    expect(response.statusCode).toBe(201);
    const body = response.json() as {
      messageCount: number;
      messages: Array<{
        id: string;
        position: number;
        role: string;
        content: string;
        payload: { citations: unknown[]; refused: boolean; provider: string };
      }>;
    };
    expect(body.messageCount).toBe(2);
    expect(body.messages).toHaveLength(2);
    expect(body.messages[0]).toMatchObject({
      role: "user",
      content: "How much annual leave do I get?",
      position: 1,
    });
    expect(body.messages[1]).toMatchObject({
      role: "assistant",
      position: 2,
    });
    expect(body.messages[1]!.content).toContain("annual leave");
    expect(body.messages[1]!.payload.refused).toBe(false);
    expect(body.messages[1]!.payload.provider).toBe("test-extract");
    expect(Array.isArray(body.messages[1]!.payload.citations)).toBe(true);
  });

  itDb("persists the transcript so a later GET replays it with citations", async () => {
    const org = await registerOrg();
    const conv = await createConversation(org.accessToken);

    await app.inject({
      method: "POST",
      url: `/api/v1/conversations/${conv.id}/messages`,
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload: { content: "How much annual leave do I get?" },
    });

    const response = await app.inject({
      method: "GET",
      url: `/api/v1/conversations/${conv.id}`,
      headers: { authorization: `Bearer ${org.accessToken}` },
    });
    const body = response.json() as {
      messageCount: number;
      messages: Array<{ role: string; content: string; payload: { citations: unknown[] } }>;
    };
    expect(body.messageCount).toBe(2);
    expect(body.messages.map((m) => m.role)).toEqual(["user", "assistant"]);
    // the assistant turn carries its citation provenance for the Sources block
    expect(body.messages[1]!.payload.citations).toBeDefined();
  });

  itDb("appends further turns in order across multiple messages", async () => {
    const org = await registerOrg();
    const conv = await createConversation(org.accessToken);

    await app.inject({
      method: "POST",
      url: `/api/v1/conversations/${conv.id}/messages`,
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload: { content: "first question" },
    });
    const second = await app.inject({
      method: "POST",
      url: `/api/v1/conversations/${conv.id}/messages`,
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload: { content: "second question" },
    });
    const body = second.json() as {
      messages: Array<{ position: number; role: string; content: string }>;
    };
    expect(body.messages.map((m) => m.role)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
    ]);
    expect(body.messages.map((m) => m.position)).toEqual([1, 2, 3, 4]);
  });

  itDb("carries the prior turns into the RAG call as conversation memory", async () => {
    const org = await registerOrg();
    const conv = await createConversation(org.accessToken);

    await app.inject({
      method: "POST",
      url: `/api/v1/conversations/${conv.id}/messages`,
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload: { content: "What is the refund policy?" },
    });
    const before = capturedInputs.length;
    const second = await app.inject({
      method: "POST",
      url: `/api/v1/conversations/${conv.id}/messages`,
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload: { content: "What about international customers?" },
    });
    const after = capturedInputs.length;

    expect(after - before).toBe(1);
    const secondInput = capturedInputs[before]!;
    // the follow-up is resolved into a self-contained retrieval/generation query
    expect(secondInput.question).toContain("refund policy");
    expect(secondInput.question).toContain("international customers");
    // the prior user+assistant turns are shipped to the generator
    expect(secondInput.history).toEqual([
      { role: "user", content: "What is the refund policy?" },
      { role: "assistant", content: "Answer: annual leave is 20 working days [1]" },
    ]);
  });

  itDb("refuses a message for another tenant's conversation (404)", async () => {
    const owner = await registerOrg();
    const stranger = await registerOrg();
    const conv = await createConversation(owner.accessToken);

    const response = await app.inject({
      method: "POST",
      url: `/api/v1/conversations/${conv.id}/messages`,
      headers: { authorization: `Bearer ${stranger.accessToken}` },
      payload: { content: "hello" },
    });
    expect(response.statusCode).toBe(404);
  });

  itDb("rejects a blank content with 400", async () => {
    const org = await registerOrg();
    const conv = await createConversation(org.accessToken);

    const response = await app.inject({
      method: "POST",
      url: `/api/v1/conversations/${conv.id}/messages`,
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload: { content: "   " },
    });
    expect(response.statusCode).toBe(400);
  });
});

describe("DELETE /api/v1/conversations/:id", () => {
  itDb("deletes the conversation and its messages", async () => {
    const org = await registerOrg();
    const conv = await createConversation(org.accessToken);

    await app.inject({
      method: "POST",
      url: `/api/v1/conversations/${conv.id}/messages`,
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload: { content: "hello" },
    });

    const del = await app.inject({
      method: "DELETE",
      url: `/api/v1/conversations/${conv.id}`,
      headers: { authorization: `Bearer ${org.accessToken}` },
    });
    expect(del.statusCode).toBe(204);

    const after = await app.inject({
      method: "GET",
      url: `/api/v1/conversations/${conv.id}`,
      headers: { authorization: `Bearer ${org.accessToken}` },
    });
    expect(after.statusCode).toBe(404);

    const { rows } = await pool.query(
      "SELECT count(*)::int AS n FROM messages WHERE conversation_id = $1::uuid",
      [conv.id]
    );
    expect((rows[0] as { n: number }).n).toBe(0);
  });

  itDb("is a 404 for a conversation that does not exist or is foreign", async () => {
    const stranger = await registerOrg();
    const owner = await registerOrg();
    const conv = await createConversation(owner.accessToken);

    const missing = await app.inject({
      method: "DELETE",
      url: `/api/v1/conversations/${randomUUID()}`,
      headers: { authorization: `Bearer ${stranger.accessToken}` },
    });
    expect(missing.statusCode).toBe(404);

    const foreign = await app.inject({
      method: "DELETE",
      url: `/api/v1/conversations/${conv.id}`,
      headers: { authorization: `Bearer ${stranger.accessToken}` },
    });
    expect(foreign.statusCode).toBe(404);
  });
});

describe("validation & auth for /api/v1/conversations", () => {
  itDb("rejects a malformed conversation id with 400", async () => {
    const org = await registerOrg();
    const response = await app.inject({
      method: "GET",
      url: "/api/v1/conversations/not-a-uuid",
      headers: { authorization: `Bearer ${org.accessToken}` },
    });
    expect(response.statusCode).toBe(400);
  });

  itDb("requires a valid access token", async () => {
    const response = await app.inject({
      method: "GET",
      url: "/api/v1/conversations",
    });
    expect(response.statusCode).toBe(401);
  });

  itDb("rejects unknown body keys on message with 400", async () => {
    const org = await registerOrg();
    const conv = await createConversation(org.accessToken);
    const response = await app.inject({
      method: "POST",
      url: `/api/v1/conversations/${conv.id}/messages`,
      headers: { authorization: `Bearer ${org.accessToken}` },
      payload: { content: "hello", nonsense: true },
    });
    expect(response.statusCode).toBe(400);
  });
});
