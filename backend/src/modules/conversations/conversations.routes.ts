import type { FastifyPluginAsync } from "fastify";

import { requireAuth } from "../../middleware/auth-guard.js";
import { AppError } from "../../utils/errors.js";
import {
  addMessageSchema,
  createConversationSchema,
  deleteConversationSchema,
  getConversationSchema,
  listConversationsSchema,
  type AddMessageInput,
  type CreateConversationInput,
} from "./conversations.schema.js";

/** Conversations under /api/v1/conversations (Day 19). A tenant's chat
 *  sessions and their transcripts. Tenant scope comes from the token, never
 *  the client; an unknown/foreign conversation answers NOT_FOUND like every
 *  other read. Reads are open to any member; the RAG-powered message endpoint
 *  is the feature. */
export const conversationRoutes: FastifyPluginAsync = async (app) => {
  app.post(
    "/conversations",
    { schema: createConversationSchema, preHandler: [requireAuth] },
    async (request, reply) => {
      const body = (request.body ?? {}) as CreateConversationInput;
      const conversation = await app.conversationsService.create(
        request.user.org,
        request.user.sub,
        body.title ?? ""
      );
      return reply.code(201).send(conversation);
    }
  );

  app.get(
    "/conversations",
    { schema: listConversationsSchema, preHandler: [requireAuth] },
    async (request) => {
      const query = request.query as { limit?: number | string; offset?: number | string };
      return app.conversationsService.list(request.user.org, {
        limit: Number(query.limit ?? 50),
        offset: Number(query.offset ?? 0),
      });
    }
  );

  app.get(
    "/conversations/:id",
    { schema: getConversationSchema, preHandler: [requireAuth] },
    async (request, reply) => {
      const { id } = request.params as { id: string };
      const conversation = await app.conversationsService.get(request.user.org, id);
      if (!conversation) {
        throw new AppError("NOT_FOUND", "conversation not found", 404);
      }
      return reply.code(200).send(conversation);
    }
  );

  app.post(
    "/conversations/:id/messages",
    { schema: addMessageSchema, preHandler: [requireAuth] },
    async (request, reply) => {
      const { id } = request.params as { id: string };
      const body = request.body as AddMessageInput;
      const conversation = await app.conversationsService.addMessage(
        request.user.org,
        request.user.sub,
        id,
        {
          content: body.content,
          rag: body.rag ?? {},
        }
      );
      return reply.code(201).send(conversation);
    }
  );

  app.delete(
    "/conversations/:id",
    { schema: deleteConversationSchema, preHandler: [requireAuth] },
    async (request, reply) => {
      const { id } = request.params as { id: string };
      await app.conversationsService.remove(request.user.org, id);
      return reply.code(204).send();
    }
  );
};
