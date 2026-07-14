-- 005: lily_addressee_log — addressee-label corpus (B1 training-data flywheel).
-- One row per finalized segment during an open answer window, plus any
-- finalized segment the agent acts on. Labels arrive later: implicit weak
-- labels at adjudication commit, explicit ground truth from the clarify flow.
-- NOTE: this table was applied verbatim to production on 2026-07-14.

CREATE TABLE IF NOT EXISTS lily_addressee_log (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  session_id TEXT NOT NULL,
  utterance_ts TIMESTAMPTZ NOT NULL,
  speaker_label TEXT,
  player_name TEXT,
  transcript TEXT NOT NULL,
  is_final BOOLEAN NOT NULL,
  phase TEXT,
  answer_window_open BOOLEAN,
  seconds_into_window NUMERIC,
  fuzzy_matched_answer BOOLEAN,
  system_directed_hit BOOLEAN,
  agent_action TEXT,
  label TEXT,
  label_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_lily_addressee_log_session ON lily_addressee_log (session_id);
