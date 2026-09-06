import type { DatabaseClient } from "../database/postgres.js";
import {
  getDocumentByOrg,
  getDocumentStatus,
} from "../database/repositories/document.repository.js";

/** Day 18 — one citation's source document, resolved from a RAG citation's
 * `documentId`. Returns the source metadata (title, filename, type) plus how
 * many pages the document actually contains, so a client rendering
 * "Sources [1] Employee Handbook — Page 14" has the document-level facts to
 * pair with the per-page citation the RAG answer already carries. */
export interface SourceView {
  document: {
    id: string;
    title: string;
    filename: string;
    sourceType: string;
    size: number;
    createdAt: string;
  };
  pages: number;
}

export class SourcesService {
  constructor(private readonly db: DatabaseClient) {}

  /** A user's source by document id (within their tenant + ownership). null when
   * unknown, under another org, or another user's (callers 404 it, never
   * distinguishing, so existence stays hidden). */
  async get(organizationId: string, userId: string, documentId: string): Promise<SourceView | null> {
    const row = await getDocumentByOrg(this.db, organizationId, documentId, userId);
    if (!row) {
      return null;
    }
    const status = await getDocumentStatus(this.db, organizationId, documentId, userId);
    return {
      document: {
        id: row.id,
        title: row.title,
        filename: row.filename,
        sourceType: row.source_type,
        size: row.size,
        createdAt: row.created_at,
      },
      pages: status ? Number(status.pages) : 0,
    };
  }
}
