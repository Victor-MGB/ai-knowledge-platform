import { describe, expect, it } from "vitest";

import { loadConfig, REFRESH_TTL_MS } from "../src/config/config.js";
import { PasswordService } from "../src/services/password.service.js";
import { RefreshTokenIssuer, hashToken } from "../src/services/token.service.js";

// low cost factor so the test suite stays fast; bcrypt structure is identical
const passwords = new PasswordService(4);

describe("password service", () => {
  it("hashes to a bcrypt string and verifies correctly", async () => {
    const hash = await passwords.hash("correct horse battery staple");
    expect(hash.startsWith("$2")).toBe(true);
    expect(await passwords.verify("correct horse battery staple", hash)).toBe(true);
    expect(await passwords.verify("wrong", hash)).toBe(false);
  });

  it("salts each hash differently", async () => {
    const a = await passwords.hash("same");
    const b = await passwords.hash("same");
    expect(a).not.toBe(b);
  });
});

describe("refresh token issuer", () => {
  it("issues 256-bit hex tokens distinct from their stored hash", () => {
    const { token, tokenHash } = new RefreshTokenIssuer().issue();
    expect(token).toMatch(/^[0-9a-f]{128}$/); // 64 bytes
    expect(tokenHash).not.toBe(token);
  });

  it("hashes tokens deterministically to 64 hex chars", () => {
    expect(hashToken("abc")).toBe(hashToken("abc"));
    expect(hashToken("abc")).toMatch(/^[0-9a-f]{64}$/);
  });
});

describe("config", () => {
  it("rejects a weak JWT_SECRET in production", () => {
    expect(() =>
      loadConfig({ NODE_ENV: "production", JWT_SECRET: "short" })
    ).toThrow(/JWT_SECRET/);
  });

  it("accepts a strong JWT_SECRET in production", () => {
    const config = loadConfig({
      NODE_ENV: "production",
      JWT_SECRET: "x".repeat(48),
    });
    expect(config.BCRYPT_ROUNDS).toBe(10);
  });

  it("computes the refresh window from the TTL config", () => {
    const config = loadConfig({ REFRESH_TTL_DAYS: "2" });
    expect(REFRESH_TTL_MS(config)).toBe(2 * 24 * 60 * 60 * 1000);
  });
});