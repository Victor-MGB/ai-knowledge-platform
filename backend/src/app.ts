import Fastify, {
  type FastifyError,
  type FastifyInstance,
  type FastifyPluginAsync,
} from "fastify";
import fastifyJwt from "@fastify/jwt";
import type { FastifyJWTOptions } from "@fastify/jwt";
import { randomUUID } from "node:crypto";

import { loadConfig, type Config, REFRESH_TTL_MS } from "./config/config.js";
import { createPool, type DatabaseClient } from "./database/postgres.js";
import { healthRoutes } from "./modules/health/health.routes.js";
import { echoRoutes } from "./modules/echo/echo.routes.js";
import { authRoutes } from "./modules/auth/auth.routes.js";
import { meRoutes } from "./modules/me/me.routes.js";
import { requestId } from "./middleware/request-id.js";
import { securityHeaders } from "./middleware/security-headers.js";
import { AuthService } from "./services/auth.service.js";
import { HealthService } from "./services/health.service.js";
import { PasswordService } from "./services/password.service.js";
import { isAppError } from "./utils/errors.js";

export interface BuildOptions {
  config?: Config;
  pool?: DatabaseClient;
}

/** Assemble the Fastify instance. Dependencies (config, db pool) can be
 * injected so tests run against fakes without a real database. */
export async function buildApp(options: BuildOptions = {}): Promise<FastifyInstance> {
  const config = options.config ?? loadConfig();
  const db = options.pool ?? createPool(config);

  const app = Fastify({
    logger: {
      level: config.LOG_LEVEL,
      ...(config.NODE_ENV !== "production"
        ? { transport: { target: "pino-pretty", options: { translateTime: "SYS:HH:MM:ss", levelFirst: true } } }
        : {}),
    },
    requestIdHeader: "x-request-id",
    genReqId: () => randomUUID(),
    // Fastify's default ajv (removeAdditional: true) silently strips unknown
    // payload fields; set strict so additionalProperties: false really rejects.
    ajv: { customOptions: { removeAdditional: false } },
  });

  app.decorate("config", config);
  app.decorate("db", db);
  app.decorate("healthService", new HealthService(db));

  setErrorHandlers(app);

  await app.register(requestId);
  await app.register(securityHeaders);

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

  await app.register(healthRoutes);
  await app.register(echoRoutes, { prefix: "/api/v1" });
  await app.register(authRoutes, { prefix: "/auth" });
  await app.register(meRoutes, { prefix: "/api/v1" });

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