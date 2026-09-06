import type { DatabaseClient } from "../postgres.js";

export interface DocumentRow {
  id: string;
  organization_id: string;
  uploaded_by: string;
  title: string;
  filename: string;
  mime_type: string;
  size: number;
  source_type: string;
  storage_key: string | null;
  status: string;
  error: string | null;
  embedding_status: string;
  embedding_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface DocumentListOptions {
  limit?: number;
  offset?: number;
  sourceType?: string;
  status?: string;
  embeddingStatus?: string;
  search?: string;
  /** Day 23 — restrict the listing to documents a specific user uploaded, so a
   *  member only ever sees their own documents (not a colleague's). */
  uploadedBy?: string;
}

export interface DocumentListResult {
  rows: DocumentRow[];
  total: number;
}

export interface DocumentStatusRow extends DocumentRow {
  pages: number;
  chunks: number;
  embeddings: number;
}

const DOCUMENT_COLUMNS = `id, organization_id, uploaded_by, title, filename, mime_type,
            size, source_type, storage_key, status, error, embedding_status,
            embedding_error, created_at, updated_at`;

type Executable = DatabaseClient;

/** Insert an uploaded document in the 'queued' state (ingestion workers flip
 * it to processing/ready in Week 2). */
export async function insertDocument(
  db: Executable,
  params: {
    organizationId: string;
    uploadedBy: string;
    title: string;
    filename: string;
    mimeType: string;
    size: number;
    sourceType: string;
    storageKey: string;
  }
): Promise<DocumentRow> {
  const { rows } = await db.query(
    `INSERT INTO documents
       (organization_id, uploaded_by, title, filename, mime_type, size, source_type, storage_key, status)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'queued')
     RETURNING ${DOCUMENT_COLUMNS}
       `,
    [
      params.organizationId,
      params.uploadedBy,
      params.title,
      params.filename,
      params.mimeType,
      params.size,
      params.sourceType,
      params.storageKey,
    ]
  );
  return rows[0] as DocumentRow;
}

/** A user's document by id, within their tenant. NULL when the id is unknown,
 * belongs to another organization, OR was uploaded by another user — callers
 * must not distinguish, so neither cross-tenant nor cross-user existence leaks. */
export async function getDocumentByOrg(
  db: Executable,
  organizationId: string,
  id: string,
  uploadedBy: string
): Promise<DocumentRow | null> {
  const { rows } = await db.query(
    `SELECT ${DOCUMENT_COLUMNS}
     FROM documents
     WHERE id = $1 AND organization_id = $2 AND uploaded_by = $3`,
    [id, organizationId, uploadedBy]
  );
  return (rows[0] as DocumentRow | undefined) ?? null;
}

/** Paginated, filterable tenant document list, newest first. `total` counts
 * every row matching the filters (window function, same statement — no second
 * query, no read-you-own-write hazard) so the API can return hasMore honestly. */
export async function listDocumentsByOrg(
  db: Executable,
  organizationId: string,
  options: DocumentListOptions = {}
): Promise<DocumentListResult> {
  const conditions = ["organization_id = $1"];
  const params: unknown[] = [organizationId];
  let index = 2;

  if (options.uploadedBy) {
    conditions.push(`uploaded_by = $${index++}`);
    params.push(options.uploadedBy);
  }

  if (options.sourceType) {
    conditions.push(`source_type = $${index++}`);
    params.push(options.sourceType);
  }
  if (options.status) {
    conditions.push(`status = $${index++}`);
    params.push(options.status);
  }
  if (options.embeddingStatus) {
    conditions.push(`embedding_status = $${index++}`);
    params.push(options.embeddingStatus);
  }
  if (options.search) {
    conditions.push(`(title ILIKE $${index} OR filename ILIKE $${index})`);
    params.push(`%${escapeLike(options.search)}%`);
    index += 1;
  }

  const limit = options.limit ?? 50;
  const offset = options.offset ?? 0;
  params.push(limit, offset);
  const limitParam = `$${index++}`;
  const offsetParam = `$${index}`;

  const { rows } = await db.query(
    `SELECT ${DOCUMENT_COLUMNS},
            count(*) OVER() AS matching_total
     FROM documents
     WHERE ${conditions.join(" AND ")}
     ORDER BY created_at DESC
     LIMIT ${limitParam} OFFSET ${offsetParam}`,
    params
  );
  const typed = rows as Array<DocumentRow & { matching_total: string }>;
  return {
    rows: typed.map(({ matching_total: _total, ...row }) => row),
    total: typed.length > 0 ? Number(typed[0]?.matching_total ?? 0) : 0,
  };
}

/** Delete a user's document (ownership + tenant scoped), returning its storage
 * key so the caller can clean up the object. Children cascade (pages/chunks/
 * embeddings/ingestion_jobs all reference documents ON DELETE CASCADE). NULL
 * when the document does not exist / is in another org / is another user's. */
export async function deleteDocument(
  db: Executable,
  organizationId: string,
  id: string,
  uploadedBy: string
): Promise<{ storage_key: string | null } | null> {
  const { rows } = await db.query(
    `DELETE FROM documents
     WHERE id = $1 AND organization_id = $2 AND uploaded_by = $3
     RETURNING storage_key`,
    [id, organizationId, uploadedBy]
  );
  return (rows[0] as { storage_key: string | null } | undefined) ?? null;
}

/** Document + pipeline counts: how far ingestion actually got (pages/chunks/
 * embeddings) and the lifecycles that failed it, for the status endpoint.
 * Scoped to the caller's tenant AND ownership so a member cannot probe a
 * colleague's document's ingestion state. */
export async function getDocumentStatus(
  db: Executable,
  organizationId: string,
  id: string,
  uploadedBy: string
): Promise<DocumentStatusRow | null> {
  const { rows } = await db.query(
    `SELECT ${DOCUMENT_COLUMNS},
            (SELECT count(*) FROM document_pages p WHERE p.document_id = d.id) AS pages,
            (SELECT count(*) FROM chunks c WHERE c.document_id = d.id) AS chunks,
            (SELECT count(*) FROM embeddings e
               JOIN chunks c2 ON c2.id = e.chunk_id
             WHERE c2.document_id = d.id) AS embeddings
     FROM documents d
     WHERE d.id = $1 AND d.organization_id = $2 AND d.uploaded_by = $3`,
    [id, organizationId, uploadedBy]
  );
  return (rows[0] as DocumentStatusRow | undefined) ?? null;
}

/** Escape LIKE wildcards so a user search term matches literally. */
function escapeLike(value: string): string {
  return value.replace(/[\\%_]/g, (char) => `\\${char}`);
}