import { randomUUID } from "node:crypto";
import { basename, extname } from "node:path";

import type { DatabaseClient } from "../database/postgres.js";
import {
  deleteDocument,
  getDocumentByOrg,
  getDocumentStatus,
  insertDocument,
  listDocumentsByOrg,
  type DocumentListOptions,
  type DocumentRow,
  type DocumentStatusRow,
} from "../database/repositories/document.repository.js";
import {
  FileValidationError,
  FileValidationService,
} from "./file-validation.service.js";
import type { StorageClient } from "./storage.service.js";
import type { EnqueueService } from "./enqueue.service.js";
import { AppError } from "../utils/errors.js";

export interface UploadDocumentInput {
  filename: string;
  buffer: Buffer;
  organizationId: string;
  uploadedBy: string;
}

export interface DocumentView {
  id: string;
  organizationId: string;
  uploadedBy: string;
  title: string;
  filename: string;
  mimeType: string;
  size: number;
  sourceType: string;
  storageKey: string | null;
  status: string;
  error: string | null;
  embeddingStatus: string;
  embeddingError: string | null;
  createdAt: string;
  updatedAt: string;
}

/** Derived ingestion stage, mirroring the AI service's state machine so both
 * stacks tell the same story: a document is READY exactly when its document
 * lifecycle completed AND its vectors landed. */
export type DocumentStage =
  | "uploaded"
  | "processing"
  | "embedding"
  | "ready"
  | "failed";

function deriveStage(status: string, embeddingStatus: string): DocumentStage {
  if (status === "failed" || embeddingStatus === "failed") return "failed";
  if (embeddingStatus === "ready") return "ready";
  if (embeddingStatus === "processing") return "embedding";
  if (status === "processing") return "processing";
  return "uploaded";
}

export interface DocumentStatusView {
  id: string;
  organizationId: string;
  filename: string;
  sourceType: string;
  status: string;
  error: string | null;
  embeddingStatus: string;
  embeddingError: string | null;
  counts: { pages: number; chunks: number; embeddings: number };
  stage: DocumentStage;
}

export interface PaginationMeta {
  limit: number;
  offset: number;
  total: number;
  hasMore: boolean;
}

export interface DocumentList extends DocumentListOptions {
  items: DocumentView[];
  pagination: PaginationMeta;
}

function toView(row: DocumentRow): DocumentView {
  return {
    id: row.id,
    organizationId: row.organization_id,
    uploadedBy: row.uploaded_by,
    title: row.title,
    filename: row.filename,
    mimeType: row.mime_type,
    size: row.size,
    sourceType: row.source_type,
    storageKey: row.storage_key,
    status: row.status,
    error: row.error,
    embeddingStatus: row.embedding_status,
    embeddingError: row.embedding_error,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

function toStatusView(row: DocumentStatusRow): DocumentStatusView {
  return {
    id: row.id,
    organizationId: row.organization_id,
    filename: row.filename,
    sourceType: row.source_type,
    status: row.status,
    error: row.error,
    embeddingStatus: row.embedding_status,
    embeddingError: row.embedding_error,
    counts: {
      pages: Number(row.pages),
      chunks: Number(row.chunks),
      embeddings: Number(row.embeddings),
    },
    stage: deriveStage(row.status, row.embedding_status),
  };
}

/** Upload orchestration: validate -> store -> record. If the DB insert fails
 * after the object hit the bucket, the object is deleted (compensating action)
 * so no orphan data accumulates. */
export class DocumentsService {
  constructor(
    private readonly db: DatabaseClient,
    private readonly storage: StorageClient,
    private readonly validator: FileValidationService,
    private readonly enqueuer?: EnqueueService
  ) {}

  async upload(input: UploadDocumentInput): Promise<DocumentView> {
    let info;
    try {
      info = this.validator.validate(input.filename, input.buffer);
    } catch (error) {
      if (error instanceof FileValidationError) {
        throw new AppError(
          error.errorCode,
          error.message,
          error.statusCode,
          { filename: input.filename }
        );
      }
      throw error;
    }

    // unique object id per upload: two uploads of the same file are two docs
    const title = basename(info.filename, extname(info.filename));
    const storageKey = `${input.organizationId}/${randomUUID()}${info.extension}`;

    await this.storage.put(storageKey, input.buffer, info.mimeType);

    try {
      const row = await insertDocument(this.db, {
        organizationId: input.organizationId,
        uploadedBy: input.uploadedBy,
        title,
        filename: info.filename,
        mimeType: info.mimeType,
        size: input.buffer.length,
        sourceType: info.sourceType,
        storageKey,
      });
      // Day 12: hand the new document to the AI service's queue so a worker
      // picks up extract -> chunk -> embed without further HTTP. Best-effort:
      // an unavailable queue must not fail an upload that already succeeded —
      // the document stays `queued` and any later sweep can re-enqueue it.
      await this.tryEnqueue(row.id);
      return toView(row);
    } catch (error) {
      await this.storage.delete(storageKey);
      throw error;
    }
  }

  private async tryEnqueue(documentId: string): Promise<void> {
    if (!this.enqueuer) {
      return;
    }
    try {
      await this.enqueuer.enqueue(documentId);
    } catch {
      // swallowed on purpose: queueing is a side effect, not the upload's job
    }
  }

  async listByOrganization(
    organizationId: string,
    userId: string,
    options: DocumentListOptions = {}
  ): Promise<DocumentList> {
    const limit = options.limit ?? 50;
    const offset = options.offset ?? 0;
    const { rows, total } = await listDocumentsByOrg(
      this.db,
      organizationId,
      { ...options, uploadedBy: userId, limit, offset }
    );
    return {
      items: rows.map(toView),
      pagination: { limit, offset, total, hasMore: offset + rows.length < total },
    };
  }

  /** A user's document detail; null when unknown, under another org, or another
   *  user's. */
  async get(organizationId: string, userId: string, id: string): Promise<DocumentView | null> {
    const row = await getDocumentByOrg(this.db, organizationId, id, userId);
    return row ? toView(row) : null;
  }

  /** Delete a user's document: the DB row first (children cascade), then the
   * stored object. Object deletion is best-effort by design — an unreachable
   * bucket must not resurrect a row the caller watched disappear. */
  async remove(organizationId: string, userId: string, id: string): Promise<void> {
    const deleted = await deleteDocument(this.db, organizationId, id, userId);
    if (!deleted) {
      throw new AppError("NOT_FOUND", "document not found", 404);
    }
    if (deleted.storage_key) {
      await this.storage.delete(deleted.storage_key);
    }
  }

  /** Ingestion + vectorization status with per-stage counts. */
  async status(
    organizationId: string,
    userId: string,
    id: string
  ): Promise<DocumentStatusView | null> {
    const row = await getDocumentStatus(this.db, organizationId, id, userId);
    return row ? toStatusView(row) : null;
  }
}