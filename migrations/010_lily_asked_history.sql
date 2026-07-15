-- 010_lily_asked_history.sql — per-group asked history (WO-LILY-OMNIBUS-002,
-- sub-agent D).
--
-- One row per question SERVED (armed into the state block for delivery),
-- keyed to the RESOLVED group_id. The bank draw and the generation
-- avoid-list exclude a group's history — by question_id for bank rows
-- (kb_<id>) and by normalized-text hash (lily_bank.lily_question_text_hash:
-- lowercase, punctuation/whitespace-stripped, sha1) for everything else —
-- so a returning table never hears a repeat.
--
-- This table is also the exposure ledger for the difficulty self-tuning
-- job (lily_bank_tuning.py): servings per question_id gate the tuning
-- decisions behind the N>=5 exposure floor. Per-group burn scope
-- (migration 009 note) rides this table when it lands.

CREATE TABLE IF NOT EXISTS lily_asked_history (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id            text NOT NULL,
    question_id         text,
    question_text_hash  text,
    session_id          text,
    asked_at            timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lily_asked_history_group
    ON lily_asked_history (group_id);
