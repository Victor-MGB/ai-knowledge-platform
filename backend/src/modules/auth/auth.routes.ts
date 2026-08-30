import type { FastifyPluginAsync } from "fastify";

import type { LoginInput, RegisterInput } from "../../services/auth.service.js";
import { loginSchema, logoutSchema, refreshSchema, registerSchema } from "./auth.schema.js";

export const authRoutes: FastifyPluginAsync = async (app) => {
  app.post<{ Body: RegisterInput }>(
    "/register",
    { schema: registerSchema },
    async (request, reply) => {
      const result = await app.authService.register(request.body);
      return reply.code(201).send(result);
    }
  );

  app.post<{ Body: LoginInput }>(
    "/login",
    { schema: loginSchema },
    async (request) => app.authService.login(request.body)
  );

  app.post<{ Body: { refreshToken: string } }>(
    "/refresh",
    { schema: refreshSchema },
    async (request) => app.authService.refresh(request.body.refreshToken)
  );

  app.post<{ Body: { refreshToken: string } }>(
    "/logout",
    { schema: logoutSchema },
    async (request, reply) => {
      await app.authService.logout(request.body.refreshToken);
      return reply.code(204).send();
    }
  );
};