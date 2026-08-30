import { expect, it } from "vitest";

import pg from "pg";

import { createPool } from "../src/database/postgres.js";
import { loadConfig } from "../src/config/config.js";

/** Integration test against the real Day-3 pgvector container. Skipped
 * automatically when the database is unreachable, so CI stays green
 * everywhere. */

async function databaseIsReachable(): Promise<boolean> {
  const pool = createPool(loadConfig());
  try {
    const { rows } = await pool.query("SELECT 1 AS one");
    return (rows[0] as { one?: number })?.one === 1;
  } catch {
    return false;
  } finally {
    await pool.end();
  }
}

const reachable = await databaseIsReachable();

const itDb = reachable ? it : it.skip;

itDb("reaches the real Postgres and reports its version", async () => {
  const pool = createPool(loadConfig());
  try {
    const { rows } = await pool.query("SELECT version() AS version");
    expect((rows[0] as { version?: string })?.version).toMatch(/PostgreSQL/);
  } finally {
    await pool.end();
  }
});

itDb("/health against the real database is healthy", async () => {
  const { buildApp } = await import("../src/app.js");
  const app = await buildApp();
  try {
    const response = await app.inject({ method: "GET", url: "/health" });
    expect(response.statusCode).toBe(200);
    expect(response.json().status).toBe("healthy");
    expect(response.json().db.version).toContain("PostgreSQL");
  } finally {
    await app.close();
  }
});