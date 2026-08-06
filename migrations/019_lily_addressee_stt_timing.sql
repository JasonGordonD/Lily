-- 019: lily_addressee_log STT stream-clock timing trail (WS-11,
-- WO-LILY-OMNIBUS-003). The overlap side of the 81BCB0 diagnosis: overlap
-- flags never fired partly because the reconciler's per-segment timing
-- provenance was never persisted, so a zero-overlap session could not be
-- audited. These two columns carry the LilyTimestampReconciler verdict:
--   timing_source         — "stt_stream_reconciled" | "arrival_time"
--   timing_drift_seconds  — arrival_ts - reconciled_end (NULL when no stream
--                           timing was recovered)
-- agent_classification and the addressee-score / side-cluster columns are
-- FL-1's (migration 018) — WS-11 defers to that classifier as the single
-- writer and adds ONLY these two non-overlapping timing columns.
-- The write path (lily_agent._addressee_row) is already live; until this
-- DDL is applied lily_persistence.lily_log_addressee strips these keys (and
-- FL-1's) and retries so no corpus row is ever lost (cardinal rule: no
-- memory is bad memory). Additive, backfill-null, non-destructive.

ALTER TABLE lily_addressee_log
  ADD COLUMN IF NOT EXISTS timing_source        TEXT,
  ADD COLUMN IF NOT EXISTS timing_drift_seconds NUMERIC;
