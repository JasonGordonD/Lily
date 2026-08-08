-- 023_lily_asked_history_category.sql — the served question's CATEGORY on
-- the asked-history row (WO-LILY-HOTFIX-006 N2).
--
-- Session lily-16A9AE: the operator asked for a Cape Cod round, Lily said
-- "I'm putting together a custom round all about Cape Cod for you right
-- now", and the six lily_asked_history rows for that session were Gold,
-- Vatican City, Psycho, One Direction, rupee and Rehab — every one of them
-- a generic curated row from migration 004. The round the table was
-- promised existed only in the narration.
--
-- The ledger is what settled that question after the fact, and it could
-- only half-settle it: the rows proved WHICH questions were served but
-- carried no category, so "was a Cape Cod round ever actually built?" had
-- to be reconstructed by hand from the question text. The category rides
-- the row from now on, which makes a custom round an auditable fact rather
-- than an inference — and makes the fiction detectable in one query:
--
--   select category, count(*) from lily_asked_history
--   where session_id = '...' group by 1;
--
-- Nullable with no default on purpose: pre-023 rows genuinely do not know
-- their category, and backfilling a guess would be exactly the kind of
-- invented certainty this WO exists to remove.
alter table lily_asked_history
  add column if not exists category text;

create index if not exists idx_lily_asked_history_group_category
    on lily_asked_history (group_id, category);
