import type { DatabaseClient, DatabaseTransaction } from "../database/postgres.js";
import {
  deleteConversation,
  getConversationByOrg,
  insertConversation,
  insertMessage,
  listConversationsByOrg,
  listMessagesByConversation,
  type ConversationDetailRow,
  type ConversationListOptions,
  type ConversationRow,
  type MessageRow,
} from "../database/repositories/conversation.repository.js";
import { AppError } from "../utils/errors.js";
import type { RagOptions, RagResponse, RagService } from "./rag.service.js";

export interface ConversationView {
  id: string;
  organizationId: string;
  title: string;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
  messageCount: number;
  lastMessageAt: string | null;
  messages: MessageView[];
}

export interface ConversationList extends ConversationListOptions {
  items: ConversationView[];
  pagination: { limit: number; offset: number; total: number; hasMore: boolean };
}

export interface MessageView {
  id: string;
  conversationId: string;
  position: number;
  role: "user" | "assistant" | "system";
  content: string;
  payload: Record<string, unknown>;
  createdAt: string;
}

export interface AddMessageInput {
  content: string;
  rag: RagOptions;
  /** Pre-generated assistant turn from the streaming UI, persisted verbatim
   *  instead of re-running RAG on the server. */
  assistant?: {
    content: string;
    refused?: boolean;
    provider?: string;
    model?: string;
    citations?: {
      id: number;
      title?: string | null;
      section?: string | null;
      page?: number | null;
      chunkId?: string | null;
      documentId?: string | null;
      similarity?: number | null;
    }[];
    usage?: { promptTokens?: number; completionTokens?: number; totalTokens?: number };
  };
}

/** Whether a message content string is non-blank (route validation enforces a
 *  pattern too; this is the repository-level guard so a direct call can't
 *  store a blank user turn). */
function isBlank(value: string): boolean {
  return value.trim().length === 0;
}

function toMessageView(row: MessageRow): MessageView {
  return {
    id: row.id,
    conversationId: row.conversation_id,
    position: row.position,
    role: row.role as MessageView["role"],
    content: row.content,
    payload: row.payload,
    createdAt: row.created_at,
  };
}

function toConversationView(
  row: ConversationDetailRow,
  messages: MessageView[] = []
): ConversationView {
  return {
    id: row.id,
    organizationId: row.organization_id,
    title: row.title,
    createdBy: row.created_by,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    messageCount: row.message_count,
    lastMessageAt: row.last_message_at,
    messages,
  };
}

/** Conversations service: a tenant's chat sessions. `create`/`list`/`get`/
 * `remove` are plain tenant-scoped CRUD; `addMessage` is the feature — it runs
 * the RAG pipeline (with Day-20 conversation memory) and persists the user
 * turn + the assistant answer (with its citation provenance) as one atomic
 * pair. */
export class ConversationsService {
  constructor(
    private readonly db: DatabaseClient,
    private readonly rag: RagService,
    private readonly defaultMaxContextTokens = 1200,
    /** Day-20 context window: how many recent prior turns to carry into
     *  generation, and the token ceiling for that window. */
    private readonly maxHistoryMessages = 20,
    private readonly maxHistoryTokens = 800
  ) {}

  async create(
    organizationId: string,
    createdBy: string,
    title: string
  ): Promise<ConversationView> {
    const row = await insertConversation(this.db, {
      organizationId,
      title: title.trim() || "New conversation",
      createdBy,
    });
    return toConversationView({ ...row, message_count: 0, last_message_at: null });
  }

  async list(
    organizationId: string,
    options: ConversationListOptions = {}
  ): Promise<ConversationList> {
    const limit = options.limit ?? 50;
    const offset = options.offset ?? 0;
    const { rows, total } = await listConversationsByOrg(this.db, organizationId, {
      limit,
      offset,
    });
    return {
      items: rows.map((row) => toConversationView(row)),
      pagination: { limit, offset, total, hasMore: offset + rows.length < total },
    };
  }

  /** A tenant's conversation with its full transcript; null when unknown or
   *  under another org. */
  async get(
    organizationId: string,
    id: string
  ): Promise<ConversationView | null> {
    const row = await getConversationByOrg(this.db, organizationId, id);
    if (!row) return null;
    const messages = await listMessagesByConversation(this.db, id);
    return toConversationView(row, messages.map(toMessageView));
  }

  /** Ask the RAG pipeline and store the turn: user question + assistant answer
   *  (with citations) committed together. Returns the new transcript. RAG
   *  retrieval runs as `userId` so the answer grounds on the caller's own
   *  documents only (Day 23). */
  async addMessage(
    organizationId: string,
    userId: string,
    conversationId: string,
    input: AddMessageInput
  ): Promise<ConversationView> {
    if (isBlank(input.content)) {
      throw new AppError("VALIDATION_ERROR", "message content cannot be blank", 400);
    }

    const conversation = await getConversationByOrg(this.db, organizationId, conversationId);
    if (!conversation) {
      throw new AppError("NOT_FOUND", "conversation not found", 404);
    }

    // The streaming UI already generated this turn — persist it verbatim so
    // the transcript matches exactly what the user read, with no second RAG
    // pass (which would produce a divergent answer and charge a duplicate
    // generation). Otherwise run the RAG pipeline as before.
    let generation: {
      answer: string;
      refused: boolean;
      provider: string;
      model: string;
      citations: RagResponse["citations"];
      usage: RagResponse["usage"];
    };
    if (input.assistant?.content) {
      generation = {
        answer: input.assistant.content,
        refused: input.assistant.refused ?? false,
        provider: input.assistant.provider ?? "",
        model: input.assistant.model ?? "",
        citations: (input.assistant.citations ?? []).map((c) => ({
          id: c.id,
          title: c.title ?? null,
          section: c.section ?? null,
          page: c.page ?? null,
          chunkId: c.chunkId ?? null,
          documentId: c.documentId ?? null,
          similarity: c.similarity ?? null,
        })),
        usage: {
          promptTokens: input.assistant.usage?.promptTokens ?? 0,
          completionTokens: input.assistant.usage?.completionTokens ?? 0,
          totalTokens: input.assistant.usage?.totalTokens ?? 0,
        },
      };
    } else {
      // Day-20 conversation memory: the prior turns (before this question),
      // trimmed to a bounded window, become the history the RAG pipeline
      // rewrites referential follow-ups against and ships to generation.
      const priorMessages = (await listMessagesByConversation(this.db, conversationId)).map(
        toMessageView
      );
      const history = buildHistoryWindow(priorMessages, {
        maxMessages: this.maxHistoryMessages,
        maxTokens: this.maxHistoryTokens,
      });

      generation = await this.rag.generate(organizationId, userId, input.content, {
        ...input.rag,
        history,
        maxContextTokens: input.rag.maxContextTokens ?? this.defaultMaxContextTokens,
      });
    }

    // Persist user + assistant as one atomic unit with exact ordering. The
    // assistant message's payload carries the citation provenance so a client
    // can render "Sources [1] ..." straight off the stored turn.
    if (!this.db.connect) {
      throw new AppError("INTERNAL_ERROR", "conversation persistence unavailable", 500);
    }
    // NOTE: call connect() as a method of the db so `this` is preserved — pg's
    // Pool.connect relies on being invoked on the pool instance (a destructured
    // bare `connect()` once threw "reading 'ending' of undefined").
    const client = await this.db.connect();
    try {
      await client.query("BEGIN");
      const next = await nextPosition(client, conversationId);
      await insertMessage(client, {
        organizationId,
        conversationId,
        position: next,
        role: "user",
        content: input.content,
      });
      await insertMessage(client, {
        organizationId,
        conversationId,
        position: next + 1,
        role: "assistant",
        content: generation.answer,
        payload: {
          refused: generation.refused,
          provider: generation.provider,
          model: generation.model,
          citations: generation.citations,
          usage: generation.usage,
        },
      });
      // updated_at honesty: appending a turn marks the conversation active.
      await client.query(
        `UPDATE conversations SET updated_at = now() WHERE id = $1 AND organization_id = $2`,
        [conversationId, organizationId]
      );
      await client.query("COMMIT");
    } catch (error) {
      await safeRollback(client);
      throw error;
    } finally {
      client.release();
    }

    // Read the transcript from a released, committed connection (never the
    // transaction client) so the returned messages carry their full jsonb
    // payload exactly as a later GET would.
    const transcript = [...(await listMessagesByConversation(this.db, conversationId))].map(
      toMessageView
    );
    return toConversationView(
      { ...conversation, message_count: transcript.length },
      transcript
    );
  }

  async remove(organizationId: string, id: string): Promise<void> {
    const deleted = await deleteConversation(this.db, organizationId, id);
    if (!deleted) {
      throw new AppError("NOT_FOUND", "conversation not found", 404);
    }
  }
}

/** Next message position for a conversation (monotonic per-conversation so turn
 *  order is exact even when two rows share one created_at instant). */
async function nextPosition(db: DatabaseTransaction, conversationId: string): Promise<number> {
  const { rows } = await db.query(
    `SELECT COALESCE(MAX(position), 0) + 1 AS next FROM messages WHERE conversation_id = $1`,
    [conversationId]
  );
  return Number((rows[0] as { next: string }).next);
}

async function safeRollback(client: DatabaseTransaction): Promise<void> {
  try {
    await client.query("ROLLBACK");
  } catch {
    // connection already aborted the transaction; nothing to do
  }
}

/** Day 20 — context window management: the history handed to RAG generation.
 *  Keeps the **most recent** `maxMessages` prior turns (oldest first in the
 *  result), then, if that still exceeds `maxTokens`, drops the oldest turns
 *  until it fits — a sliding window that always favors recency, since a
 *  referential follow-up is about the latest exchange. Assistant-only reference
 *  frames are kept so pronouns resolve; every kept frame is role/content
 *  compatible with the generator request. */
export function buildHistoryWindow(
  priorMessages: MessageView[],
  options: { maxMessages: number; maxTokens: number }
): { role: "user" | "assistant"; content: string }[] {
  const windowed = priorMessages.slice(-options.maxMessages);
  let total = 0;
  const frames: { role: "user" | "assistant"; content: string }[] = [];
  // iterate newest-first so we can drop the oldest when over budget
  for (const message of [...windowed].reverse()) {
    if (message.role !== "user" && message.role !== "assistant") continue;
    const tokens = estimateMessageTokens(message.content);
    if (frames.length > 0 && total + tokens > options.maxTokens) continue;
    frames.push({ role: message.role, content: message.content });
    total += tokens;
  }
  return frames.reverse();
}

/** Cheap token estimate (words + punctuation runs), just to bound the window. */
function estimateMessageTokens(content: string): number {
  if (!content) return 0;
  const words = content.trim().split(/\s+/).length;
  return Math.max(1, Math.ceil(words * 1.3));
}
