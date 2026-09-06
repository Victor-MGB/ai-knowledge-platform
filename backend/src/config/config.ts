import "dotenv/config";

import { z } from "zod";

const DEV_JWT_SECRET = "knowflow-dev-secret-change-me";

const envSchema = z
  .object({
    NODE_ENV: z.enum(["development", "test", "production"]).default("development"),
    HOST: z.string().default("0.0.0.0"),
    PORT: z.coerce.number().int().positive().default(3000),
    LOG_LEVEL: z
      .enum(["fatal", "error", "warn", "info", "debug", "trace", "silent"])
      .default("info"),
    DB_HOST: z.string().default("localhost"),
    DB_PORT: z.coerce.number().int().positive().default(5434),
    DB_USER: z.string().default("knowflow"),
    DB_PASSWORD: z.string().default("knowflow"),
    DB_NAME: z.string().default("knowflow"),
    JWT_SECRET: z.string().min(16).default(DEV_JWT_SECRET),
    JWT_ISSUER: z.string().default("knowflow"),
    JWT_EXPIRES_IN: z.string().default("15m"),
    REFRESH_TTL_DAYS: z.coerce.number().int().positive().default(30),
    BCRYPT_ROUNDS: z.coerce.number().int().min(4).max(15).default(10),
    // Day 22: how long an invitation token stays valid before it rots.
    INVITATION_TTL_DAYS: z.coerce.number().int().positive().default(7),
    // S3-compatible storage (MinIO locally). Defaults match the Day-8 dev
    // container; production must override the credentials before exposing it.
    S3_ENDPOINT: z.string().url().default("http://localhost:9000"),
    S3_REGION: z.string().default("us-east-1"),
    S3_BUCKET: z.string().default("knowflow"),
    S3_ACCESS_KEY: z.string().default("knowflow"),
    S3_SECRET_KEY: z.string().min(8).default("knowflow123"),
    S3_FORCE_PATH_STYLE: z.coerce.boolean().default(true),
    MAX_UPLOAD_BYTES: z.coerce.number().int().positive().default(25 * 1024 * 1024),
    // Day 12: the AI service that owns the ingestion queue. The backend calls
    // its /v1/queue/documents/{id}/process after an upload; a dead/mercurial
    // queue must never break the upload, so the call is best-effort with a
    // short timeout and the document simply stays `queued` for a later sweep.
    AI_SERVICE_URL: z.string().url().default("http://127.0.0.1:8001"),
    AI_ENQUEUE_TIMEOUT_MS: z.coerce.number().int().positive().default(5000),
    // Day 15: retrieval embeds the query via the AI service before searching;
    // this bounds that call so a hung service can't hang the search either.
    AI_EMBED_TIMEOUT_MS: z.coerce.number().int().positive().default(5000),
    // Day 17: RAG generation calls the AI service's /v1/rag/generate with the
    // question + retrieved context after uploading the query embedding; a real
    // LLM can be slow, so this timeout is more generous than the embed one.
    AI_RAG_TIMEOUT_MS: z.coerce.number().int().positive().default(15000),
    // Day 17: how many context tokens a RAG answer may draw on (soft cap —
    // the strongest chunk always survives). Matches the AI service default.
    AI_RAG_MAX_CONTEXT_TOKENS: z.coerce.number().int().positive().default(1200),
    // Day 29: CORS. The SPA is same-origin in dev (vite) and in docker (nginx),
    // so this is off by default and only needs a value for a genuinely
    // cross-origin browser client. Comma-separated allow-list of origins.
    CORS_ORIGINS: z.string().default(""),
    // Day 29: brute-force protection. Per-IP/`x-forwarded-for` request windows,
    // applied globally as a baseline and tightened on the public auth routes.
    RATE_LIMIT_MAX: z.coerce.number().int().positive().default(100),
    RATE_LIMIT_TIME_WINDOW_MS: z.coerce.number().int().positive().default(60_000),
    // Public auth endpoints (register/login/refresh/invitation-accept) are the
    // brute-force / credential-stuffing surface: a much tighter allowance.
    AUTH_RATE_LIMIT_MAX: z.coerce.number().int().positive().default(10),
    AUTH_RATE_LIMIT_WINDOW_MS: z.coerce.number().int().positive().default(60_000),
  })
  .superRefine((value, ctx) => {
    // Refuse to boot production on the obvious dev secret or a weak one.
    if (
      value.NODE_ENV === "production" &&
      (value.JWT_SECRET === DEV_JWT_SECRET || value.JWT_SECRET.length < 32)
    ) {
      ctx.addIssue({
        code: "custom",
        path: ["JWT_SECRET"],
        message: "production requires a strong JWT_SECRET (>= 32 chars)",
      });
    }
  });

export type Config = z.infer<typeof envSchema>;

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const parsed = envSchema.safeParse(env);
  if (!parsed.success) {
    const details = JSON.stringify(parsed.error.issues, null, 2);
    throw new Error(`invalid environment configuration:\n${details}`);
  }
  return parsed.data;
}

/** postgres://user:password@host:port/database */
export function databaseUrl(config: Config): string {
  return `postgres://${config.DB_USER}:${config.DB_PASSWORD}@${config.DB_HOST}:${config.DB_PORT}/${config.DB_NAME}`;
}

export const REFRESH_TTL_MS = (config: Config): number =>
  config.REFRESH_TTL_DAYS * 24 * 60 * 60 * 1000;

export const INVITATION_TTL_MS = (config: Config): number =>
  config.INVITATION_TTL_DAYS * 24 * 60 * 60 * 1000;