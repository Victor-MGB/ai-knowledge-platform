import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // The integration files share one Postgres container. With files running
    // in parallel, one file's pool-client acquisition (2.5s connection
    // timeout, max 10) gets starved while the auth suite holds transactions,
    // so /health intermittently reported "degraded" -> flaky test.
    // Serial files: deterministic; the whole suite runs in ~90s.
    fileParallelism: false,
  },
});