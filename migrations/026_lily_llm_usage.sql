-- 026_lily_llm_usage.sql — per-call LLM usage + latency persistence
-- (WO-LILY-LLM-USAGE-PERSISTENCE).
--
-- Closes an observability gap surfaced by the 2026-08-14 dead-air
-- diagnosis: Lily emitted rich per-call LLMMetrics (ttft, tokens,
-- empty-STOP) to logs only, with no durable store, so "was a generation
-- even attempted, and how long did it take?" could not be answered from
-- the database after a session. This table durably records one row per
-- vocal LLM call off the framework's blessed per-call metrics surface.
--
-- Written by: lily_persistence.lily_write_llm_usage (fire-and-forget,
-- fail-open — a usage-write failure never touches the hot path).
-- Fed from: lily_metrics.LilyMetricsCollector.collect_llm_call (the single
-- per-call choke point), enriched with the empty-STOP finish state stashed
-- by lily_agent's llm_node empty-STOP guard.
--
-- utterance_id holds the per-call speech_id (the agent speech handle the
-- call served) — the natural correlation key the LLMMetrics event carries;
-- it joins to lily_transcripts / the addressee log by speech turn.
-- empty_stop=true marks a FinishReason.STOP with no text and no tools (the
-- lobby dead-air class); finish_reason carries the guard's verdict tag.

create table if not exists lily_llm_usage (
  id                uuid primary key default gen_random_uuid(),
  session_id        text        not null,
  utterance_id      text,
  phase             text,
  model             text,
  purpose           text        not null default 'vocal',
  ttft_ms           double precision,
  total_ms          double precision,
  prompt_tokens     integer,
  completion_tokens integer,
  finish_reason     text,
  empty_stop        boolean     not null default false,
  created_at        timestamptz not null default now()
);

create index if not exists lily_llm_usage_session_idx
  on lily_llm_usage (session_id);
create index if not exists lily_llm_usage_created_idx
  on lily_llm_usage (created_at);
-- Empty-STOP forensics: the dead-air class is a scoped scan of this flag.
create index if not exists lily_llm_usage_empty_stop_idx
  on lily_llm_usage (empty_stop) where empty_stop;

-- No client policy: the service role owns usage reads/writes and bypasses
-- RLS; browser/anon clients must never read token accounting.
alter table public.lily_llm_usage enable row level security;
