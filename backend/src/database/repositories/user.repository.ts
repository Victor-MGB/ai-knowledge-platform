import type { DatabaseClient, DatabaseTransaction } from "../postgres.js";

export interface UserRow {
  id: string;
  organization_id: string;
  email: string;
  password_hash: string;
  role: string;
  created_at: string;
  last_login_at: string | null;
}

type Executable = DatabaseClient | DatabaseTransaction;

const USER_COLUMNS =
  "id, organization_id, email, password_hash, role, created_at, last_login_at";

export async function insertUser(
  db: Executable,
  organizationId: string,
  email: string,
  passwordHash: string,
  role = "owner"
): Promise<UserRow> {
  const { rows } = await db.query(
    `INSERT INTO users (organization_id, email, password_hash, role)
     VALUES ($1, $2, $3, $4)
     RETURNING ${USER_COLUMNS}`,
    [organizationId, email, passwordHash, role]
  );
  return rows[0] as UserRow;
}

export async function findUserById(
  db: Executable,
  id: string
): Promise<UserRow | undefined> {
  const { rows } = await db.query(
    `SELECT ${USER_COLUMNS} FROM users WHERE id = $1`,
    [id]
  );
  return rows[0] as UserRow | undefined;
}

/** All users with this email across all tenants. Day-3 schema scopes email
 * uniqueness per organization, so the same address can exist in many orgs. */
export async function findUsersByEmail(db: Executable, email: string): Promise<UserRow[]> {
  const { rows } = await db.query(
    `SELECT ${USER_COLUMNS} FROM users WHERE email = $1 ORDER BY created_at ASC`,
    [email]
  );
  return rows as UserRow[];
}

export async function touchLastLogin(
  db: Executable,
  userId: string
): Promise<void> {
  await db.query("UPDATE users SET last_login_at = now() WHERE id = $1", [userId]);
}