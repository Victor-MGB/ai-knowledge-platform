import { describe, expect, it, vi } from "vitest";

import { RagService, buildContext, expandQuestionForRetrieval } from "../src/services/rag.service.js";
import type { SearchResultItem } from "../src/services/search.service.js";
import type {
  RagGenerator,
  RagGeneration,
  RagGenerateInput,
} from "../src/services/rag-generator-client.service.js";

function searchItem(partial: Record<string, unknown> = {}): SearchResultItem {
  return {
    chunk: {
      id: "11111111-1111-1111-1111-111111111111",
      chunkIndex: 0,
      pageNumber: 1,
      tokenCount: 42,
      content: "refunds are processed within 5 days",
    },
    similarity: 0.75,
    document: {
      id: "22222222-2222-2222-2222-222222222222",
      title: "guide",
      filename: "guide.pdf",
      mimeType: "application/pdf",
      sourceType: "pdf",
    },
    page: 1,
    metadata: { strategy: "paragraph", section: "policy" },
    ...partial,
  } as SearchResultItem;
}

const DONT_KNOW = "I don't know.";

function fakeSearch(results: SearchResultItem[]) {
  const fn = vi.fn(async (_org: string, _user: string, _q: string, _options?: Record<string, unknown>) => ({
    query: "test",
    model: "knowflow-hash-384",
    results,
  }));
  return { search: fn as never };
}

function fakeGenerator(overrides: Partial<RagGeneration> = {}) {
  const fn = vi.fn(async (input: RagGenerateInput): Promise<RagGeneration> => {
    const refused =
      !input.context.length ||
      (input.minScore !== undefined &&
        input.context[0]!.similarity !== undefined &&
        input.context[0]!.similarity < input.minScore);
    return {
      answer: refused ? DONT_KNOW : "refunds happen in five days.",
      refused,
      provider: "extract",
      model: "knowflow-extract-1",
      evidence: input.context.map((c, i) => ({
        index: i,
        chunkId: c.chunkId,
        documentId: c.documentId,
        documentTitle: c.documentTitle,
        section: c.section ?? null,
        page: c.page ?? null,
        similarity: c.similarity ?? null,
      })),
      citations: input.context.map((c, i) => ({
        id: i + 1,
        title: c.documentTitle ?? null,
        section: c.section ?? null,
        page: c.page ?? null,
        chunkId: c.chunkId,
        documentId: c.documentId,
        similarity: c.similarity ?? null,
      })),
      usage: { promptTokens: 10, completionTokens: 3, totalTokens: 13 },
      ...overrides,
    };
  });
  return { generate: fn } as unknown as RagGenerator;
}

describe("rag service", () => {
  it("retrieves, budgets context best-first by token count, and forwards to the generator", async () => {
    const results = [
      searchItem({ chunk: { id: "a", chunkIndex: 0, pageNumber: 1, tokenCount: 400, content: "cancellations fast" }, similarity: 0.9 }),
      searchItem({ chunk: { id: "b", chunkIndex: 1, pageNumber: 2, tokenCount: 900, content: "refunds process" }, similarity: 0.75 }),
      searchItem({ chunk: { id: "c", chunkIndex: 2, pageNumber: 3, tokenCount: 100, content: "tiny text" }, similarity: 0.3 }),
    ];
    const search = fakeSearch(results);
    const generator = fakeGenerator();
    const svc = new RagService(search as never, generator, 1200);

    const response = await svc.generate("org-1", "user-1", "question");

    expect(response.answer).toBe("refunds happen in five days.");
    expect(response.refused).toBe(false);
    expect(response.provider).toBe("extract");
    expect(response.model).toBe("knowflow-extract-1");
    expect(response.retrieval.query).toBe("test");
    expect(response.retrieval.model).toBe("knowflow-hash-384");
    // budget: 400@0.9 kept, 900@0.75 → 1300 > 1200 skip, 100@0.3 → 500 ≤ 1200 keep
    expect(response.retrieval.retrieved).toBe(2);
    const input = generator.generate.mock.calls[0]![0] as RagGenerateInput;
    expect(input.context[0]!.text).toBe("cancellations fast");
    expect(input.context[0]!.similarity).toBe(0.9);
    expect(input.context[1]!.text).toBe("tiny text");
    expect(input.maxContextTokens).toBe(1200);
  });

  it("passes limit, sourceType, and metadata to retrieval", async () => {
    const search = fakeSearch([searchItem()]);
    const generator = fakeGenerator();
    const svc = new RagService(search as never, generator);

    await svc.generate("org-1", "user-1", "q", {
      limit: 3,
      sourceType: "pdf",
      metadata: { section: "x" },
    });

    expect(search.search).toHaveBeenCalledWith(
      "org-1",
      "user-1",
      "q",
      {
        limit: 3,
        sourceType: "pdf",
        metadata: { section: "x" },
      }
    );
  });

  it("honors maxContextTokens from the request (over default)", async () => {
    const results = [
      searchItem({ chunk: { id: "a", chunkIndex: 0, pageNumber: 1, tokenCount: 400, content: "a" }, similarity: 0.9 }),
      searchItem({ chunk: { id: "b", chunkIndex: 1, pageNumber: 2, tokenCount: 900, content: "b" }, similarity: 0.75 }),
    ];
    const search = fakeSearch(results);
    const generator = fakeGenerator();
    const svc = new RagService(search as never, generator, 1200);

    await svc.generate("org-1", "user-1", "q", { maxContextTokens: 500 });
    const input = generator.generate.mock.calls[0]![0] as RagGenerateInput;
    // 400 fits, 400+900 > 500 skip
    expect(input.context).toHaveLength(1);
    expect(input.context[0]!.text).toBe("a");
    expect(input.maxContextTokens).toBe(500);
  });

  it("maps a refused generation as a 200-shaped response (not an error)", async () => {
    const search = fakeSearch([]);
    const generator = fakeGenerator({
      answer: DONT_KNOW,
      refused: true,
      evidence: [],
    });
    const svc = new RagService(search as never, generator);

    const response = await svc.generate("org-1", "user-1", "q");
    expect(response.refused).toBe(true);
    expect(response.answer).toBe(DONT_KNOW);
    expect(response.evidence).toEqual([]);
    expect(response.retrieval.retrieved).toBe(0);
  });

  it("is a 503 RAG_GENERATION_UNAVAILABLE when the generator fails", async () => {
    const search = fakeSearch([searchItem()]);
    const failing: RagGenerator = {
      generate: vi.fn(async () => {
        throw new Error("ai service down");
      }),
    };
    const svc = new RagService(search as never, failing);

    await expect(svc.generate("org-1", "user-1", "q")).rejects.toMatchObject({
      code: "RAG_GENERATION_UNAVAILABLE",
      statusCode: 503,
    });
  });

  it("passes minScore through to the generator", async () => {
    const search = fakeSearch([searchItem({ similarity: 0.6 })]);
    const generator = fakeGenerator();
    const svc = new RagService(search as never, generator);

    await svc.generate("org-1", "user-1", "q", { minScore: 0.5 });
    const input = generator.generate.mock.calls[0]![0] as RagGenerateInput;
    expect(input.minScore).toBe(0.5);
  });
});

describe("buildContext", () => {
  it("keeps the strongest chunk even when it alone exceeds the budget", () => {
    const results = [
      searchItem({
        chunk: { id: "x", chunkIndex: 0, pageNumber: 1, tokenCount: 900, content: "big" },
        similarity: 0.9,
      }),
    ];
    const context = buildContext(results, 500);
    expect(context).toHaveLength(1);
    expect(context[0]!.text).toBe("big");
  });

  it("best-first ordering means evidence[0] is always the strongest", () => {
    const results = [
      searchItem({ chunk: { id: "weak", chunkIndex: 0, pageNumber: 1, tokenCount: 10, content: "weak" }, similarity: 0.1 }),
      searchItem({ chunk: { id: "strong", chunkIndex: 0, pageNumber: 1, tokenCount: 10, content: "strong" }, similarity: 0.9 }),
    ];
    const context = buildContext(results, 1000);
    expect(context[0]!.text).toBe("strong");
    expect(context[0]!.similarity).toBe(0.9);
  });

  it("preserves section metadata from the chunk's metadata bag", () => {
    const results = [
      searchItem({
        chunk: { id: "s", chunkIndex: 0, pageNumber: 1, tokenCount: 10, content: "text" },
        similarity: 0.8,
        metadata: { strategy: "paragraph", section: "Return Window" },
      }),
    ];
    const context = buildContext(results, 1000);
    expect(context[0]!.section).toBe("Return Window");
  });
});

describe("conversation memory (Day 20)", () => {
  const history = [
    { role: "user" as const, content: "What is the refund policy?" },
    { role: "assistant" as const, content: "The refund period is 30 days. [1]" },
  ];

  it("passes a standalone question retrieval through unchanged", () => {
    expect(expandQuestionForRetrieval(history, "How do I file a warranty claim?")).toBe(
      "How do I file a warranty claim?"
    );
    expect(expandQuestionForRetrieval([], "What about international customers?")).toBe(
      "What about international customers?"
    );
  });

  it("expands a referential follow-up with the thread anchor", () => {
    const query = expandQuestionForRetrieval(history, "What about international customers?");
    expect(query).toContain("refund policy");
    expect(query).toContain("30 days");
    expect(query).toContain("international customers");
    // the citation marker carries no retrieval meaning and is clipped
    expect(query).not.toContain("[1]");
  });

  it("searches and generates on the expanded query, but reports the raw question", async () => {
    const search = fakeSearch([searchItem()]);
    const generator = fakeGenerator();
    const svc = new RagService(search as never, generator);

    const response = await svc.generate("org-1", "user-1", "What about international customers?", {
      history,
    });

    const searchCall = (search.search as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(searchCall![2]).toContain("refund policy");
    // the persisted/reported question stays the raw user turn
    expect(response.question).toBe("What about international customers?");
    const input = generator.generate.mock.calls[0]![0] as RagGenerateInput;
    expect(input.question).toContain("refund policy");
    expect(input.history).toEqual(history);
  });

  it("passes no history to the generator when none is given", async () => {
    const search = fakeSearch([searchItem()]);
    const generator = fakeGenerator();
    const svc = new RagService(search as never, generator);

    await svc.generate("org-1", "user-1", "What is the refund policy?");
    const input = generator.generate.mock.calls[0]![0] as RagGenerateInput;
    expect(input.history).toEqual([]);
  });
});
