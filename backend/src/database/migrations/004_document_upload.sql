-- Day 8 - document upload support (migration for the live Day-3 database).
--
-- Adds the upload metadata the API needs and admits DOCX (source_type was
-- CHECKed to pdf/md/html/txt only). schema.sql is the from-scratch source and
-- already carries these columns + 'docx' for fresh installs.

ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS filename     text;
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS mime_type    text;
ALTER TABLE public.documents ADD COLUMN IF NOT EXISTS size         bigint NOT NULL DEFAULT 0;

-- backfill existing rows (seed data uploaded no filename) then lock the columns
UPDATE public.documents SET filename = title,
                            mime_type = CASE source_type
                                WHEN 'pdf'  THEN 'application/pdf'
                                WHEN 'docx' THEN 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                                WHEN 'html' THEN 'text/html'
                                WHEN 'txt'  THEN 'text/plain'
                                ELSE 'text/markdown'
                            END
WHERE filename IS NULL OR mime_type IS NULL;

ALTER TABLE public.documents ALTER COLUMN filename  SET NOT NULL;
ALTER TABLE public.documents ALTER COLUMN mime_type SET NOT NULL;

ALTER TABLE public.documents ADD CONSTRAINT documents_size_non_negative CHECK (size >= 0);

-- widen source_type to include docx; the constraint name is auto-generated,
-- so find it by what it guards instead of assuming a name
DO $$
DECLARE
    con record;
BEGIN
    FOR con IN
        SELECT conname FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
        WHERE c.conrelid = 'public.documents'::regclass
          AND c.contype = 'c'
          AND a.attname = 'source_type'
    LOOP
        EXECUTE format('ALTER TABLE public.documents DROP CONSTRAINT %I', con.conname);
    END LOOP;
END $$;

ALTER TABLE public.documents ADD CONSTRAINT documents_source_type_check
    CHECK (source_type IN ('pdf', 'docx', 'md', 'html', 'txt'));