-- 018: per-utterance addressee judgment (WO-LILY-FLOOR-001 FL-1).
-- The 81BCB0 session's root condition was agent_classification null on
-- every utterance. Every finalized segment now writes one row carrying
-- the classifier's judgment: classification, fused host-directed score,
-- per-family score components, and the side-cluster lock/extend/break
-- trail. Additive only — no existing column changes.

ALTER TABLE lily_addressee_log
  ADD COLUMN IF NOT EXISTS agent_classification TEXT,
  ADD COLUMN IF NOT EXISTS addressee_score NUMERIC,
  ADD COLUMN IF NOT EXISTS addressee_score_components JSONB,
  ADD COLUMN IF NOT EXISTS side_cluster_id INTEGER,
  ADD COLUMN IF NOT EXISTS side_cluster_event TEXT;
