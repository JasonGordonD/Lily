-- 022_lily_picture_arsenal_entry.sql
-- WO-LILY-ARSENAL-SEED-001 A1 — what an arsenal entry actually is.
--
-- lily_picture_arsenal and lily_picture_arsenal_usage were created by
-- migration lily_picture_arsenal_001 (applied direct to prod, never
-- checked in). That shelf holds a question, an answer, a prompt and an
-- image path — enough to serve, not enough to be COHERENT. Two columns
-- the serving code already reads (difficulty_tier, reveal_color) did not
-- exist at all: lily_arsenal._row_to_question read them through
-- dict.get() and silently took the default on every row.
--
-- This migration completes the entry anatomy so the bank is a designed
-- corpus rather than a pile of pictures:
--
--   FORMAT (A2)    format, options, is_real_image — "picture trivia" is
--                  not one shape; the tag is what lets a round MIX.
--   BINDING (A1)   binding_direction — image-first or question-first.
--                  Recorded per entry because correspondence failures
--                  cluster by direction, and you cannot see the cluster
--                  if you did not write down which way each entry went.
--   CONTENT (A3)   subject_area, difficulty_tier, reveal_color — the
--                  spread that keeps a bank from feeling repetitive.
--   GATE (A5)      classifier_verdict, gate_mode, reviewed_at/by —
--                  entries land 'generating' and are PROMOTED. The first
--                  adult batch cannot serve on a classifier's say-so.
--   COST (A10)     generation_cost_usd, generation_attempts — the bank
--                  is a standing spend; the operator sets depth with the
--                  number in front of him, not after the invoice.
--
-- Idempotent: every statement is if-not-exists / add-column-if-not-exists,
-- so re-running against a partially-migrated database is safe.

-- -- entry anatomy ----------------------------------------------------------

alter table public.lily_picture_arsenal
  -- A2: which format this entry is, so a round can deliberately vary shape.
  add column if not exists format text not null default 'identify',
  -- A2: multiple-choice options (phonetically distinct; never bare letters).
  -- Null for freeform formats.
  add column if not exists options jsonb,
  -- A2: real_or_imagined needs BOTH halves in the bank. A generated entry
  -- is false; a web-sourced real photograph is true.
  add column if not exists is_real_image boolean not null default false,
  -- Provenance of the pixels: 'generated' (Grok/Gemini) or 'web' (Exa).
  add column if not exists image_source text not null default 'generated',
  -- A1: image-first (image drives, question written about it) or
  -- question-first (question drives, image generated to complete it).
  add column if not exists binding_direction text not null default 'image_first',
  -- A3: subject-area tag from the partition brief — the anti-repetition
  -- axis the seeding job spreads across.
  add column if not exists subject_area text,
  -- A3: difficulty spread. Already read by _row_to_question; never existed.
  add column if not exists difficulty_tier integer not null default 2,
  -- Already read by _row_to_question; never existed. The spoken reveal.
  add column if not exists reveal_color text,
  -- A4: outbound classifier verdict recorded per entry — the same gate
  -- live generation passes, evidenced rather than assumed.
  add column if not exists classifier_verdict text,
  add column if not exists classified_at timestamptz,
  -- A5: how this entry was promoted, and by whom if a human did it.
  add column if not exists gate_mode text not null default 'auto',
  add column if not exists reviewed_at timestamptz,
  add column if not exists reviewed_by text,
  -- A5: why a 'rejected' row was rejected (moderation, dedup, gate).
  add column if not exists rejected_reason text,
  -- A10: measured spend for this entry, including retried attempts.
  add column if not exists generation_cost_usd numeric(10, 5),
  add column if not exists generation_attempts integer not null default 1,
  -- A6: which seeding run created this entry (null for in-session
  -- replenishment), so a run summary can be reconstructed after the fact.
  add column if not exists run_id uuid;

-- 'rejected' joins the status vocabulary: a moderation refusal or a failed
-- gate is a RECORDED outcome, not a silently dropped one. 'retired' still
-- means served-out, never deleted, so provenance survives either way.
alter table public.lily_picture_arsenal
  drop constraint if exists lily_picture_arsenal_status_check;
alter table public.lily_picture_arsenal
  add constraint lily_picture_arsenal_status_check
  check (status in ('generating', 'ready', 'rejected', 'retired'));

alter table public.lily_picture_arsenal
  drop constraint if exists lily_picture_arsenal_binding_check;
alter table public.lily_picture_arsenal
  add constraint lily_picture_arsenal_binding_check
  check (binding_direction in ('image_first', 'question_first'));

alter table public.lily_picture_arsenal
  drop constraint if exists lily_picture_arsenal_image_source_check;
alter table public.lily_picture_arsenal
  add constraint lily_picture_arsenal_image_source_check
  check (image_source in ('generated', 'web'));

-- The draw is (partition, status) filtered; the seeding job counts the
-- same pair per partition. Format/subject spread reads drive the readout.
create index if not exists lily_picture_arsenal_partition_status_idx
  on public.lily_picture_arsenal (partition, status);
create index if not exists lily_picture_arsenal_format_idx
  on public.lily_picture_arsenal (partition, format);
create index if not exists lily_picture_arsenal_hash_idx
  on public.lily_picture_arsenal (question_text_hash);

-- -- seeding runs (A6 summary, A9 rejection rate, A10 cost) ------------------

create table if not exists public.lily_picture_arsenal_runs (
  id                  uuid primary key default gen_random_uuid(),
  partition           text        not null
                        check (partition in ('general', 'adult_suggestive',
                                             'adult_explicit')),
  status              text        not null default 'running'
                        check (status in ('running', 'completed', 'failed')),
  target_depth        integer     not null,
  started_at          timestamptz not null default now(),
  finished_at         timestamptz,
  -- A6 run summary.
  created_count       integer     not null default 0,
  skipped_duplicate   integer     not null default 0,
  -- A9: a provider moderation refusal is an EXPECTED outcome at seeding
  -- time, counted per partition so a heat setting the provider will not
  -- paint shows up as a number the operator can act on.
  rejected_moderation integer     not null default 0,
  rejected_gate       integer     not null default 0,
  error_count         integer     not null default 0,
  -- A10.
  cost_usd            numeric(10, 5) not null default 0,
  duration_seconds    numeric(10, 2),
  notes               text,
  -- Resumability: a run interrupted mid-flight leaves its row 'running'
  -- with a heartbeat; the next run adopts or supersedes it.
  heartbeat_at        timestamptz not null default now()
);

-- A6 concurrency safety, DB-enforced rather than merely intended: at most
-- ONE running seeding job per partition. A second concurrent run cannot
-- insert its row, so two runs can never double-fill the same shelf. Same
-- shape of guarantee as UNIQUE(arsenal_id, group_id) on the usage table —
-- structurally impossible, not unlikely.
create unique index if not exists lily_picture_arsenal_runs_one_active_idx
  on public.lily_picture_arsenal_runs (partition)
  where status = 'running';

create index if not exists lily_picture_arsenal_runs_recent_idx
  on public.lily_picture_arsenal_runs (partition, started_at desc);

-- Service role owns the arsenal end to end (seeding job + agent). No
-- client policy: a browser must never read unserved adult entries.
alter table public.lily_picture_arsenal_runs enable row level security;
