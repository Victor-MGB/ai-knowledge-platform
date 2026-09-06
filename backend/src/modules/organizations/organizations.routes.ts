import type { FastifyPluginAsync } from "fastify";

import { requireAtLeast, requireAuth } from "../../middleware/auth-guard.js";
import type { CreateInvitationInput, UpdateMemberRoleInput } from "./organizations.schema.js";
import {
  createInvitationSchema,
  getOrganizationSchema,
  listInvitationsSchema,
  listMembersSchema,
  revokeInvitationSchema,
  updateMemberRoleSchema,
} from "./organizations.schema.js";

/** Organizations under /api/v1/organizations (Day 22). Tenant profile +
 * membership + how a team grows. Reads are open to any member; inviting and
 * role changes require ADMIN (or OWNER). Tenant scope always comes from the
 * token, never the client. */
export const organizationRoutes: FastifyPluginAsync = async (app) => {
  app.get(
    "/organizations",
    { schema: getOrganizationSchema, preHandler: [requireAuth] },
    async (request) => app.organizationService.profile(request.user.org)
  );

  app.get(
    "/organizations/members",
    { schema: listMembersSchema, preHandler: [requireAuth] },
    async (request) => app.organizationService.listMembers(request.user.org)
  );

  app.patch(
    "/organizations/members/:id/role",
    { schema: updateMemberRoleSchema, preHandler: [requireAuth, requireAtLeast("admin")] },
    async (request, reply) => {
      const { id } = request.params as { id: string };
      const body = request.body as UpdateMemberRoleInput;
      const member = await app.organizationService.updateMemberRole(
        request.user.org,
        request.user.sub,
        request.user.role,
        id,
        body.role
      );
      return reply.code(200).send(member);
    }
  );

  app.get(
    "/organizations/invitations",
    { schema: listInvitationsSchema, preHandler: [requireAuth, requireAtLeast("admin")] },
    async (request) => app.organizationService.listInvitations(request.user.org)
  );

  app.post(
    "/organizations/invitations",
    { schema: createInvitationSchema, preHandler: [requireAuth, requireAtLeast("admin")] },
    async (request, reply) => {
      const body = request.body as CreateInvitationInput;
      const invitation = await app.organizationService.createInvitation(
        request.user.org,
        request.user.sub,
        request.user.role,
        { email: body.email, role: body.role }
      );
      return reply.code(201).send(invitation);
    }
  );

  app.delete(
    "/organizations/invitations/:id",
    { schema: revokeInvitationSchema, preHandler: [requireAuth, requireAtLeast("admin")] },
    async (request, reply) => {
      const { id } = request.params as { id: string };
      await app.organizationService.revokeInvitation(request.user.org, id);
      return reply.code(204).send();
    }
  );
};
