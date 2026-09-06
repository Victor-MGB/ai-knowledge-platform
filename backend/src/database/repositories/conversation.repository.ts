import type { DatabaseClient, DatabaseTransaction } from "../postgres.js";

export interface ConversationRow {
  id: string;
  organization_id: string;
  title: string;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface MessageRow {
  id: string;
  organization_id: string;
  conversation_id: string;
  position: number;
  role: string;
  content: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface ConversationDetailRow extends ConversationRow {
  message_count: number;
  last_message_at: string | null;
}

const CONVERSATION_COLUMNS = `id, organization_id, title, created_by, created_at, updated_at`;
const MESSAGE_COLUMNS = `id, organization_id, conversation_id, position, role, content, payload, created_at`;

type Executable = DatabaseClient | DatabaseTransaction;

export interface ConversationListOptions {
  limit?: number;
  offset?: number;
}

export interface ConversationListResult {
  rows: ConversationDetailRow[];
  total: number;
}

/** Create a conversation for a tenant, owned by the caller. */
export async function insertConversation(
  db: Executable,
  params: { organizationId: string; title: string; createdBy: string }
): Promise<ConversationRow> {
  const { rows } = await db.query(
    `INSERT INTO conversations (organization_id, title, created_by)
     VALUES ($1, $2, $3)
     RETURNING ${CONVERSATION_COLUMNS}`,
    [params.organizationId, params.title, params.createdBy]
  );
  return rows[0] as ConversationRow;
}

/** A tenant's conversation by id. NULL when unknown OR under another org —
 *  callers must not distinguish, so cross-tenant existence stays hidden. */
export async function getConversationByOrg(
  db: Executable,
  organizationId: string,
  id: string
): Promise<ConversationDetailRow | null> {
  const { rows } = await db.query(
    `SELECT c.id, c.organization_id, c.title, c.created_by, c.created_at, c.updated_at,
            (SELECT count(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count,
            (SELECT max(m2.created_at) FROM messages m2 WHERE m2.conversation_id = c.id) AS last_message_at
     FROM conversations c
     WHERE c.id = $1 AND c.organization_id = $2`,
    [id, organizationId]
  );
  const row = rows[0] as
    | (ConversationRow & { message_count: string; last_message_at: string | null })
    | undefined;
  if (!row) return null;
  return {
    ...row,
    message_count: Number(row.message_count),
  };
}

/** Paginated tenant conversation list, most-recently-active first. `total` is a
 *  window count in the same statement (no second query / read-you-own-write). */
export async function listConversationsByOrg(
  db: Executable,
  organizationId: string,
  options: ConversationListOptions = {}
): Promise<ConversationListResult> {
  const limit = options.limit ?? 50;
  const offset = options.offset ?? 0;
  const { rows } = await db.query(
    `SELECT c.id, c.organization_id, c.title, c.created_by, c.created_at, c.updated_at,
            (SELECT count(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count,
            (SELECT max(m2.created_at) FROM messages m2 WHERE m2.conversation_id = c.id) AS last_message_at,
            count(*) OVER() AS matching_total
     FROM conversations c
     WHERE c.organization_id = $1
     ORDER BY updated_at DESC, created_at DESC
     LIMIT $2 OFFSET $3`,
    [organizationId, limit, offset]
  );
  const typed = rows as Array<
    ConversationRow & { message_count: string; last_message_at: string | null; matching_total: string }
  >;
  return {
    rows: typed.map(({ matching_total: _t, message_count, ...row }) => ({
      ...row,
      message_count: Number(message_count),
    })),
    total: typed.length > 0 ? Number(typed[0]?.matching_total ?? 0) : 0,
  };
}

/** Delete a tenant's conversation; messages cascade with it. NULL when it does
 *  not exist in this organization. */
export async function deleteConversation(
  db: Executable,
  organizationId: string,
  id: string
): Promise<boolean> {
  const { rows } = await db.query(
    `DELETE FROM conversations
     WHERE id = $1 AND organization_id = $2
     RETURNING id`,
    [id, organizationId]
  );
  return rows.length > 0;
}

/** All of a conversation's messages, oldest first — the order the turns
 *  actually happened (exact via the monotonic position ordinal). */
export async function listMessagesByConversation(
  db: Executable,
  conversationId: string
): Promise<MessageRow[]> {
  const { rows } = await db.query(
    `SELECT ${MESSAGE_COLUMNS}
     FROM messages
     WHERE conversation_id = $1
     ORDER BY position ASC`,
    [conversationId]
  );
  return (rows as MessageRow[]).map((row) => ({
    ...row,
    payload: row.payload ?? {},
  }));
}

/** Insert one message at the next position for its conversation. */
export async function insertMessage(
  db: Executable,
  params: {
    organizationId: string;
    conversationId: string;
    position: number;
    role: string;
    content: string;
    payload?: Record<string, unknown>;
  }
): Promise<MessageRow> {
  const { rows } = await db.query(
    `INSERT INTO messages (organization_id, conversation_id, position, role, content, payload)
     VALUES ($1, $2, $3, $4, $5, $6)
     RETURNING ${MESSAGE_COLUMNS}`,
    [
      params.organizationId,
      params.conversationId,
      params.position,
      params.role,
      params.content,
      JSON.stringify(params.payload ?? {}),
    ]
  );
  return rows[0] as MessageRow;
}
