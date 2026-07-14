-- 008: lily_acoustic_trajectories — per-user-turn acoustic snapshots
-- (WO-LILY-AUDEERING-001 Task 6). Clone of the drt_acoustic_trajectories
-- schema referenced in the JRVS donor, names adapted to lily_: one row per
-- user turn carrying the latest devAIce capture's category / dimension /
-- prosody / features jsonb. `features` stays for schema parity even though
-- Lily's upload config does not request the features module (column is
-- normally an empty object).
-- Writes are fire-and-forget (asyncio.to_thread) — never on the hot path.

CREATE TABLE IF NOT EXISTS lily_acoustic_trajectories (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  session_id TEXT NOT NULL,
  turn_index INTEGER NOT NULL,
  category JSONB,
  dimension JSONB,
  prosody JSONB,
  features JSONB,
  audio_quality JSONB,
  scene JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lily_acoustic_traj_session_turn
  ON lily_acoustic_trajectories (session_id, turn_index);

-- Task 6 addressee synergy: every lily_addressee_log row carries the LATEST
-- acoustic snapshot — non-null jsonb when the pipeline is healthy, an
-- EXPLICIT SQL null (the key is always set by the writer, never absent)
-- when the circuit breaker is open.
ALTER TABLE lily_addressee_log
  ADD COLUMN IF NOT EXISTS acoustic_snapshot JSONB;
