import type { FastifyPluginAsync } from "fastify";

import { requireAuth } from "../../middleware/auth-guard.js";
import { type RagBodyInput, ragSchema, ragStreamSchema } from "./rag.schema.js";

/** Day 17: RAG generation — `POST /api/v1/rag/generate`. Question in, answer
 * out (possibly the canonical "I don't know." with `refused=true`). Tenant
 * scope comes from the token, exactly like /search; the AI service does the
 * formatting/prompting/generation over the context this route ships. */
export const ragRoutes: FastifyPluginAsync = async (app) => {
  app.post(
    "/rag/generate",
    { schema: ragSchema, preHandler: [requireAuth] },
    async (request) => {
      const body = request.body as RagBodyInput;
      return app.ragService.generate(request.user.org, request.user.sub, body.question.trim(), {
        limit: body.limit,
        maxContextTokens: body.maxContextTokens,
        minScore: body.minScore,
        sourceType: body.sourceType,
        metadata: body.metadata,
      });
    }
  );

  /** Day 27 streaming — `POST /api/v1/rag/generate/stream`. Identical body to
   * /rag/generate, but the reply is a `text/event-stream`: `data:` deltas with
   * the answer tokens as they arrive, then a final `done` event carrying the
   * complete generation (citations/evidence/refused). No response schema
   * (the streamed shape doesn't validate like a normal JSON body). */
  app.post(
    "/rag/generate/stream",
    { schema: ragStreamSchema, preHandler: [requireAuth] },
    async (request, reply) => {
      const body = request.body as RagBodyInput;

      reply.raw.writeHead(200, {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
        "X-Accel-Buffering": "no",
      });

      const stream = app.ragService.generateStream(
        request.user.org,
        request.user.sub,
        body.question.trim(),
        {
          limit: body.limit,
          maxContextTokens: body.maxContextTokens,
          minScore: body.minScore,
          sourceType: body.sourceType,
          metadata: body.metadata,
        }
      );

      for await (const event of stream) {
        if (event.type === "delta") {
          reply.raw.write(`data: ${JSON.stringify({ delta: event.text })}\n\n`);
        } else {
          const g = event.generation;
          reply.raw.write(
            `data: ${JSON.stringify({
              done: {
                answer: g.answer,
                refused: g.refused,
                provider: g.provider,
                model: g.model,
                evidence: g.evidence,
                citations: g.citations,
                usage: g.usage,
              },
            })}\n\n`
          );
          reply.raw.write(`data: [DONE]\n\n`);
        }
      }
      reply.raw.end();
    }
  );
};