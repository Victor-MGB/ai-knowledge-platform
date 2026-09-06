import { describe, expect, it, vi } from "vitest";

import { DocumentsService } from "../src/services/documents.service.js";
import {
  FileValidationService,
  FileValidationError,
  sanitizeFilename,
} from "../src/services/file-validation.service.js";
import { MemoryStorageClient } from "../src/services/storage.service.js";

const validator = new FileValidationService(1024 * 1024);
const PDF = Buffer.from("%PDF-1.7\n% doc\n1 0 obj\nendobj\n%%EOF");
const DOCX = Buffer.from([0x50, 0x4b, 0x03, 0x04, 0x00, 0x00, 0x00, 0x00]);

describe("file validation", () => {
  it("accepts a real pdf and reports detected metadata", () => {
    const info = validator.validate("guide.pdf", PDF);
    expect(info).toMatchObject({ sourceType: "pdf", mimeType: "application/pdf" });
  });

  it("accepts a docx (zip container) and reports its mime", () => {
    const info = validator.validate("report.docx", DOCX);
    expect(info.sourceType).toBe("docx");
    expect(info.mimeType).toContain("officedocument");
  });

  it("accepts markdown and other text types without magic bytes", () => {
    expect(validator.validate("notes.md", Buffer.from("# hi")).sourceType).toBe("md");
    expect(validator.validate("page.html", Buffer.from("<p>hi</p>")).sourceType).toBe("html");
    expect(validator.validate("raw.txt", Buffer.from("hello")).sourceType).toBe("txt");
  });

  it("rejects a renamed or spoofed file (zip bytes named .pdf)", () => {
    expect(() => validator.validate("fake.pdf", DOCX)).toThrow(FileValidationError);
    try {
      validator.validate("fake.pdf", DOCX);
    } catch (error) {
      expect((error as FileValidationError).errorCode).toBe("FILE_TYPE_MISMATCH");
    }
  });

  it("rejects non-zip bytes named .docx", () => {
    expect(() => validator.validate("fake.docx", Buffer.from("not a zip"))).toThrow(
      "zip container"
    );
  });

  it("rejects binary bytes passed off as text", () => {
    expect(() =>
      validator.validate("notes.md", Buffer.from([0x68, 0x69, 0x00, 0x67]))
    ).toThrow(FileValidationError);
  });

  it("rejects unsupported extensions and fails, not trust, hostile names", () => {
    expect(() => validator.validate("virus.exe", PDF)).toThrow(FileValidationError);
    expect(() => validator.validate("", PDF)).toThrow(FileValidationError);
    // path traversal is sanitized away (name becomes bare), not rejected
    expect(validator.validate("../traversal.pdf", PDF).filename).toBe("traversal.pdf");
    expect(validator.validate("../../etc/passwd.pdf", PDF).filename).toBe("passwd.pdf");
    expect(sanitizeFilename("../../etc/passwd")).toBe("passwd");
    expect(sanitizeFilename("C:\\Users\\az\\doc.pdf")).toBe("doc.pdf");
    // anything that sanitizes down to nothing is rejected
    expect(() => validator.validate("...\\..", PDF)).toThrow(FileValidationError);
  });

  it("rejects files over the configured limit", () => {
    const tiny = new FileValidationService(16);
    expect(() => tiny.validate("big.pdf", PDF)).toThrow(FileValidationError);
    try {
      tiny.validate("big.pdf", PDF);
    } catch (error) {
      expect((error as FileValidationError).errorCode).toBe("FILE_TOO_LARGE");
    }
  });

  it("rejects an empty file", () => {
    expect(() => validator.validate("empty.pdf", Buffer.alloc(0))).toThrow("empty");
  });
});

describe("documents service", () => {
  const memory = () => new MemoryStorageClient();

  function fakeDb(overrides?: { insertThrows?: boolean }) {
    let counter = 0;
    const rows = [
      {
        id: "doc-1",
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
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ];
    return {
      query: vi.fn(async () => {
        if (overrides?.insertThrows) throw new Error("boom");
        counter += 1;
        return { rows: [{ ...rows[0], id: `doc-${counter}` }] };
      }),
    };
  }

  it("uploads to storage with a unique per-org key and records metadata", async () => {
    const storage = memory();
    const putSpy = vi.spyOn(storage, "put");
    const db = fakeDb();
    const svc = new DocumentsService(db as never, storage, validator);

    const doc = await svc.upload({
      filename: "guide.pdf",
      buffer: PDF,
      organizationId: "org-1",
      uploadedBy: "user-1",
    });

    expect(doc.filename).toBe("guide.pdf");
    expect(doc.mimeType).toBe("application/pdf");
    expect(doc.status).toBe("queued");

    // what really went to storage is the generated key, independent of the
    // (fake, per-call) DB row
    const key = putSpy.mock.calls[0][0];
    expect(key).toMatch(/^org-1\/[0-9a-f-]{36}\.pdf$/);
    const stored = await storage.head(key);
    expect(stored?.size).toBe(PDF.length);
    expect(db.query).toHaveBeenCalledTimes(1);
  });

  it("deletes the stored object when the DB insert fails (compensating action)", async () => {
    const storage = memory();
    const putSpy = vi.spyOn(storage, "put");
    const db = fakeDb({ insertThrows: true });
    const svc = new DocumentsService(db as never, storage, validator);

    await expect(
      svc.upload({
        filename: "guide.pdf",
        buffer: PDF,
        organizationId: "org-1",
        uploadedBy: "user-1",
      })
    ).rejects.toThrow("boom");

    // the object must not survive: any orphan is a leaked byte
    const key = putSpy.mock.calls[0][0];
    expect(await storage.head(key)).toBeUndefined();
  });

  it("two uploads of identical bytes produce independent documents", async () => {
    const storage = memory();
    const putSpy = vi.spyOn(storage, "put");
    const db = fakeDb();
    const svc = new DocumentsService(db as never, storage, validator);

    const a = await svc.upload({
      filename: "guide.pdf",
      buffer: PDF,
      organizationId: "org-1",
      uploadedBy: "user-1",
    });
    const b = await svc.upload({
      filename: "guide.pdf",
      buffer: PDF,
      organizationId: "org-1",
      uploadedBy: "user-1",
    });

    expect(a.id).not.toBe(b.id);
    expect(putSpy.mock.calls).toHaveLength(2);
    expect(putSpy.mock.calls[0][0]).not.toBe(putSpy.mock.calls[1][0]);
  });

  it("enqueues the uploaded document with the AI service", async () => {
    const storage = memory();
    const db = fakeDb();
    const enqueued: string[] = [];
    const enqueuer = { enqueue: vi.fn(async (id: string) => void enqueued.push(id)) };
    const svc = new DocumentsService(db as never, storage, validator, enqueuer);

    const doc = await svc.upload({
      filename: "guide.pdf",
      buffer: PDF,
      organizationId: "org-1",
      uploadedBy: "user-1",
    });

    expect(enqueued).toEqual([doc.id]);
  });

  it("never fails an upload when the queue is down", async () => {
    const storage = memory();
    const putSpy = vi.spyOn(storage, "put");
    const db = fakeDb();
    const enqueuer = {
      enqueue: vi.fn(async () => {
        throw new Error("connection refused");
      }),
    };
    const svc = new DocumentsService(db as never, storage, validator, enqueuer);

    const doc = await svc.upload({
      filename: "guide.pdf",
      buffer: PDF,
      organizationId: "org-1",
      uploadedBy: "user-1",
    });
    expect(doc.id).toBeDefined();
    expect(db.query).toHaveBeenCalledTimes(1);
    // the object must still exist: a failed enqueue is not a failed upload
    const key = putSpy.mock.calls[0][0];
    expect(await storage.head(key)).toBeDefined();
  });

  it("no-op without an enqueuer (CLI/previous callers)", async () => {
    const storage = memory();
    const db = fakeDb();
    const svc = new DocumentsService(db as never, storage, validator);
    const doc = await svc.upload({
      filename: "guide.pdf",
      buffer: PDF,
      organizationId: "org-1",
      uploadedBy: "user-1",
    });
    expect(doc.status).toBe("queued");
  });
});