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
WHERE id = 'a665f656-8326-47ee-9892-659913b8b441'::uuid
  AND group_id = 'grp_5863973edc6707da45b52e49857fecbebe4ce969'
  AND sample_count = 1
  AND model_tag = 'ecapa-192-v1';

DELETE FROM lily_speaker_voiceprints
WHERE id IN (315, 316)
  AND lower(player_name) = 'playing'
  AND group_id IN (
    'lily-9337B1-331ff234',
    'grp_5863973edc6707da45b52e49857fecbebe4ce969'
  );

DELETE FROM lily_memories
WHERE id = 31
  AND session_id = 'lily-9337B1-331ff234'
  AND group_id = 'grp_5863973edc6707da45b52e49857fecbebe4ce969'
  AND lower(COALESCE(winner, '')) = 'playing';
