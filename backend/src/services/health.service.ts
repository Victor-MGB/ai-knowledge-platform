import { pingDatabase } from "../database/postgres.js";
import type { DatabaseClient, DatabaseHealth } from "../database/postgres.js";

export interface HealthReport {
  status: "healthy" | "degraded";
  uptimeSeconds: number;
  db: DatabaseHealth;
}

/** Aggregates dependency health into a single report for the /health route. */
export class HealthService {
  constructor(private readonly db: DatabaseClient) {}

  async check(): Promise<HealthReport> {
    const health = await pingDatabase(this.db);
    return {
      status: health.reachable ? "healthy" : "degraded",
      uptimeSeconds: Math.round(process.uptime()),
      db: health,
    };
  }
}