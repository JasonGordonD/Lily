-- 020_lily_category_operator_requested.sql — first-class operator-requested
-- categories (WO-LILY-CAPABILITY-RESTORE-001 scope addition).
--
-- When an operator names a topic on the fly ("give me a Game of Thrones
-- round"), the category is registered in lily_category_candidates
-- immediately as FIRST-CLASS — it does not have to earn promotion through
-- the use_count>=10 / >=3 distinct-groups gate that model-proposed
-- categories must clear. This flag marks that stronger provenance so
-- lily_load_promoted_categories surfaces it right away and later rounds
-- can serve its banked questions (the compounding arsenal).
--
-- Applied live to project svqbfxdhpsmioaosuhkb (PRMPT) 2026-08-06.
alter table lily_category_candidates
  add column if not exists operator_requested boolean not null default false;
