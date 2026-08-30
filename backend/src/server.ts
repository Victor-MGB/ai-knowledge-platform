import { buildApp } from "./app.js";
import { loadConfig } from "./config/config.js";

async function main(): Promise<void> {
  const config = loadConfig();
  const app = await buildApp({ config });

  const shutdown = async (signal: string): Promise<void> => {
    app.log.info({ signal }, "shutting down");
    await app.close();
    process.exit(0);
  };
  process.on("SIGINT", () => void shutdown("SIGINT"));
  process.on("SIGTERM", () => void shutdown("SIGTERM"));

  await app.listen({ host: config.HOST, port: config.PORT });
  app.log.info({ url: `http://${config.HOST}:${config.PORT}` }, "KnowFlow backend up");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});