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