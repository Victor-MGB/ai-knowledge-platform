import type { DatabaseClient, DatabaseTransaction } from "../postgres.js";

export interface MemberRow {
  id: string;
  organization_id: string;
  email: string;
  role: string;
  created_at: string;
  last_login_at: string | null;
}

type Executable = DatabaseClient | DatabaseTransaction;

const MEMBER_COLUMNS =
  "id, organization_id, email, role, created_at, last_login_at";

export async function countMembersByOrg(
  db: Executable,
  organizationId: string
): Promise<number> {
  const { rows } = await db.query(
    `SELECT COUNT(*)::int AS count FROM users WHERE organization_id = $1`,
    [organizationId]
  );
  return (rows[0] as { count: number }).count;
}

export async function listMembersByOrg(
  db: Executable,
  organizationId: string
): Promise<MemberRow[]> {
  const { rows } = await db.query(
    `SELECT ${MEMBER_COLUMNS} FROM users
     WHERE organization_id = $1
     ORDER BY created_at ASC, id ASC`,
    [organizationId]
  );
  return rows as MemberRow[];
}

export async function findMemberByOrg(
  db: Executable,
  organizationId: string,
  memberId: string
): Promise<MemberRow | undefined> {
  const { rows } = await db.query(
    `SELECT ${MEMBER_COLUMNS} FROM users
     WHERE organization_id = $1 AND id = $2`,
    [organizationId, memberId]
  );
  return rows[0] as MemberRow | undefined;
}

export async function updateMemberRole(
  db: Executable,
  organizationId: string,
  memberId: string,
  role: string
): Promise<MemberRow | undefined> {
  const { rows } = await db.query(
    `UPDATE users SET role = $3
     WHERE organization_id = $1 AND id = $2
     RETURNING ${MEMBER_COLUMNS}`,
    [organizationId, memberId, role]
  );
  return rows[0] as MemberRow | undefined;
}
