import { randomBytes } from "node:crypto";

import { AppError } from "../utils/errors.js";
import type { DatabaseClient, DatabaseTransaction } from "../database/postgres.js";
import { findOrganizationById } from "../database/repositories/organization.repository.js";
import {
  countMembersByOrg,
  findMemberByOrg,
  listMembersByOrg,
  updateMemberRole,
  type MemberRow,
} from "../database/repositories/member.repository.js";
import {
  findInvitationById,
  findInvitationByToken,
  insertInvitation,
  listInvitationsByOrg,
  markInvitationAccepted,
  revokeInvitation,
  type InvitationRow,
} from "../database/repositories/invitation.repository.js";
import { insertUser } from "../database/repositories/user.repository.js";
import { hashToken } from "./token.service.js";
import type { PasswordService } from "./password.service.js";
import { atLeast, isOrgRole, type OrgRole } from "../middleware/roles.js";

export interface MemberView {
  id: string;
  email: string;
  role: OrgRole;
  createdAt: string;
  lastLoginAt: string | null;
}

export interface OrganizationView {
  id: string;
  name: string;
  slug: string;
  memberCount: number;
}

export interface InvitationView {
  id: string;
  email: string;
  role: OrgRole;
  status: "pending" | "accepted" | "revoked";
  expiresAt: string;
  createdAt: string;
}

export interface MembersResponse {
  organization: OrganizationView;
  items: MemberView[];
}

export interface InvitationsResponse {
  organization: OrganizationView;
  items: InvitationView[];
}

export interface CreateInvitationInput {
  email: string;
  role: OrgRole;
}

export interface AcceptInvitationInput {
  token: string;
  password: string;
}

export interface AcceptInvitationResult {
  invitation: InvitationView;
  user: { id: string; email: string; role: OrgRole };
  organization: OrganizationView;
}

function toMemberView(row: MemberRow): MemberView {
  return {
    id: row.id,
    email: row.email,
    role: row.role as OrgRole,
    createdAt: row.created_at,
    lastLoginAt: row.last_login_at,
  };
}

function toInvitationView(row: InvitationRow): InvitationView {
  return {
    id: row.id,
    email: row.email,
    role: row.role as OrgRole,
    status: row.status,
    expiresAt: row.expires_at,
    createdAt: row.created_at,
  };
}

function isUniqueViolation(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { code?: string }).code === "23505"
  );
}

/** Day 22 — organizations: the tenant profile, its membership (the `users` of
 *  the org, where each row is a membership with a role), and how a team grows
 *  (invitations with a role that an invitee accepts to become a member). */
export class OrganizationService {
  constructor(
    private readonly db: DatabaseClient,
    private readonly passwords: PasswordService,
    private readonly inviteTtlMs: number
  ) {}

  private async orgView(organizationId: string): Promise<OrganizationView> {
    const org = await findOrganizationById(this.db, organizationId);
    if (!org) {
      throw new AppError("NOT_FOUND", "organization not found", 404);
    }
    const memberCount = await countMembersByOrg(this.db, organizationId);
    return { id: org.id, name: org.name, slug: org.slug, memberCount };
  }

  async profile(organizationId: string): Promise<OrganizationView> {
    return this.orgView(organizationId);
  }

  async listMembers(organizationId: string): Promise<MembersResponse> {
    const members = await listMembersByOrg(this.db, organizationId);
    return {
      organization: await this.orgView(organizationId),
      items: members.map(toMemberView),
    };
  }

  /** Change a member's role. Guardrails: only non-owner members can be
   *  reassigned (the owner cannot demote themselves or the sole owner), and
   *  only roles at or below the caller's own rank may be granted (a member can
   *  never mint an admin). */
  async updateMemberRole(
    organizationId: string,
    actorId: string,
    actorRole: string,
    memberId: string,
    newRole: OrgRole
  ): Promise<MemberView> {
    const target = await findMemberByOrg(this.db, organizationId, memberId);
    if (!target) {
      throw new AppError("NOT_FOUND", "member not found", 404);
    }
    if (target.role === "owner" || actorId === memberId) {
      throw new AppError("FORBIDDEN", "an owner cannot be reassigned", 403);
    }
    if (!atLeast(actorRole, newRole) && actorRole !== newRole) {
      throw new AppError("FORBIDDEN", "cannot grant a role above your own", 403);
    }
    const updated = await updateMemberRole(this.db, organizationId, memberId, newRole);
    if (!updated) {
      throw new AppError("NOT_FOUND", "member not found", 404);
    }
    return toMemberView(updated);
  }

  async createInvitation(
    organizationId: string,
    invitedBy: string,
    actorRole: string,
    input: CreateInvitationInput
  ): Promise<InvitationView> {
    if (!isOrgRole(input.role)) {
      throw new AppError("VALIDATION_ERROR", "invalid role", 400);
    }
    if (!atLeast(actorRole, input.role)) {
      throw new AppError("FORBIDDEN", "cannot invite a role above your own", 403);
    }
    const email = input.email.toLowerCase().trim();
    const expiresAt = new Date(Date.now() + this.inviteTtlMs);
    const token = randomBytes(32).toString("hex");
    try {
      const row = await insertInvitation(this.db, {
        organizationId,
        email,
        role: input.role,
        tokenHash: hashToken(token),
        expiresAt,
        invitedBy,
      });
      return Object.assign(toInvitationView(row), { token });
    } catch (error) {
      if (isUniqueViolation(error)) {
        throw new AppError(
          "INVITATION_EXISTS",
          "there is already a pending invitation for this email",
          409
        );
      }
      throw error;
    }
  }

  async listInvitations(organizationId: string): Promise<InvitationsResponse> {
    const invitations = await listInvitationsByOrg(this.db, organizationId);
    return {
      organization: await this.profile(organizationId),
      items: invitations.map(toInvitationView),
    };
  }

  async revokeInvitation(organizationId: string, invitationId: string): Promise<void> {
    const row = await findInvitationById(this.db, organizationId, invitationId);
    if (!row) {
      throw new AppError("NOT_FOUND", "invitation not found", 404);
    }
    if (row.status !== "pending") {
      throw new AppError("INVITATION_NOT_PENDING", "invitation is not pending", 409);
    }
    await revokeInvitation(this.db, organizationId, invitationId);
  }

  /** Accept an invitation: verify the raw token (hashed), the expiry, and that
   *  it is still pending; create the invitee's `users` row in the org with the
   *  invited role; spend the invitation — all in one transaction, so a person
   *  can never be half-created or double-accept. */
  async acceptInvitation(input: AcceptInvitationInput): Promise<AcceptInvitationResult> {
    const invitation = await findInvitationByToken(this.db, hashToken(input.token));
    if (!invitation) {
      throw new AppError("INVITATION_NOT_FOUND", "invitation is invalid", 404);
    }
    if (invitation.status !== "pending") {
      throw new AppError("INVITATION_SPENT", "invitation has already been used", 409);
    }
    if (new Date(invitation.expires_at).getTime() <= Date.now()) {
      throw new AppError("INVITATION_EXPIRED", "invitation has expired", 410);
    }

    if (!this.db.connect) {
      throw new AppError("DATASTORE_UNAVAILABLE", "database connection unavailable", 503);
    }
    const client = await this.db.connect();
    try {
      await client.query("BEGIN");
      const passwordHash = await this.passwords.hash(input.password);
      const user = await insertUser(
        client,
        invitation.organization_id,
        invitation.email,
        passwordHash,
        invitation.role
      );
      await markInvitationAccepted(client, invitation.id);
      await client.query("COMMIT");
      const orgRow = await findOrganizationById(this.db, invitation.organization_id);
      const organization: OrganizationView = {
        id: invitation.organization_id,
        name: orgRow?.name ?? "organization",
        slug: orgRow?.slug ?? "organization",
        memberCount: await countMembersByOrg(this.db, invitation.organization_id),
      };
      const acceptedInvitation: InvitationRow = { ...invitation, status: "accepted" };
      return {
        invitation: toInvitationView(acceptedInvitation),
        user: { id: user.id, email: user.email, role: user.role as OrgRole },
        organization,
      };
    } catch (error) {
      await safeRollback(client);
      if (isUniqueViolation(error)) {
        throw new AppError(
          "EMAIL_TAKEN",
          "an account with this email already exists in this organization",
          409
        );
      }
      throw error;
    } finally {
      client.release();
    }
  }
}

async function safeRollback(client: DatabaseTransaction): Promise<void> {
  try {
    await client.query("ROLLBACK");
  } catch {
    // connection already aborted the transaction; nothing to do
  }
}
