import type { DatabaseClient, DatabaseTransaction } from "../postgres.js";

export interface InvitationRow {
  id: string;
  organization_id: string;
  email: string;
  role: string;
  token_hash: string;
  status: "pending" | "accepted" | "revoked";
  expires_at: string;
  invited_by: string;
  created_at: string;
  accepted_at: string | null;
}

type Executable = DatabaseClient | DatabaseTransaction;

const INVITATION_COLUMNS =
  "id, organization_id, email, role, token_hash, status, expires_at, invited_by, created_at, accepted_at";

export async function insertInvitation(
  db: Executable,
  data: {
    organizationId: string;
    email: string;
    role: string;
    tokenHash: string;
    expiresAt: Date;
    invitedBy: string;
  }
): Promise<InvitationRow> {
  const { rows } = await db.query(
    `INSERT INTO invitations
       (organization_id, email, role, token_hash, expires_at, invited_by)
     VALUES ($1, $2, $3, $4, $5, $6)
     RETURNING ${INVITATION_COLUMNS}`,
    [
      data.organizationId,
      data.email,
      data.role,
      data.tokenHash,
      data.expiresAt,
      data.invitedBy,
    ]
  );
  return rows[0] as InvitationRow;
}

export async function listInvitationsByOrg(
  db: Executable,
  organizationId: string
): Promise<InvitationRow[]> {
  const { rows } = await db.query(
    `SELECT ${INVITATION_COLUMNS} FROM invitations
     WHERE organization_id = $1
     ORDER BY created_at DESC, id ASC`,
    [organizationId]
  );
  return rows as InvitationRow[];
}

export async function findInvitationByToken(
  db: Executable,
  tokenHash: string
): Promise<InvitationRow | undefined> {
  const { rows } = await db.query(
    `SELECT ${INVITATION_COLUMNS} FROM invitations WHERE token_hash = $1`,
    [tokenHash]
  );
  return rows[0] as InvitationRow | undefined;
}

export async function findInvitationById(
  db: Executable,
  organizationId: string,
  invitationId: string
): Promise<InvitationRow | undefined> {
  const { rows } = await db.query(
    `SELECT ${INVITATION_COLUMNS} FROM invitations
     WHERE organization_id = $1 AND id = $2`,
    [organizationId, invitationId]
  );
  return rows[0] as InvitationRow | undefined;
}

export async function markInvitationAccepted(
  db: Executable,
  invitationId: string
): Promise<void> {
  await db.query(
    `UPDATE invitations
     SET status = 'accepted', accepted_at = now()
     WHERE id = $1`,
    [invitationId]
  );
}

export async function revokeInvitation(
  db: Executable,
  organizationId: string,
  invitationId: string
): Promise<boolean> {
  // RETURNING id makes the "did the pending row actually update?" test read
  // off the typed rows array instead of relying on a rowCount that the
  // DatabaseClient interface does not expose.
  const { rows } = await db.query(
    `UPDATE invitations SET status = 'revoked'
     WHERE organization_id = $1 AND id = $2 AND status = 'pending'
     RETURNING id`,
    [organizationId, invitationId]
  );
  return rows.length > 0;
}
