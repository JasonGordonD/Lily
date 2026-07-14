-- 006: lily_session_reports — session scoring pipeline (B3, WRITE side only).
-- Shape ported from the Lovebirds call-report capture pattern (one row per
-- session, upserted idempotently on session_id at session close). The agent
-- writes transcript + game_stats; `assessment` is filled LATER by the
-- clinical desk (report_status: 'pending' until then) — never by agent code.

CREATE TABLE IF NOT EXISTS lily_session_reports (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  session_id     TEXT NOT NULL UNIQUE,
  group_id       TEXT,
  created_at     TIMESTAMPTZ DEFAULT now(),
  transcript     JSONB,
  game_stats     JSONB,
  report_status  TEXT DEFAULT 'pending',
  assessment     JSONB
);
CREATE INDEX IF NOT EXISTS idx_lily_session_reports_group ON lily_session_reports (group_id);
