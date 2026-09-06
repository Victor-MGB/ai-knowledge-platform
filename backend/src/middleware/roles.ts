/** Day 22 — organization role hierarchy: OWNER > ADMIN > MEMBER > VIEWER.
 *
 * The role lives on `users` (one user row per org = one membership with a
 * role). These are the only valid roles and their strict ordering, used by
 * `requireAtLeast` so a guard can express "at least ADMIN" once instead of
 * enumerating every stronger role.
 */

export const ORG_ROLES = ["owner", "admin", "member", "viewer"] as const;

export type OrgRole = (typeof ORG_ROLES)[number];

export const ROLE_RANK: Record<OrgRole, number> = {
  viewer: 1,
  member: 2,
  admin: 3,
  owner: 4,
};

export function isOrgRole(value: string): value is OrgRole {
  return (ORG_ROLES as readonly string[]).includes(value);
}

/** True when `role` is at least as powerful as `minimum` in the hierarchy. */
export function atLeast(role: string, minimum: OrgRole): boolean {
  if (!isOrgRole(role)) return false;
  return ROLE_RANK[role] >= ROLE_RANK[minimum];
}
