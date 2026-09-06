import Fastify, {
  type FastifyError,
  type FastifyInstance,
  type FastifyPluginAsync,
  type FastifyServerOptions,
} from "fastify";
import fastifyJwt from "@fastify/jwt";
import type { FastifyJWTOptions } from "@fastify/jwt";
import fastifyMultipart from "@fastify/multipart";
import { randomUUID } from "node:crypto";

import { loadConfig, type Config, INVITATION_TTL_MS, REFRESH_TTL_MS } from "./config/config.js";
import { createPool, type DatabaseClient } from "./database/postgres.js";
import { healthRoutes } from "./modules/health/health.routes.js";
import { echoRoutes } from "./modules/echo/echo.routes.js";
import { authRoutes } from "./modules/auth/auth.routes.js";
import { meRoutes } from "./modules/me/me.routes.js";
import { documentRoutes } from "./modules/documents/documents.routes.js";
import { searchRoutes } from "./modules/search/search.routes.js";
import { ragRoutes } from "./modules/rag/rag.routes.js";
import { sourceRoutes } from "./modules/sources/sources.routes.js";
import { conversationRoutes } from "./modules/conversations/conversations.routes.js";
import { organizationRoutes } from "./modules/organizations/organizations.routes.js";
import { requestId } from "./middleware/request-id.js";
import { securityHeaders } from "./middleware/security-headers.js";
import { cors } from "./middleware/cors.js";
import { rateLimit } from "./middleware/rate-limit.js";
import { metrics } from "./middleware/metrics.js";
import { AuthService } from "./services/auth.service.js";
import { DocumentsService } from "./services/documents.service.js";
import { FileValidationService } from "./services/file-validation.service.js";
import { HealthService } from "./services/health.service.js";
import { PasswordService } from "./services/password.service.js";
import { S3StorageClient, type StorageClient } from "./services/storage.service.js";
import {
  HttpEnqueueService,
  type EnqueueService,
} from "./services/enqueue.service.js";
import {
  HttpQueryEmbedder,
  type QueryEmbedder,
} from "./services/embedding-client.service.js";
import {
  HttpRagGenerator,
  type RagGenerator,
} from "./services/rag-generator-client.service.js";
import { RagService } from "./services/rag.service.js";
import { SearchService } from "./services/search.service.js";
import { SourcesService } from "./services/sources.service.js";
import { ConversationsService } from "./services/conversations.service.js";
import { OrganizationService } from "./services/organization.service.js";
import { isAppError } from "./utils/errors.js";

export interface BuildOptions {
  config?: Config;
  pool?: DatabaseClient;
  storage?: StorageClient;
  enqueuer?: EnqueueService;
  embedder?: QueryEmbedder;
  ragGenerator?: RagGenerator;
}

/** Assemble the Fastify instance. Dependencies (config, db pool) can be
 * injected so tests run against fakes without a real database. */
export async function buildApp(options: BuildOptions = {}): Promise<FastifyInstance> {
  const config = options.config ?? loadConfig();
  const db = options.pool ?? createPool(config);
  const storage =
    options.storage ??
    new S3StorageClient({
      endpoint: config.S3_ENDPOINT,
      region: config.S3_REGION,
      bucket: config.S3_BUCKET,
      accessKey: config.S3_ACCESS_KEY,
      secretKey: config.S3_SECRET_KEY,
      forcePathStyle: config.S3_FORCE_PATH_STYLE,
    });

  const serverOptions: FastifyServerOptions = {
    logger: {
      level: config.LOG_LEVEL,
      serializers: {
        // keep error stacks tidy and typed instead of pino's default Objects
        err: (error: FastifyError) => ({
          type: "Error",
          code: error.code,
          message: error.message,
          stack: error.stack ?? "",
        }),
      },
      ...(config.NODE_ENV !== "production"
        ? { transport: { target: "pino-pretty", options: { translateTime: "SYS:HH:MM:ss", levelFirst: true } } }
        : {}),
    },
    // Day 30: one structured access line per request (with org/user/latency)
    // instead of Fastify's default start+completed pair. See the onRequest /
    // onResponse hooks below.
    disableRequestLogging: true,
    requestIdHeader: "x-request-id",
    genReqId: () => randomUUID(),
    // Fastify's default ajv (removeAdditional: true) silently strips unknown
    // payload fields; set strict so additionalProperties: false really rejects.
    ajv: { customOptions: { removeAdditional: false } },
  };
  const app = Fastify(serverOptions);

  // Day 30: structured access logging — one JSON line per request carrying the
  // method, matched route, status, latency and (post-auth) caller identity, so
  // a trace can be followed by x-request-id with tenant attribution on every
  // line. This complements the /metrics endpoint: logs answer "which requests",
  // metrics answer "how many/how slow/how broken".
  app.addHook("onRequest", async (request) => {
    request.log.info(
      {
        req: {
          method: request.method,
          url: request.url,
          route: request.routeOptions?.url ?? "unmatched",
        },
      },
      "request started"
    );
  });

  app.addHook("onResponse", async (request, reply) => {
    request.log.info(
      {
        org: request.user?.org ?? null,
        user: request.user?.sub ?? null,
        statusCode: reply.statusCode,
        durationMs: reply.elapsedTime,
      },
      "request completed"
    );
  });

  app.decorate("config", config);
  app.decorate("db", db);
  app.decorate("healthService", new HealthService(db));
  app.decorate("storageService", storage);

  setErrorHandlers(app);

  await app.register(requestId);
  await app.register(securityHeaders);
  await app.register(cors);
  await app.register(rateLimit);
  await app.register(metrics);

  // default-import interop quirk: the CJS plugin resolves to a module
  // namespace, not a function - cast to the plugin signature we register.
  const jwtPlugin = fastifyJwt as unknown as FastifyPluginAsync<FastifyJWTOptions>;
  await app.register(jwtPlugin, {
    secret: config.JWT_SECRET,
    // fast-jwt (used by @fastify/jwt v9) names the claim option `iss`, and
    // issuer verification is `allowedIss`.
    sign: { iss: config.JWT_ISSUER, expiresIn: config.JWT_EXPIRES_IN },
    verify: { allowedIss: config.JWT_ISSUER },
  });

  const multipartPlugin =
    fastifyMultipart as unknown as FastifyPluginAsync<{ limits?: Record<string, unknown> }>;
  await app.register(multipartPlugin, {
    limits: { fileSize: config.MAX_UPLOAD_BYTES, files: 1 },
  });

  app.decorate(
    "authService",
    new AuthService(
      db,
      new PasswordService(config.BCRYPT_ROUNDS),
      (user) =>
        app.jwt.sign({
          sub: user.id,
          org: user.organization_id,
          role: user.role,
          typ: "access",
          // unique token id so the same user can hold many concurrent sessions
          jti: randomUUID(),
        }),
      REFRESH_TTL_MS(config)
    )
  );

  app.decorate(
    "documentsService",
    new DocumentsService(
      db,
      storage,
      new FileValidationService(config.MAX_UPLOAD_BYTES),
      // Day 12: after every upload, ask the AI service to queue the document.
      // The service swallows failures itself, but tests override to a fake.
      options.enqueuer ??
        new HttpEnqueueService({
          baseUrl: config.AI_SERVICE_URL,
          timeoutMs: config.AI_ENQUEUE_TIMEOUT_MS,
        })
    )
  );

  app.decorate(
    "searchService",
    new SearchService(
      db,
      // Day 15: query vectors come from the AI service's embedding endpoint
      // (the model that indexed the corpus). Tests inject a fake embedder.
      options.embedder ??
        new HttpQueryEmbedder({
          baseUrl: config.AI_SERVICE_URL,
          timeoutMs: config.AI_EMBED_TIMEOUT_MS,
        })
    )
  );

  app.decorate(
    "ragService",
    new RagService(
      app.searchService,
      // Day 17: answers come from the AI service's RAG generation endpoint,
      // which owns prompt assembly + the "I don't know" guardrails. Tests
      // inject a fake generator.
      options.ragGenerator ??
        new HttpRagGenerator({
          baseUrl: config.AI_SERVICE_URL,
          timeoutMs: config.AI_RAG_TIMEOUT_MS,
        }),
      config.AI_RAG_MAX_CONTEXT_TOKENS
    )
  );

  // Day 18: the Source API resolves a RAG citation's documentId back to the
  // source document. Pure DB read, tenant-scoped.
  app.decorate("sourcesService", new SourcesService(db));

  // Day 19: conversations — tenant chat sessions built on the RAG pipeline.
  // addMessage runs RagService.generate and stores the user + assistant turns.
  app.decorate(
    "conversationsService",
    new ConversationsService(db, app.ragService)
  );

  // Day 22: organizations — tenant profile, membership (role changes), and
  // invitations (create/list/revoke/accept) so a team can grow.
  app.decorate(
    "organizationService",
    new OrganizationService(db, new PasswordService(config.BCRYPT_ROUNDS), INVITATION_TTL_MS(config))
  );

  await app.register(healthRoutes);
  await app.register(echoRoutes, { prefix: "/api/v1" });
  await app.register(authRoutes, { prefix: "/auth" });
  await app.register(meRoutes, { prefix: "/api/v1" });
  await app.register(documentRoutes, { prefix: "/api/v1" });
  await app.register(searchRoutes, { prefix: "/api/v1" });
  await app.register(ragRoutes, { prefix: "/api/v1" });
  await app.register(sourceRoutes, { prefix: "/api/v1" });
  await app.register(conversationRoutes, { prefix: "/api/v1" });
  await app.register(organizationRoutes, { prefix: "/api/v1" });

  app.get("/", async () => ({
    name: "knowflow-backend",
    version: "0.1.0",
  }));

  app.addHook("onClose", async () => {
    await db.end?.();
  });

  return app;
}

/** Central error + 404 handlers: every failure leaves as a JSON envelope and
 * leaks no internal detail outside development. */
function setErrorHandlers(app: FastifyInstance): void {
  app.setNotFoundHandler((request, reply) => {
    reply.status(404).send({
      error: { code: "NOT_FOUND", message: `route ${request.method} ${request.url} not found` },
    });
  });

  app.setErrorHandler(
    (error: FastifyError, request, reply) => {
      request.log.error({ err: error }, "request failed");

      // multipart plugin raises stream-level errors before routes run
      if (error.code === "FST_REQ_FILE_TOO_LARGE") {
        return reply.status(413).send({
          error: {
            code: "FILE_TOO_LARGE",
            message: error.message ?? "upload exceeds the size limit",
          },
        });
      }
      if (error.code === "FST_PARTS_LIMIT_PART_FILES") {
        return reply.status(400).send({
          error: { code: "MULTIPLE_FILES", message: "send exactly one file per upload" },
        });
      }

      if (Array.isArray((error as { validation?: unknown[] }).validation)) {
        return reply.status(400).send({
          error: {
            code: "VALIDATION_ERROR",
            message: "request failed validation",
            details: (error as { validation?: unknown[] }).validation,
          },
        });
      }

      if (isAppError(error)) {
        return reply.status(error.statusCode).send({
          error: { code: error.code, message: error.message, details: error.details },
        });
      }

      // Security: the rate limiter rejects with a structured 429 payload that
      // fastify surfaces as an error object here. Honor its statusCode rather
      // than collapsing it into the generic 500 below, so a genuinely
      // rate-limited client still sees a "too many requests" response.
      if (error.statusCode === 429) {
        const structured = (
          error as { error?: { code?: string; message?: string } }
        ).error;
        return reply.status(429).send({
          error: {
            code: structured?.code ?? "RATE_LIMITED",
            message: structured?.message ?? "rate limit exceeded",
          },
        });
      }

      const isProd = app.config.NODE_ENV === "production";
      return reply.status(500).send({
        error: {
          code: "INTERNAL_ERROR",
          message: isProd ? "internal server error" : error.message ?? "internal server error",
          details: isProd ? undefined : error.stack,
        },
      });
    }
  );
}