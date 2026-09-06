import type { FastifyPluginAsync } from "fastify";

import type { LoginInput, RegisterInput } from "../../services/auth.service.js";
import type { AcceptInvitationInput } from "../../services/organization.service.js";
import {
  acceptInvitationSchema,
  loginSchema,
  logoutSchema,
  refreshSchema,
  registerSchema,
} from "./auth.schema.js";
import { authRateLimit } from "../../middleware/rate-limit.js";

export const authRoutes: FastifyPluginAsync = async (app) => {
  const AUTH_RATE_LIMIT = authRateLimit(app.config);

  app.post<{ Body: RegisterInput }>(
    "/register",
    { schema: registerSchema, ...AUTH_RATE_LIMIT },
    async (request, reply) => {
      const result = await app.authService.register(request.body);
      return reply.code(201).send(result);
    }
  );

  // Day 22 — accept an invitation token and create the invitee's account in
  // the inviting org. Public (no auth): the bearer knows the emitted token.
  app.post<{ Body: AcceptInvitationInput }>(
    "/invitations/accept",
    { schema: acceptInvitationSchema, ...AUTH_RATE_LIMIT },
    async (request, reply) => {
      const result = await app.organizationService.acceptInvitation(request.body);
      return reply.code(201).send(result);
    }
  );

  app.post<{ Body: LoginInput }>(
    "/login",
    { schema: loginSchema, ...AUTH_RATE_LIMIT },
    async (request) => app.authService.login(request.body)
  );

  app.post<{ Body: { refreshToken: string } }>(
    "/refresh",
    { schema: refreshSchema, ...AUTH_RATE_LIMIT },
    async (request) => app.authService.refresh(request.body.refreshToken)
  );

  app.post<{ Body: { refreshToken: string } }>(
    "/logout",
    { schema: logoutSchema, ...AUTH_RATE_LIMIT },
    async (request, reply) => {
      await app.authService.logout(request.body.refreshToken);
      return reply.code(204).send();
    }
  );
};