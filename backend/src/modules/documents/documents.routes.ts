import type { FastifyPluginAsync } from "fastify";

import { requireAuth, requireRole } from "../../middleware/auth-guard.js";
import { AppError } from "../../utils/errors.js";
import {
  deleteDocumentSchema,
  documentStatusSchema,
  getDocumentSchema,
  listDocumentsSchema,
  type ListDocumentsQueryInput,
  uploadDocumentSchema,
} from "./documents.schema.js";

/** Document management under /api/v1/documents. Tenant is always taken from
 * the authenticated token (`request.user.org`), never from the client. Reads
 * are open to any member; deletion is owner-only (authorization). Every
 * unknown/foreign id answers NOT_FOUND so cross-tenant existence stays hidden.
 */
export const documentRoutes: FastifyPluginAsync = async (app) => {
  app.post(
    "/documents",
    { schema: uploadDocumentSchema, preHandler: [requireAuth] },
    async (request, reply) => {
      let file: { buffer: Buffer; filename: string } | null = null;
      let fileCount = 0;

      for await (const part of request.parts()) {
        if (part.type !== "file") continue; // swallow form values, files go first
        fileCount += 1;
        if (fileCount > 1) {
          throw new AppError("MULTIPLE_FILES", "send exactly one file per upload", 400);
        }
        file = { buffer: await part.toBuffer(), filename: part.filename ?? "" };
      }

      if (!file) {
        throw new AppError("FILE_REQUIRED", "a multipart file upload is required", 400);
      }

      const document = await app.documentsService.upload({
        filename: file.filename,
        buffer: file.buffer,
        organizationId: request.user.org,
        uploadedBy: request.user.sub,
      });
      return reply.code(201).send(document);
    }
  );

  app.get(
    "/documents",
    { schema: listDocumentsSchema, preHandler: [requireAuth] },
    async (request) => {
      const query = request.query as ListDocumentsQueryInput;
      // Day 23: a member lists only their own documents (uploadedBy = caller).
      return app.documentsService.listByOrganization(request.user.org, request.user.sub, {
        limit: Number(query.limit ?? 50),
        offset: Number(query.offset ?? 0),
        sourceType: query.sourceType,
        status: query.status,
        embeddingStatus: query.embeddingStatus,
        search: query.search,
      });
    }
  );

  app.get(
    "/documents/:id",
    { schema: getDocumentSchema, preHandler: [requireAuth] },
    async (request, reply) => {
      const { id } = request.params as { id: string };
      const document = await app.documentsService.get(request.user.org, request.user.sub, id);
      if (!document) {
        throw new AppError("NOT_FOUND", "document not found", 404);
      }
      return document;
    }
  );

  app.delete(
    "/documents/:id",
    { schema: deleteDocumentSchema, preHandler: [requireAuth, requireRole("owner")] },
    async (request, reply) => {
      const { id } = request.params as { id: string };
      await app.documentsService.remove(request.user.org, request.user.sub, id);
      return reply.code(204).send();
    }
  );

  app.get(
    "/documents/:id/status",
    { schema: documentStatusSchema, preHandler: [requireAuth] },
    async (request) => {
      const { id } = request.params as { id: string };
      const status = await app.documentsService.status(request.user.org, request.user.sub, id);
      if (!status) {
        throw new AppError("NOT_FOUND", "document not found", 404);
      }
      return status;
    }
  );
};