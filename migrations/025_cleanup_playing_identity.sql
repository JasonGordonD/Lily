-- P0-E3: retire/delete the identity poison created by
-- lily-9337B1-331ff234.
--
-- Evidence:
--   * S1 was incorrectly bound as player_name='Playing'
--   * legacy voiceprint rekey failed on nonexistent sample_count
--   * close-time ECAPA enrollment founded a 1-sample rival centroid
--   * cosine to canonical Rami (grp_0b07...) = 0.7518736: above the
--     absolute floor but too marginal to merge without a fresh verification
--
-- Keep lily_sessions/transcripts/answers/report rows intact as the audit
-- record. Retire (not delete) the ECAPA row; delete only derived rows whose
-- identity label is provably false. Every predicate is idempotent and narrow.

UPDATE lily_voice_identity
SET status = 'retired',
    retired_at = COALESCE(retired_at, now()),
    updated_at = now()
WHERE group_id = (
    SELECT group_id
    FROM lily_sessions
    WHERE session_id = 'lily-9337B1-331ff234'
  )
  AND sample_count = 1
  AND model_tag = 'ecapa-192-v1'
  AND status = 'active';

DELETE FROM lily_speaker_voiceprints
WHERE lower(player_name) = 'playing'
  AND group_id IN (
    'lily-9337B1-331ff234',
    (
      SELECT group_id
      FROM lily_sessions
      WHERE session_id = 'lily-9337B1-331ff234'
    )
  );

DELETE FROM lily_memories
WHERE session_id = 'lily-9337B1-331ff234'
  AND group_id = (
    SELECT group_id
    FROM lily_sessions
    WHERE session_id = 'lily-9337B1-331ff234'
  )
  AND lower(COALESCE(winner, '')) = 'playing';
