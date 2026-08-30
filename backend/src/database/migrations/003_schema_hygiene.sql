-- Day 7 - schema hygiene fixes from the architecture review.
--
-- Bug found: documents.updated_at and (new) users.updated_at were never
-- auto-updated. A document whose status flipped queued -> processing ->
-- ready advertised the same created_at silently, and Day-6 auth bumped
-- last_login_at without touching any "modified" timestamp. Standard practice
-- is a BEFORE UPDATE trigger that keeps the column honest, so stale
-- "last modified" bugs stop at the schema.

ALTER TABLE public.users ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

CREATE OR REPLACE FUNCTION public.touch_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_documents_updated_at ON public.documents;
CREATE TRIGGER trg_documents_updated_at
    BEFORE UPDATE ON public.documents
    FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

DROP TRIGGER IF EXISTS trg_users_updated_at ON public.users;
CREATE TRIGGER trg_users_updated_at
    BEFORE UPDATE ON public.users
    FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();