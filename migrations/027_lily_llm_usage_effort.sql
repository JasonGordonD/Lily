-- 027_lily_llm_usage_effort.sql — additive `effort` column on lily_llm_usage
-- (WO-LILY-LLM-USAGE-ALL-PATHS-001).
--
-- Operator directive: "a row per LLM call with purpose, model, effort,
-- ttft_ms, total_ms on every path ... do not redesign the table in this
-- wave." Migration 026 shipped the table WITHOUT an effort column, and the
-- directive requires one — this is ONE additive nullable column, not a
-- redesign: no existing column, index, default, or policy changes.
--
-- effort carries the reasoning-effort tier ACTUALLY sent on the call
-- ('low' / 'medium' / 'high' for Grok lanes); NULL for transports that send
-- no effort (Grok vision, Gemini grounding) and for rows written by an
-- older writer.
--
-- The writer (lily_persistence.lily_record_llm_call) stays fail-open when
-- this migration is not yet applied in an environment: on PostgREST's
-- missing-column error (PGRST204) it drops the key, retries once, warns.

alter table public.lily_llm_usage
  add column if not exists effort text;
