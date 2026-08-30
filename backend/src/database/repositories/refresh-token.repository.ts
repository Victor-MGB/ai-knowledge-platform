import type { DatabaseClient, DatabaseTransaction } from "../postgres.js";

export interface RefreshTokenRow {
  id: string;
  user_id: string;
  organization_id: string;
}

type Executable = DatabaseClient | DatabaseTransaction;

export async function insertRefreshToken(
  db: Executable,
  userId: string,
  organizationId: string,
  tokenHash: string,
  expiresAt: Date
): Promise<RefreshTokenRow> {
  const { rows } = await db.query(
    `INSERT INTO refresh_tokens (user_id, organization_id, token_hash, expires_at)
     VALUES ($1, $2, $3, $4)
     RETURNING id, user_id, organization_id`,
    [userId, organizationId, tokenHash, expiresAt]
  );
  return rows[0] as RefreshTokenRow;
}

/** A token is only reusable if it exists, was never revoked, and is not yet
 * expired - single-use rotation nullifies it the moment it is presented. */
export async function findValidRefreshToken(
  db: Executable,
  tokenHash: string
): Promise<RefreshTokenRow | undefined> {
  const { rows } = await db.query(
    `SELECT id, user_id, organization_id
     FROM refresh_tokens
     WHERE token_hash = $1 AND revoked_at IS NULL AND expires_at > now()`,
    [tokenHash]
  );
  return rows[0] as RefreshTokenRow | undefined;
}

export async function revokeRefreshToken(db: Executable, tokenHash: string): Promise<void> {
  await db.query("UPDATE refresh_tokens SET revoked_at = now() WHERE token_hash = $1", [
    tokenHash,
  ]);
}