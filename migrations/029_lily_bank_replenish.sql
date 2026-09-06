-- 029_lily_bank_replenish.sql
-- WO-LILY-SUPPLY-001 S2 — the bank-first supply lane's write side.
--
-- Operator ruling (2026-09-06): "the bank serves, the author replenishes.
-- Live question authoring never sits on the delivery path again."
--
-- Evidence this exists for: grok-4.5 authoring takes 20-39s to the first
-- content token per call. The bank draw takes milliseconds. Every one of
-- those authoring seconds that a table sat through was a second nobody had
-- to spend, because the same question could have been written the night
-- before by a job nobody was waiting on. This migration is that job's
-- storage: the columns a background author needs to land a row the draw
-- can see, and the receipt table that says what a run actually did.
--
-- ADDITIVE AND IDEMPOTENT. Every statement is add-column-if-not-exists /
-- create-if-not-exists; no existing column, default, or row is touched.
-- Re-running against a partially-migrated database is safe.
--
-- THE STATUS CONTRACT (the seam with S1, running in parallel):
--   * migration 009 established `status`, with 'active' = servable and
--     "any value other than 'active' means not servable".
--   * The background author lands its rows at status='ready' — verified,
--     deduped, moderation-passed, never served. S1's draw
--     (lily_persistence.lily_fetch_bank_question) accepts BOTH 'ready' and
--     'active'; nothing in S2 writes 'active', and nothing in S2 changes
--     what 'active' means for the 448 rows already in the table.
--   * 'ready' is deliberately NOT a synonym the writer picks at random: it
--     is the arsenal's own promotion vocabulary (lily_picture_arsenal
--     status='ready'), so the two banks name a servable row the same way.

-- -- lane, hash, and the replenishment stamp ---------------------------------

-- LANE. The unit the watermark is tracked on: one deck×category slot of the
-- rotation, as `<deck>:<category>` (general:academic, adult:adult_kink, ...).
-- Nullable because every pre-existing row predates lanes; the replenisher
-- back-reads a lane's depth by (mode, adult, category) so legacy rows count
-- toward depth without being rewritten. See lily_bank_replenish.LANES.
alter table public.lily_questions
  add column if not exists lane text;

-- EXACT-DUP KEY. sha256 of the normalized question text
-- (lily_bank.lily_normalize_question_text: lowercased, punctuation
-- stripped, whitespace collapsed). Deliberately a DIFFERENT column name
-- from lily_asked_history.question_text_hash, which is sha1 and is a
-- per-group serving key — same idea, different digest and different
-- meaning, and one column carrying two digests is how a dedup check
-- silently stops matching.
alter table public.lily_questions
  add column if not exists question_text_sha256 text;

-- WHEN THE AUTHOR LANDED IT. Null for every hand-curated / seed row; a
-- timestamp only on rows a replenishment run wrote. This is what makes
-- "last replenishment" in the health readout a measurement rather than a
-- guess about created_at (lily_questions has no created_at at all).
alter table public.lily_questions
  add column if not exists replenished_at timestamptz;

-- WHICH RUN WROTE IT. Joins a banked row to its receipt below, so a bad
-- batch is attributable to the run that produced it.
alter table public.lily_questions
  add column if not exists replenish_run_id uuid;

-- The watermark's own read: count rows per lane by status.
create index if not exists lily_questions_lane_status_idx
  on public.lily_questions (lane, status);

-- The draw's read once 'ready' is servable (mirrors migration 016's
-- lily_questions_draw_idx, which pins status first).
create index if not exists lily_questions_status_ready_idx
  on public.lily_questions (status, adult, category)
  where status = 'ready';

-- DEDUP AS A CONSTRAINT, not merely as a check the writer remembers to
-- run. The application-side gate (exact sha256 + the ARSENAL-SEED A5
-- difflib similarity pass) rejects near-duplicates the database cannot
-- see; this index makes the EXACT case structurally impossible, so two
-- concurrent runs racing on the same question text end with one insert and
-- one honest failure rather than two rows. Partial, so the hundreds of
-- legacy rows that carry no hash are unaffected.
create unique index if not exists lily_questions_sha256_unique_idx
  on public.lily_questions (question_text_sha256)
  where question_text_sha256 is not null;

-- -- the run receipt ---------------------------------------------------------
--
-- Mirrors lily_picture_arsenal_runs (migration 022) column for column where
-- the concept survives the change of medium: a text question has no image,
-- so `cost` is measured in TOKENS off the streaming usage rows
-- (lily_llm_usage, migrations 026/027) rather than in a per-image price
-- sheet. A run that dies still leaves its row: a run whose numbers nobody
-- can read is the seeding job that "ran" on 2026-08-07 and stocked nothing.

create table if not exists public.lily_bank_replenish_runs (
  id                  uuid        primary key default gen_random_uuid(),
  lane                text        not null,
  deck                text        not null,
  category            text        not null,
  status              text        not null default 'running'
                        check (status in ('running', 'completed', 'failed')),
  target_depth        integer     not null,
  ready_at_start      integer,
  started_at          timestamptz not null default now(),
  finished_at         timestamptz,
  -- Run summary. `authored` counts author calls that returned a question;
  -- accepted + rejected_verify + rejected_moderation + skipped_duplicate +
  -- error_count accounts for every one of them, so a run that banked
  -- nothing still says WHY.
  authored_count      integer     not null default 0,
  accepted_count      integer     not null default 0,
  skipped_duplicate   integer     not null default 0,
  rejected_verify     integer     not null default 0,
  rejected_moderation integer     not null default 0,
  error_count         integer     not null default 0,
  -- COST PER QUESTION at the chosen effort, summed off lily_llm_usage rows
  -- tagged with this run's id (purpose='bank_replenish'). Tokens, not
  -- dollars: the provider price sheet moves and the token count does not.
  prompt_tokens       bigint      not null default 0,
  completion_tokens   bigint      not null default 0,
  cost_tokens         bigint      not null default 0,
  effort              text,
  duration_seconds    numeric(10, 2),
  notes               text,
  -- Resumability, exactly as the arsenal does it: an interrupted run leaves
  -- its row 'running' with a heartbeat; the next run reclaims a DEAD row
  -- (silent heartbeat) and leaves a live one alone.
  heartbeat_at        timestamptz not null default now()
);

-- Concurrency safety, DB-enforced rather than merely intended: at most ONE
-- running replenishment per lane. A second concurrent run's insert fails
-- outright, so an in-session job and a cron run cannot double-fill a lane.
-- Same shape of guarantee as lily_picture_arsenal_runs_one_active_idx.
create unique index if not exists lily_bank_replenish_runs_one_active_idx
  on public.lily_bank_replenish_runs (lane)
  where status = 'running';

create index if not exists lily_bank_replenish_runs_recent_idx
  on public.lily_bank_replenish_runs (lane, started_at desc);

-- Service role owns the replenisher end to end (cron job + agent). No
-- client policy: a browser must never read unserved questions — that is
-- the answer key.
alter table public.lily_bank_replenish_runs enable row level security;
