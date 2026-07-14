-- LILY migration 003 — persistent cross-session memory ("rematch" promoted
-- to v1) + consent-safety adult column on the curated bank.

-- One row per finished session, keyed to the stable group_id (the
-- device-scoped UUID the frontend passes as participant token metadata).
-- session_id is UNIQUE so finish_game and the shutdown callback can both
-- upsert idempotently.
CREATE TABLE IF NOT EXISTS lily_memories (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id        text NOT NULL,
    session_id      text UNIQUE NOT NULL,
    played_at       timestamptz DEFAULT now(),
    players         jsonb,          -- final [{name, score, streak}]
    winner          text,
    question_count  integer,
    highlights      jsonb,          -- callouts / best moments
    summary         text            -- deterministic template, no LLM
);

CREATE INDEX IF NOT EXISTS idx_lily_memories_group
    ON lily_memories (group_id);

-- Consent-safety: the bank now contains adult-register rows; they must
-- never surface at a general-mode table (guard applied in
-- lily_persistence.lily_fetch_bank_question via lily_memory.lily_bank_mode_filter).
ALTER TABLE lily_questions
    ADD COLUMN IF NOT EXISTS adult boolean DEFAULT false;
