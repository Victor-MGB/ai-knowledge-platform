import { createHash, randomBytes } from "node:crypto";

/** Access token claims carried in the signed JWT. */
export interface AccessTokenPayload {
  sub: string; // user id
  org: string; // organization id (tenant)
  role: string;
  typ: "access"; // distinguishes from any other audience
  jti?: string; // unique token id, set at signing time
}

export interface IssuedRefreshToken {
  token: string; // sent to the client once
  tokenHash: string; // sha256(token) - all that is ever persisted
}

/** Opaque, 256-bit refresh tokens. Only the hash touches the database, so a
 * DB leak cannot be replayed. */
export class RefreshTokenIssuer {
  issue(): IssuedRefreshToken {
    const token = randomBytes(64).toString("hex");
    return { token, tokenHash: hashToken(token) };
  }
}

export function hashToken(token: string): string {
  return createHash("sha256").update(token).digest("hex");
}