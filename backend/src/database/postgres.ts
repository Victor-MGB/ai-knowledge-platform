import pg from "pg";

import type { Config } from "../config/config.js";

const { Pool } = pg;

/** Minimal surface the rest of the app needs - keeps tests free of a real Pool. */
export interface DatabaseTransaction {
  query(text: string, values?: readonly unknown[]): Promise<{ rows: unknown[] }>;
  release(): void;
}

export interface DatabaseClient {
  query(text: string, values?: readonly unknown[]): Promise<{ rows: unknown[] }>;
  connect?(): Promise<DatabaseTransaction>;
  end?(): Promise<void>;
}

export interface DatabaseHealth {
  reachable: boolean;
  latencyMs: number | null;
  version: string | null;
  error?: string;
}

export function createPool(config: Config): pg.Pool {
  return new Pool({
    connectionString: `postgres://${config.DB_USER}:${config.DB_PASSWORD}@${config.DB_HOST}:${config.DB_PORT}/${config.DB_NAME}`,
    max: 10,
    connectionTimeoutMillis: 2500,
    query_timeout: 2500,
    idleTimeoutMillis: 30_000,
    ssl: config.DB_SSL
      ? { rejectUnauthorized: false }
      : undefined,
  });
}

/** Cheap liveness probe against Postgres - never throws, always reports. */
export async function pingDatabase(pool: DatabaseClient): Promise<DatabaseHealth> {
  const started = Date.now();
  try {
    const { rows } = await pool.query(
      "SELECT current_database() AS db, version() AS version"
    );
    const version = (rows[0] as { version?: string } | undefined)?.version ?? "unknown";
    return { reachable: true, latencyMs: Date.now() - started, version };
  } catch (error) {
    return {
      reachable: false,
      latencyMs: null,
      version: null,
      error: error instanceof Error ? error.message : String(error),
    };
  }
}