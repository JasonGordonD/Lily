-- 011_lily_category_candidates.sql — gated category proposals
-- (WO-LILY-OMNIBUS-002, sub-agent F).
--
-- Generation may return a `proposed_category` (reserved schema field in
-- lily_reasoning). Each proposal upserts one row here: use_count bumps and
-- the proposing group's RESOLVED group_id joins the distinct-groups list.
-- The question itself keeps serving under its round FAMILY
-- (lily_agent.CATEGORY_FAMILIES) until the candidate is PROMOTED:
-- use_count >= 10 AND >= 3 distinct groups
-- (lily_bank.lily_category_promotion_ready). Promoted extras surface as
-- one lobby state-block line; Lily never announces unpromoted categories.

CREATE TABLE IF NOT EXISTS lily_category_candidates (
    name        text PRIMARY KEY,
    family      text,
    use_count   int DEFAULT 0,
    groups      jsonb DEFAULT '[]',
    first_seen  timestamptz DEFAULT now()
);
