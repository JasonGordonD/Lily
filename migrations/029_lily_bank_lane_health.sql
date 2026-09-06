-- 029_lily_bank_lane_health.sql — per-lane replenishment health
-- (WO-LILY-SUPPLY-001; S1 reads it, S2 writes it).
--
-- With the delivery path drawing from `lily_questions` and never waiting
-- on an author (S1), the thing that can now starve a table is a LANE
-- running dry — not the bank as a whole. `lily_bank_health()` counts
-- ready/burned rows per lane straight off `lily_questions`, but two facts
-- about a lane cannot be counted from the question rows: when the
-- replenisher last succeeded on it, and how much of what it authored for
-- that lane was thrown away. A lane that reads "ready: 4" is one story if
-- it was topped up an hour ago and quite another if the replenisher has
-- been failing on it for two days at a 90% rejection rate.
--
-- Both are honestly NULL until S2 lands: the readout reports null, never
-- a fabricated 0, because "not measured" and "measured as zero" are
-- different claims and only one of them is true today.
--
-- CONTRACT (S1 <-> S2):
--   * one row per rotation lane (lily_bank.LANE_BANK_CATEGORIES keys:
--     'academic', 'pop culture', 'wordplay', 'lifestyle-potpourri');
--     `lane` is the primary key, so the replenisher upserts on it.
--   * `last_replenished_at` — the completion time of the last run that
--     actually banked >= 1 row for this lane. A run that banked nothing
--     does NOT move it; that is what makes a stale timestamp legible as
--     "the replenisher is not landing rows here" rather than "the
--     replenisher is not running".
--   * `rejection_rate` — authored-and-discarded / authored, over the last
--     completed run for this lane (0.0-1.0).
--   * S1 never writes this table and treats an absent table or an absent
--     row as null, so S1 ships and runs correctly before S2 exists.

create table if not exists lily_bank_lane_health (
    lane                 text primary key,
    last_replenished_at  timestamptz,
    rejection_rate       numeric,
    authored_count       integer,
    rejected_count       integer,
    updated_at           timestamptz default now()
);

-- Draw support for the bank-first delivery path. Migration 016 already
-- indexes (status, adult, category, difficulty_tier, id), which is the
-- exact shape of every stage `lily_fetch_bank_question` now issues
-- (status + deck + lane category + register); no second index is added
-- here on purpose — a redundant one costs writes and buys nothing.
