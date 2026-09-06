import type { FastifyRequest, preHandlerHookHandler } from "fastify";

import { AppError } from "../utils/errors.js";
import { atLeast, type OrgRole } from "./roles.js";

/** Gate for authenticated endpoints: verifies the Bearer access token and
 * confirms it is an access token for a known subject. */
export const requireAuth: preHandlerHookHandler = async (request: FastifyRequest) => {
  try {
    await request.jwtVerify();
  } catch {
    throw new AppError("UNAUTHORIZED", "a valid access token is required", 401);
  }
  if (request.user?.typ !== "access" || !request.user?.sub) {
    throw new AppError("UNAUTHORIZED", "token is not a valid access token", 401);
  }
};

/** Optional role gate - owners always pass; others need the matching role. */
export const requireRole =
  (role: string): preHandlerHookHandler =>
  async (request: FastifyRequest) => {
    if (request.user?.role !== role && request.user?.role !== "owner") {
      throw new AppError("FORBIDDEN", "insufficient permissions", 403);
    }
  };

/** Day 22 — hierarchy gate: the caller's role must be at least `minimum`.
 *  Implements OWNER > ADMIN > MEMBER > VIEWER so a route can require "admin or
 *  stronger" without enumerating roles. Owners always clear every level. */
export const requireAtLeast =
  (minimum: OrgRole): preHandlerHookHandler =>
  async (request: FastifyRequest) => {
    if (!atLeast(request.user?.role ?? "", minimum)) {
      throw new AppError("FORBIDDEN", "insufficient permissions", 403);
    }
  };