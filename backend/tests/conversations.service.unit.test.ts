import { describe, expect, it, vi, beforeEach } from "vitest";

import { ConversationsService, buildHistoryWindow } from "../src/services/conversations.service.js";
import type { RagService } from "../src/services/rag.service.js";

vi.mock("../src/database/repositories/conversation.repository.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/database/repositories/conversation.repository.js")>();
  return {
    ...actual,
    insertConversation: vi.fn(),
    getConversationByOrg: vi.fn(),
    listConversationsByOrg: vi.fn(),
    deleteConversation: vi.fn(),
    listMessagesByConversation: vi.fn(),
    insertMessage: vi.fn(),
  };
});

import {
  insertConversation,
  getConversationByOrg,
  listMessagesByConversation,
  insertMessage,
  deleteConversation,
} from "../src/database/repositories/conversation.repository.js";

const mock = {
  insertConversation: vi.mocked(insertConversation),
  getConversationByOrg: vi.mocked(getConversationByOrg),
  listMessagesByConversation: vi.mocked(listMessagesByConversation),
  insertMessage: vi.mocked(insertMessage),
  deleteConversation: vi.mocked(deleteConversation),
};

function fakeDb() {
  const tx = {
    query: vi.fn(async (_text: string, _values?: readonly unknown[]) => ({ rows: [] })),
    release: vi.fn(),
  };
  const db = {
    query: vi.fn(async () => ({ rows: [] })),
    connect: vi.fn(async () => tx),
  };
  return { db, tx };
}

function fakeRag(answer = "annual leave is 20 working days [1]"): {
  rag: RagService;
  generateSpy: ReturnType<typeof vi.fn>;
} {
  const generateSpy = vi.fn(async () => ({
    question: "q",
    answer,
    refused: false,
    provider: "extract",
    model: "knowflow-extract-1",
    retrieval: { query: "q", model: "knowflow-hash-384", retrieved: 1 },
    evidence: [],
    citations: [
      {
        id: 1,
        title: "Employee Handbook",
        section: null,
        page: 14,
        chunkId: "chunk-1",
        documentId: "doc-1",
        similarity: 0.8,
      },
    ],
    usage: { promptTokens: 5, completionTokens: 3, totalTokens: 8 },
  }));
  return { rag: { generate: generateSpy } as unknown as RagService, generateSpy };
}

const CONVERSATION = {
  id: "c1",
  organization_id: "o1",
  title: "Leave chat",
  created_by: "u1",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ConversationsService", () => {
  it("creates a conversation with a default title when none is supplied", async () => {
    const { db } = fakeDb();
    mock.insertConversation.mockResolvedValue(CONVERSATION);
    const service = new ConversationsService(db, fakeRag().rag);

    const view = await service.create("o1", "u1", "  ");

    expect(mock.insertConversation).toHaveBeenCalledWith(db, {
      organizationId: "o1",
      title: "New conversation",
      createdBy: "u1",
    });
    expect(view).toMatchObject({
      id: "c1",
      messageCount: 0,
      messages: [],
    });
  });

  it("uses the supplied title when present", async () => {
    const { db } = fakeDb();
    mock.insertConversation.mockResolvedValue(CONVERSATION);
    const service = new ConversationsService(db, fakeRag().rag);

    await service.create("o1", "u1", "  Handbook Q&A  ");

    expect(mock.insertConversation).toHaveBeenCalledWith(
      db,
      expect.objectContaining({ title: "Handbook Q&A" })
    );
  });

  it("returns null for a get outside the org (existence hidden)", async () => {
    const { db } = fakeDb();
    mock.getConversationByOrg.mockResolvedValue(null);
    const service = new ConversationsService(db, fakeRag().rag);

    expect(await service.get("o1", "unknown")).toBeNull();
  });

  it("rejects a blank message before touching the DB", async () => {
    const { db } = fakeDb();
    const service = new ConversationsService(db, fakeRag().rag);

    await expect(
      service.addMessage("o1", "u1", "c1", { content: "   ", rag: {} })
    ).rejects.toMatchObject({ code: "VALIDATION_ERROR", statusCode: 400 });
    expect(mock.getConversationByOrg).not.toHaveBeenCalled();
  });

  it("404s a message into a conversation that is not in the org", async () => {
    const { db } = fakeDb();
    mock.getConversationByOrg.mockResolvedValue(null);
    const service = new ConversationsService(db, fakeRag().rag);

    await expect(
      service.addMessage("o1", "u1", "c1", { content: "hello", rag: {} })
    ).rejects.toMatchObject({ code: "NOT_FOUND", statusCode: 404 });
  });

  it("stores the user and assistant turn atomically with citation payload", async () => {
    const { db, tx } = fakeDb();
    mock.getConversationByOrg.mockResolvedValue({ ...CONVERSATION, message_count: 0, last_message_at: null });
    mock.insertMessage.mockImplementation(async (_d, params) =>
      Promise.resolve({
        id: `m-${params.position}`,
        organization_id: "o1",
        conversation_id: "c1",
        position: params.position,
        role: params.role,
        content: params.content,
        payload: params.payload ?? {},
        created_at: "2026-01-01T00:00:00Z",
      })
    );
    mock.listMessagesByConversation.mockResolvedValue([
      { id: "m-1", organization_id: "o1", conversation_id: "c1", position: 1, role: "user", content: "how much leave?", payload: {}, created_at: "t" },
      { id: "m-2", organization_id: "o1", conversation_id: "c1", position: 2, role: "assistant", content: "annual leave is 20 working days [1]", payload: { citations: [1] }, created_at: "t" },
    ]);
    // tx.query drives nextPosition + conversation touch; BEGIN/COMMIT passthrough
    (tx.query as ReturnType<typeof vi.fn>).mockImplementation(
      (text: string, values?: readonly unknown[]) => {
        if ((text as string).trimStart().toUpperCase().startsWith("SELECT")) {
          return Promise.resolve({ rows: [{ next: 1 }] });
        }
        return Promise.resolve({ rows: [] });
      }
    );
    const service = new ConversationsService(db, fakeRag().rag);

    const view = await service.addMessage("o1", "u1", "c1", {
      content: "how much leave?",
      rag: { limit: 3 },
    });

    expect(mock.insertMessage).toHaveBeenCalledWith(
      tx,
      expect.objectContaining({ role: "user", content: "how much leave?", position: 1 })
    );
    // assistant stores its citations in payload
    const assistantCall = mock.insertMessage.mock.calls.find(
      ([, p]) => (p as { role?: string }).role === "assistant"
    );
    expect(assistantCall).toBeDefined();
    const assistantParams = assistantCall![1] as {
      position: number;
      payload: { citations: unknown[]; refused: boolean; provider: string };
    };
    expect(assistantParams.position).toBe(2);
    expect(assistantParams.payload.citations[0]).toMatchObject({ id: 1, title: "Employee Handbook", page: 14 });
    expect(assistantParams.payload.refused).toBe(false);
    expect(view.messageCount).toBe(2);
    expect(view.messages).toHaveLength(2);
    expect(tx.release).toHaveBeenCalled();
  });

  it("rolls back the transaction when the assistant insert fails", async () => {
    const { db, tx } = fakeDb();
    mock.getConversationByOrg.mockResolvedValue({ ...CONVERSATION, message_count: 0, last_message_at: null });
    mock.insertMessage
      .mockImplementationOnce(async (_d, params) =>
        Promise.resolve({
          id: "m-1", organization_id: "o1", conversation_id: "c1", position: params.position,
          role: "user", content: params.content, payload: {}, created_at: "t",
        })
      )
      .mockRejectedValueOnce(new Error("assistant insert failed"));
    (tx.query as ReturnType<typeof vi.fn>).mockImplementation(
      (text: string) =>
        (text as string).trimStart().toUpperCase().startsWith("SELECT")
          ? Promise.resolve({ rows: [{ next: 1 }] })
          : Promise.resolve({ rows: [] })
    );
    const service = new ConversationsService(db, fakeRag().rag);

    await expect(service.addMessage("o1", "u1", "c1", { content: "q", rag: {} })).rejects.toThrow(
      "assistant insert failed"
    );
    const texts = (tx.query as ReturnType<typeof vi.fn>).mock.calls.map((c) => (c[0] as string).trimStart().toUpperCase());
    expect(texts).toContain("BEGIN");
    expect(texts).toContain("ROLLBACK");
    expect(texts).not.toContain("COMMIT");
  });

  it("404s removing a conversation not in the org", async () => {
    const { db } = fakeDb();
    mock.deleteConversation.mockResolvedValue(false);
    const service = new ConversationsService(db, fakeRag().rag);

    await expect(service.remove("o1", "c1")).rejects.toMatchObject({ code: "NOT_FOUND", statusCode: 404 });
  });

  it("passes the prior turns as conversation memory to the RAG pipeline", async () => {
    const { db, tx } = fakeDb();
    mock.getConversationByOrg.mockResolvedValue({ ...CONVERSATION, message_count: 2, last_message_at: "t" });
    mock.listMessagesByConversation.mockResolvedValue([
      { id: "m-1", organization_id: "o1", conversation_id: "c1", position: 1, role: "user", content: "What is the refund policy?", payload: {}, created_at: "t" },
      { id: "m-2", organization_id: "o1", conversation_id: "c1", position: 2, role: "assistant", content: "The refund period is 30 days. [1]", payload: {}, created_at: "t" },
    ]);
    mock.insertMessage.mockImplementation(async (_d, params) =>
      Promise.resolve({
        id: `m-${params.position}`, organization_id: "o1", conversation_id: "c1",
        position: params.position, role: params.role, content: params.content,
        payload: params.payload ?? {}, created_at: "t",
      })
    );
    (tx.query as ReturnType<typeof vi.fn>).mockImplementation(
      (text: string) =>
        (text as string).trimStart().toUpperCase().startsWith("SELECT")
          ? Promise.resolve({ rows: [{ next: 3 }] })
          : Promise.resolve({ rows: [] })
    );
    const { rag, generateSpy } = fakeRag();
    const service = new ConversationsService(db, rag);

    await service.addMessage("o1", "u1", "c1", {
      content: "What about international customers?",
      rag: {},
    });

    const ragInput = generateSpy.mock.calls[0] as [string, string, string, { history: { role: string; content: string }[] }];
    expect(ragInput[0]).toBe("o1");
    expect(ragInput[1]).toBe("u1");
    expect(ragInput[2]).toBe("What about international customers?");
    expect(ragInput[3].history).toEqual([
      { role: "user", content: "What is the refund policy?" },
      { role: "assistant", content: "The refund period is 30 days. [1]" },
    ]);
  });
});

describe("buildHistoryWindow (Day 20 context window)", () => {
  const msg = (position: number, role: "user" | "assistant", content: string) => ({
    id: `m-${position}`, organizationId: "o1", conversationId: "c1", position,
    role, content, payload: {}, createdAt: "t",
  });

  it("keeps a bounded recent window and returns it oldest-first", () => {
    const messages = [
      msg(1, "user", "first"),
      msg(2, "assistant", "second"),
      msg(3, "user", "third"),
      msg(4, "assistant", "fourth"),
    ];
    const window = buildHistoryWindow(messages, { maxMessages: 2, maxTokens: 1000 });
    expect(window).toEqual([
      { role: "user", content: "third" },
      { role: "assistant", content: "fourth" },
    ]);
  });

  it("drops the oldest turns when the window exceeds the token budget", () => {
    const messages = [
      msg(1, "user", "word word word word word word word word word word"),
      msg(2, "assistant", "tail"),
      msg(3, "user", "current question"),
    ];
    // a tight budget that only the most recent turn fits in
    const window = buildHistoryWindow(messages, { maxMessages: 20, maxTokens: 3 });
    expect(window.map((m) => m.content)).toEqual(["current question"]);
  });

  it("rejects the system role out of the window", () => {
    const messages = [
      msg(1, "user", "hi"),
      // a system note should never leak into generation history
      { ...msg(2, "assistant", "note"), role: "system" as const },
    ];
    const window = buildHistoryWindow(messages, { maxMessages: 20, maxTokens: 1000 });
    expect(window).toEqual([{ role: "user", content: "hi" }]);
  });

  it("returns an empty window for an empty transcript", () => {
    expect(buildHistoryWindow([], { maxMessages: 20, maxTokens: 1000 })).toEqual([]);
  });
});
