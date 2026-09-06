-- Day 22: Organizations — role hierarchy + invitations.
--
-- Two changes to support real team membership:
--
-- 1. Widen users.role to admit `viewer` (read-only), completing the
--    OWNER > ADMIN > MEMBER > VIEWER hierarchy. The role column on `users`
--    IS the membership record (one user row per org, role per membership);
--    the hierarchy itself (which roles may do what) lives in the backend's
--    requireAtLeast guard, not in SQL.
--
-- 2. Add invitations so a team can grow without every new person registering
--    a fresh tenant. An invitation is how scoped credentials reach an org:
--    an owner/admin invites an email with a role; the invitee accepts with a
--    password (creating their `users` row in that org); the invite is then
--    spent (accepted) and can no longer be replayed.
--
-- Invitation rules enforced in code + here:
--   * (organization_id, email) unique among *pending* invites so a team can't
--     stack duplicates they'd have to unwind;
--   * a token is stored only as sha256 (same convention as refresh_tokens) and
--     carries an expiry so a leaked link rots;
--   * accepting a spent (accepted) or revoked invite is rejected; an accepted
--     invite that would collide with an existing (org, email) user is rejected.

ALTER TABLE public.users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE public.users ADD CONSTRAINT users_role_check
    CHECK (role IN ('owner', 'admin', 'member', 'viewer'));

CREATE TABLE invitations (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    email           text NOT NULL,
    role            text NOT NULL DEFAULT 'member'
                    CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    token_hash      text NOT NULL UNIQUE,
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'accepted', 'revoked')),
    expires_at      timestamptz NOT NULL,
    invited_by      uuid NOT NULL REFERENCES users(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    accepted_at     timestamptz
);

-- one outstanding invite per (org, email) — a partial unique index on the
-- pending subset, so a team cannot stack duplicates
CREATE UNIQUE INDEX idx_invitations_org_email_pending
    ON invitations (organization_id, email)
    WHERE status = 'pending';

CREATE INDEX idx_invitations_org      ON invitations (organization_id);
CREATE INDEX idx_invitations_expires  ON invitations (expires_at);
