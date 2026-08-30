-- Day 6 - authentication support.
-- Refresh tokens are opaque, single-use, stored hashed (sha256), and revoked
-- (rather than deleted) so replay of a rotated token is detectable.

CREATE TABLE IF NOT EXISTS public.refresh_tokens (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    organization_id uuid NOT NULL REFERENCES public.organizations (id) ON DELETE CASCADE,
    token_hash      text NOT NULL UNIQUE,
    expires_at      timestamptz NOT NULL,
    revoked_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user ON public.refresh_tokens (user_id);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_expires ON public.refresh_tokens (expires_at);

ALTER TABLE public.users ADD COLUMN IF NOT EXISTS last_login_at timestamptz;