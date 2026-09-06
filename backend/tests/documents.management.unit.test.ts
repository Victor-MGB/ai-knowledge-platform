import { describe, expect, it, vi } from "vitest";

import { DocumentsService } from "../src/services/documents.service.js";
import { FileValidationService } from "../src/services/file-validation.service.js";
import { MemoryStorageClient } from "../src/services/storage.service.js";

const validator = new FileValidationService(1024 * 1024);

function docRow(partial: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    organization_id: "org-1",
    uploaded_by: "user-1",
    title: "guide",
    filename: "guide.pdf",
    mime_type: "application/pdf",
    size: 24,
    source_type: "pdf",
    storage_key: "org-1/abc.pdf",
    status: "queued",
    error: null,
    embedding_status: "none",
    embedding_error: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...partial,
  };
}

function fakeDb(handler: (sql: string, params: unknown[]) => { rows: unknown[] }) {
  return { query: handler };
}

const memory = () => new MemoryStorageClient();
const svc = (db: unknown) => new DocumentsService(db as never, memory(), validator);

describe("documents service — list", () => {
  it("passes filters through, escapes LIKE input, and reports honest pagination", async () => {
    const handler = vi.fn(async (sql: string, params: unknown[]) => {
      expect(sql).toContain("matching_total"); // window-counted, one statement
      expect(sql).toContain("uploaded_by = $2");
      expect(sql).toContain("source_type = $3");
      expect(sql).toContain("status = $4");
      expect(sql).toContain("embedding_status = $5");
      // search `gui%` must be matched literally: %gui\% %  (wildcard escaped)
      expect(params).toContain('%gui\\%%');
      return { rows: [{ ...docRow(), matching_total: "9" }, docRow({ id: "22222222-2222-2222-2222-222222222222" })] };
    });

    const result = await svc(fakeDb(handler)).listByOrganization("org-1", "user-1", {
      limit: 2,
      offset: 0,
      sourceType: "md",
      status: "queued",
      embeddingStatus: "none",
      search: "gui%",
    });

    expect(result.items).toHaveLength(2);
    expect(result.pagination).toEqual({ limit: 2, offset: 0, total: 9, hasMore: true });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("defaults to 50/0 and hasMore false when the page reaches the total", async () => {
    const handler = vi.fn(async () => ({
      rows: [{ ...docRow(), matching_total: "2" }, docRow({ id: "22222222-2222-2222-2222-222222222222" })],
    }));
    const result = await svc(fakeDb(handler)).listByOrganization("org-1", "user-1", {});
    expect(result.pagination).toEqual({ limit: 50, offset: 0, total: 2, hasMore: false });
  });

  it("returns empty pagination metadata for an empty result", async () => {
    const handler = vi.fn(async () => ({ rows: [] }));
    const result = await svc(fakeDb(handler)).listByOrganization("org-1", "user-1", {});
    expect(result.items).toEqual([]);
    expect(result.pagination.total).toBe(0);
    expect(result.pagination.hasMore).toBe(false);
  });
});

describe("documents service — get / remove / status", () => {
  it("returns the tenant-scoped detail, null when unknown", async () => {
    const found = await svc(fakeDb(async () => ({ rows: [docRow()] }))).get(
      "org-1",
      "user-1",
      "11111111-1111-1111-1111-111111111111"
    );
    expect(found?.embeddingStatus).toBe("none");

    const empty = await svc(fakeDb(async () => ({ rows: [] }))).get(
      "org-1",
      "user-1",
      "22222222-2222-2222-2222-222222222222"
    );
    expect(empty).toBeNull();
  });

  it("remove deletes the DB row and the stored object together", async () => {
    const storage = memory();
    await storage.put("org-1/abc.pdf", Buffer.from("%PDF-1.7"), "application/pdf");

    const handler = vi.fn(async (sql: string) => {
      expect(sql).toContain("DELETE FROM documents");
      return { rows: [{ storage_key: "org-1/abc.pdf" }] };
    });
    const service = new DocumentsService(fakeDb(handler) as never, storage, validator);
    await service.remove("org-1", "user-1", "11111111-1111-1111-1111-111111111111");

    expect(await storage.head("org-1/abc.pdf")).toBeUndefined();
  });

  it("remove skips the object when the row has none (seed rows)", async () => {
    const storage = memory();
    const deleteSpy = vi.spyOn(storage, "delete");
    const handler = vi.fn(async () => ({ rows: [{ storage_key: null }] }));
    const service = new DocumentsService(fakeDb(handler) as never, storage, validator);
    await service.remove("org-1", "user-1", "11111111-1111-1111-1111-111111111111");
    expect(deleteSpy).not.toHaveBeenCalled();
  });

  it("remove answers 404 for an unknown or foreign document", async () => {
    const handler = vi.fn(async () => ({ rows: [] }));
    await expect(
      svc(fakeDb(handler)).remove("org-1", "user-1", "99999999-9999-9999-9999-999999999999")
    ).rejects.toMatchObject({ code: "NOT_FOUND", statusCode: 404 });
  });

  it("status reports counts (pg bigints coerced) and derives the stage", async () => {
    const cases: Array<{ row: Record<string, unknown>; stage: string }> = [
      {
        row: docRow({ status: "queued", embedding_status: "none" }),
        stage: "uploaded",
      },
      {
        row: docRow({ status: "processing", embedding_status: "none" }),
        stage: "processing",
      },
      {
        row: docRow({ status: "ready", embedding_status: "processing" }),
        stage: "embedding",
      },
      {
        row: docRow({ status: "ready", embedding_status: "ready" }),
        stage: "ready",
      },
      { row: docRow({ status: "failed", embedding_status: "ready" }), stage: "failed" },
      {
        row: docRow({ status: "queued", embedding_status: "failed" }),
        stage: "failed",
      },
    ];

    for (const { row, stage } of cases) {
      const handler = vi.fn(async () => ({
        rows: [{ ...row, pages: "3", chunks: "2", embeddings: "1" }],
      }));
      const view = await svc(fakeDb(handler)).status("org-1", "user-1", "11111111-1111-1111-1111-111111111111");
      expect(view).not.toBeNull();
      expect(view?.counts).toEqual({ pages: 3, chunks: 2, embeddings: 1 });
      expect(view?.stage).toBe(stage);
    }
  });

  it("status returns null for an unknown document", async () => {
    const view = await svc(fakeDb(async () => ({ rows: [] }))).status(
      "org-1",
      "user-1",
      "99999999-9999-9999-9999-999999999999"
    );
    expect(view).toBeNull();
  });
});