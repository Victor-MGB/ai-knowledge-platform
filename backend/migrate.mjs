#!/usr/bin/env node
/**
 * KnowFlow fresh-DB bootstrap for Render (and any empty Postgres).
 *
 *   node migrate.mjs
 *
 * Applies, in order, against an empty database:
 *   1. backend/bootstrap.sql  (mirror of infrastructure/db/schema.sql: extensions,
 *      base tables + indexes) — applied once, tracked as the "bootstrap" row.
 *   2. backend/migrations/*.sql (the 002-011 deltas), each tracked by filename.
 *
 * Connection comes from DATABASE_URL (preferred) or the classic DB_* vars.
 * Set DB_SSL=true (or sslmode=require in DATABASE_URL) for managed Postgres.
 * The backend Dockerfile copies bootstrap.sql + migrations/ into the image.
 */
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import pg from "pg";

const CONFIG = {
  databaseUrl: process.env.DATABASE_URL,
  host: process.env.DB_HOST ?? "localhost",
  port: Number(process.env.DB_PORT ?? 5432),
  user: process.env.DB_USER ?? "knowflow",
  password: process.env.DB_PASSWORD ?? "knowflow",
  database: process.env.DB_NAME ?? "knowflow",
};

const sslRequested =
  (process.env.DB_SSL ?? "") === "true" ||
  String(CONFIG.databaseUrl ?? "").includes("sslmode=require") ||
  String(CONFIG.databaseUrl ?? "").includes("sslmode=verify");

const client = new pg.Client({
  connectionString:
    CONFIG.databaseUrl ??
    `postgres://${CONFIG.user}:${CONFIG.password}@${CONFIG.host}:${CONFIG.port}/${CONFIG.database}`,
  ssl: sslRequested ? { rejectUnauthorized: false } : undefined,
});

const HERE = path.dirname(new URL(import.meta.url).pathname);

async function setMigrationsTable() {
  await client.query(`
    CREATE TABLE IF NOT EXISTS schema_migrations (
      name       text PRIMARY KEY,
      applied_at timestamptz NOT NULL DEFAULT now()
    );
  `);
}

async function appliedNames() {
  const { rows } = await client.query("SELECT name FROM schema_migrations");
  return new Set(rows.map((r) => r.name));
}

const DUP_CODES = new Set(["42P07", "42701", "42710", "42712"]);

async function applyFile(name, sql, errorsAreOk = false) {
  try {
    await client.query(sql);
    await client.query("INSERT INTO schema_migrations (name) VALUES ($1)", [name]);
    console.log(`  applied ${name}`);
  } catch (error) {
    // db/schema.sql is a mirror of the current schema, so a migration that only
    // duplicates objects it already created is legitimately a no-op.
    if (errorsAreOk && error?.code && DUP_CODES.has(error.code)) {
      await client.query("INSERT INTO schema_migrations (name) VALUES ($1)", [name]);
      console.log(`  skipped ${name} (already present in db/schema.sql)`);
      return;
    }
    throw error;
  }
}

/** Resolve the migrations directory (Docker image: ./migrations; dev: src/database/migrations). */
async function migrationDir() {
  const candidates = [
    path.join(HERE, "migrations"),
    path.join(HERE, "src", "database", "migrations"),
  ];
  for (const dir of candidates) {
    try {
      await readdir(dir);
      return dir;
    } catch {}
  }
  throw new Error(`migrations dir not found (tried ${candidates.join(", ")})`);
}

/** Migration names in apply order (bootstrap.sql is handled separately). */
async function migrationFiles() {
  const dir = await migrationDir();
  return (await readdir(dir))
    .filter((f) => f.endsWith(".sql"))
    .sort((a, b) => a.localeCompare(b, "en", { numeric: true }));
}

async function main() {
  await client.connect();
  await setMigrationsTable();
  const applied = await appliedNames();

  const schema = await readFile(path.join(HERE, "bootstrap.sql"), "utf8");
  const hasBase = await client.query(
    "SELECT to_regclass('public.organizations') AS t"
  );
  const isFresh = hasBase.rows[0].t === null;

  if (isFresh) {
    // Fresh DB: backend/bootstrap.sql is the mirror of the CURRENT schema (per
    // the repo's "migrations are history, db/schema.sql is the mirror" rule).
    // Replaying the historical 002-011 deltas on top would fight it (e.g. 006
    // DROPs the mirror's embeddings and 007's rebuild is a no-op), so a fresh
    // DB ingests the mirror and records the deltas as covered.
    await applyFile("bootstrap", schema);
    const files = await migrationFiles();
    for (const file of files) {
      if (!applied.has(file)) {
        await client.query("INSERT INTO schema_migrations (name) VALUES ($1)", [file]);
      }
    }
    console.log(`fresh bootstrap complete (${files.length} historical migrations marked covered)`);
  } else {
    // Upgrade path for an existing partial DB: apply whatever deltas are missing.
    await client.query("INSERT INTO schema_migrations (name) VALUES ('bootstrap') ON CONFLICT DO NOTHING");
    const dir = await migrationDir();
    const files = await migrationFiles();
    for (const file of files) {
      if (applied.has(file)) continue;
      await applyFile(file, await readFile(path.join(dir, file), "utf8"), true);
    }
    console.log(`upgrade complete`);
  }
  await client.end();
}

main().catch(async (error) => {
  console.error(error);
  process.exitCode = 1;
  try {
    await client.end();
  } catch {}
});