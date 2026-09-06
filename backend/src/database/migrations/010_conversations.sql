-- Day 19: conversations — the Q&A chat layer on top of RAG.
--
-- A `conversations` row is a tenant's chat session; `messages` is the append-
-- only log of turns inside it. Both carry organization_id and every query MUST
-- scope on it (same tenancy rule as every other table — tenant isolation is by
-- schema + index, RLS lands on the hardening day).
--
-- The message model stores BOTH sides of a Q&A turn:
--   * role='user'      content = the question the caller asked;
--   * role='assistant' content = the RAG answer, and `payload` = the
--     JSONB provenance the answer shipped with (citations to render the
--     "Sources [n] ..." block, `refused`, `provider`, `model`, similarity).
-- `payload` is free-form JSONB on purpose: the assistant turn's shape moves
-- (Day 20 evaluation may add scores), and JSONB absorbs that without a schema
-- migration.
--
-- `position` is a per-conversation monotonic ordinal so message order is exact
-- and stable even when two turns share the same created_at instant (the
-- user+assistant pair is inserted together). Deletes CASCADE with the
-- conversation, the tenant, or the authoring user.

CREATE TABLE conversations (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    title           text NOT NULL,
    created_by      uuid NOT NULL REFERENCES users(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE messages (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    position        integer NOT NULL CHECK (position > 0),
    role            text NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content         text NOT NULL,
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_conversations_org   ON conversations (organization_id);
CREATE INDEX idx_conversations_user  ON conversations (created_by);
CREATE INDEX idx_messages_org        ON messages (organization_id);
CREATE INDEX idx_messages_conv       ON messages (conversation_id, position);

-- updated_at honesty (Day 7 convention) for conversations: any appended
-- message touches the conversation's modified timestamp.
CREATE OR REPLACE FUNCTION touch_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_conversations_updated_at ON conversations;
CREATE TRIGGER trg_conversations_updated_at
    BEFORE UPDATE ON conversations
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
