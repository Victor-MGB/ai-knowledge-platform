import { describe, expect, it, vi } from "vitest";

import { SearchService } from "../src/services/search.service.js";
import type { QueryEmbedder } from "../src/services/embedding-client.service.js";

function searchRow(partial: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    chunk_id: "11111111-1111-1111-1111-111111111111",
    chunk_index: 0,
    page_number: 1,
    token_count: 42,
    content: "refunds are processed within 5 days",
    metadata: { strategy: "paragraph", section: "policy", source: "s3://org/x.pdf", page_range: [1, 1] },
    document_id: "22222222-2222-2222-2222-222222222222",
    title: "guide",
    filename: "guide.pdf",
    mime_type: "application/pdf",
    source_type: "pdf",
    cosine_distance: 0.25,
    ...partial,
  };
}

const fakeEmbedder = (vector = [0.6, 0.8, 0.0, 0.0]): QueryEmbedder => ({
  embed: vi.fn(async () => ({ vector, model: "knowflow-hash-384" })) as never,
});

function fakeDb(handler: (sql: string, params: unknown[]) => { rows: unknown[] }) {
  return { query: handler };
}

const svc = (db: unknown, embedder?: QueryEmbedder) =>
  new SearchService(db as never, embedder, 5);

describe("search service", () => {
  it("embeds the query, scopes to the org + owner + model, and maps cosine distance to similarity", async () => {
    const embedder = fakeEmbedder();
    const handler = vi.fn(async (sql: string, params: unknown[]) => {
      expect(sql).toContain("d.organization_id = $1");
      expect(sql).toContain("d.uploaded_by = $3");
      expect(sql).toContain("e.model = $4");
      expect(sql).toContain("d.status = 'ready'");
      expect(sql).toContain("d.embedding_status = 'ready'");
      expect(sql).toContain("embedding::vector(384) <=> $2::vector");
      // query vector literal, rounded to 6 decimals like the stored vectors
      expect(params[1]).toBe("[0.600000,0.800000,0.000000,0.000000]");
      expect(params[2]).toBe("user-1");
      expect(params[3]).toBe("knowflow-hash-384");
      expect(params[4]).toBe(5);
      return { rows: [searchRow()] };
    });

    const result = await svc(fakeDb(handler), embedder).search(
      "org-1",
      "user-1",
      "refund policy"
    );

    expect(result.query).toBe("refund policy");
    expect(result.model).toBe("knowflow-hash-384");
    expect((embedder.embed as ReturnType<typeof vi.fn>).mock.calls[0][0]).toBe("refund policy");
    const item = result.results[0];
    expect(item.similarity).toBeCloseTo(0.75, 6); // 1 - 0.25
    expect(item.chunk).toEqual({
      id: "11111111-1111-1111-1111-111111111111",
      chunkIndex: 0,
      pageNumber: 1,
      tokenCount: 42,
      content: "refunds are processed within 5 days",
    });
    expect(item.page).toBe(1);
    expect(item.metadata.section).toBe("policy");
    expect(item.document.title).toBe("guide");
  });

  it("returns an empty result set for an empty corpus", async () => {
    const result = await svc(fakeDb(async () => ({ rows: [] })), fakeEmbedder()).search(
      "org-1",
      "user-1",
      "anything"
    );
    expect(result.results).toEqual([]);
  });

  it("passes the requested limit through (not just the default)", async () => {
    const handler = vi.fn(async (_sql: string, params: unknown[]) => {
      expect(params[4]).toBe(1);
      return { rows: [searchRow()] };
    });
    await svc(fakeDb(handler), fakeEmbedder()).search("org-1", "user-1", "x", { limit: 1 });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("applies a similarity floor as distance <= (1 - minSimilarity)", async () => {
    const handler = vi.fn(async (sql: string, params: unknown[]) => {
      expect(sql).toContain("(e.embedding::vector(384) <=> $2::vector)::float8 <= $5");
      expect(params[4]).toBeCloseTo(0.8, 6);   // 1 - 0.2
      expect(params[5]).toBe(5);               // limit is now the last param
      return { rows: [searchRow()] };
    });
    await svc(fakeDb(handler), fakeEmbedder()).search("org-1", "user-1", "x", { minSimilarity: 0.2 });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("adds NO similarity floor when minSimilarity is absent or zero", async () => {
    const handler = vi.fn(async (sql: string) => {
      expect(sql).not.toContain("<= $");
      return { rows: [searchRow()] };
    });
    await svc(fakeDb(handler), fakeEmbedder()).search("org-1", "user-1", "x", { minSimilarity: 0 });
    // and the default path
    await svc(fakeDb(handler), fakeEmbedder()).search("org-1", "user-1", "x");
    expect(handler).toHaveBeenCalledTimes(2);
  });

  it("filters by source type at the document level", async () => {
    const handler = vi.fn(async (sql: string, params: unknown[]) => {
      expect(sql).toContain("d.source_type = $5");
      expect(sql).toContain("c.metadata @> $6::jsonb");
      expect(params[4]).toBe("pdf");
      expect(params[5]).toBe('{"section":"Return Window"}');
      expect(params[6]).toBe(5);
      return { rows: [searchRow()] };
    });
    await svc(fakeDb(handler), fakeEmbedder()).search("org-1", "user-1", "x", {
      sourceType: "pdf",
      metadata: { section: "Return Window" },
    });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("is a 503 EMBEDDING_UNAVAILABLE when the embedder fails", async () => {
    const embedder: QueryEmbedder = {
      embed: vi.fn(async () => {
        throw new Error("connection refused");
      }),
    };
    await expect(svc(fakeDb(async () => ({ rows: [] })), embedder).search("org-1", "user-1", "x"))
      .rejects.toMatchObject({ code: "EMBEDDING_UNAVAILABLE", statusCode: 503 });
  });

  it("is a 503 EMBEDDING_UNAVAILABLE when no embedder is configured", async () => {
    await expect(svc(fakeDb(async () => ({ rows: [] }))).search("org-1", "user-1", "x"))
      .rejects.toMatchObject({ code: "EMBEDDING_UNAVAILABLE", statusCode: 503 });
  });
});