/** Organizations module schemas (Day 22).
 *
 * Tenant profile + membership. `GET /members` lists the org's members (each
 * `users` row is a membership with a role); `PATCH /members/:id/role` changes
 * one. `POST /invitations` mints an invite (email + role); the invitee accepts
 * it via the public `POST /auth/invitations/accept` (see auth routes). Roles
 * are the OWNER > ADMIN > MEMBER > VIEWER hierarchy.
 */

export const ORG_ROLE_ENUM = ["owner", "admin", "member", "viewer"];

const organizationObject = {
  type: "object",
  required: ["id", "name", "slug", "memberCount"],
  properties: {
    id: { type: "string" },
    name: { type: "string" },
    slug: { type: "string" },
    memberCount: { type: "integer" },
  },
} as const;

const memberObject = {
  type: "object",
  required: ["id", "email", "role", "createdAt", "lastLoginAt"],
  properties: {
    id: { type: "string" },
    email: { type: "string" },
    role: { type: "string", enum: ORG_ROLE_ENUM },
    createdAt: { type: "string" },
    lastLoginAt: { type: ["string", "null"] },
  },
} as const;

const invitationObject = {
  type: "object",
  required: ["id", "email", "role", "status", "expiresAt", "createdAt"],
  properties: {
    id: { type: "string" },
    email: { type: "string" },
    role: { type: "string", enum: ORG_ROLE_ENUM },
    status: { type: "string", enum: ["pending", "accepted", "revoked"] },
    expiresAt: { type: "string" },
    createdAt: { type: "string" },
  },
} as const;

const memberIdParams = {
  type: "object",
  additionalProperties: false,
  required: ["id"],
  properties: {
    id: {
      type: "string",
      pattern:
        "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    },
  },
} as const;

export const getOrganizationSchema = {
  response: { 200: organizationObject },
} as const;

export const listMembersSchema = {
  response: {
    200: {
      type: "object",
      required: ["organization", "items"],
      properties: {
        organization: organizationObject,
        items: { type: "array", items: memberObject },
      },
    },
  },
} as const;

export const updateMemberRoleSchema = {
  params: memberIdParams,
  body: {
    type: "object",
    additionalProperties: false,
    required: ["role"],
    properties: {
      role: { type: "string", enum: ORG_ROLE_ENUM },
    },
  },
  response: { 200: memberObject },
} as const;

export const listInvitationsSchema = {
  response: {
    200: {
      type: "object",
      required: ["organization", "items"],
      properties: {
        organization: organizationObject,
        items: { type: "array", items: invitationObject },
      },
    },
  },
} as const;

export const createInvitationSchema = {
  body: {
    type: "object",
    additionalProperties: false,
    required: ["email", "role"],
    properties: {
      email: { type: "string", format: "email", maxLength: 320 },
      role: { type: "string", enum: ORG_ROLE_ENUM },
    },
  },
  response: {
    201: {
      type: "object",
      required: [...Object.keys(invitationObject.properties), "token"],
      properties: {
        ...invitationObject.properties,
        token: { type: "string" },
      },
    },
  },
} as const;

export const revokeInvitationSchema = {
  params: memberIdParams,
  response: { 204: { type: "null" } },
} as const;

export interface UpdateMemberRoleInput {
  role: "owner" | "admin" | "member" | "viewer";
}

export interface CreateInvitationInput {
  email: string;
  role: "owner" | "admin" | "member" | "viewer";
}
