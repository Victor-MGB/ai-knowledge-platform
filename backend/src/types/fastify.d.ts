import type { Config } from "../config/config.js";
import type { DatabaseClient } from "../database/postgres.js";
import type { AuthService } from "../services/auth.service.js";
import type { HealthService } from "../services/health.service.js";
import type { AccessTokenPayload } from "../services/token.service.js";

declare module "fastify" {
  interface FastifyInstance {
    config: Config;
    db: DatabaseClient;
    healthService: HealthService;
    authService: AuthService;
  }
}

declare module "@fastify/jwt" {
  interface FastifyJWT {
    payload: AccessTokenPayload;
    user: AccessTokenPayload;
  }
}