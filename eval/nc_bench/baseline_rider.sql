-- WO-LILY-NC-BENCH-001 Task 4 — NC-off baseline rider.
-- Run against the NEXT live session (NC-off, full new stack) and compare
-- with the Aug 5 audited session. If the non-NC stack carries the room,
-- NC's return becomes optional rather than pending (record the verdict
-- in the README's WS-14 memo section).
--
-- Usage: replace :session_id with the live session's id
-- (SELECT session_id FROM lily_sessions ORDER BY created_at DESC LIMIT 5;)

-- 1. Dropped-answer rate: questions played vs adjudication rows.
--    (Aug 5 audit: Q5's pre-window answer wrote no row — the class the
--    pre-window buffer has since closed; expect attempted == played on
--    every question the table actually answered.)
SELECT
  s.session_id,
  s.question_number                            AS questions_played,
  COUNT(DISTINCT a.question_index)             AS questions_with_answer_rows,
  COUNT(a.id)                                  AS answer_rows_total,
  s.question_number - COUNT(DISTINCT a.question_index) AS questions_missing_rows
FROM lily_sessions s
LEFT JOIN lily_answers a ON a.session_id = s.session_id
WHERE s.session_id = :session_id
GROUP BY s.session_id, s.question_number;

-- 2. Phantom-label count: speaker labels in the transcript that never
--    produced an adjudicated answer AND never got a bound player name —
--    diarization inventing people (NC-relevant: noise fragments become
--    phantom S-labels).
SELECT
  t.speaker_label,
  COUNT(*)                                     AS segments,
  MAX(t.speaker_name)                          AS bound_name,   -- NULL = never bound
  SUM((LENGTH(TRIM(t.text)) < 3)::int)         AS near_empty_segments
FROM lily_transcripts t
WHERE t.session_id = :session_id
  AND t.speaker_label <> 'LILY'
GROUP BY t.speaker_label
ORDER BY segments DESC;

-- 3. Attribution spread: answers per player vs the roster — a player
--    with zero attributed answers in a full game is an attribution
--    failure candidate (cross-check against the session report / scorekeeper state).
SELECT
  a.player_name,
  COUNT(*)                                     AS answers,
  SUM((a.verdict = 'correct')::int)            AS correct,
  SUM(a.awarded_points)                        AS points
FROM lily_answers a
WHERE a.session_id = :session_id
GROUP BY a.player_name
ORDER BY answers DESC;

-- 4. Segment-span sanity: spans <= 0s or > 30s are broken segmentation
--    (the WS-10 quarantine class); healthy speech runs ~0.3–10s.
SELECT
  COUNT(*)                                                    AS segments,
  ROUND(AVG(t.segment_end - t.segment_start)::numeric, 2)     AS mean_span_s,
  ROUND(MAX(t.segment_end - t.segment_start)::numeric, 2)     AS max_span_s,
  SUM(((t.segment_end - t.segment_start) <= 0)::int)          AS nonpositive_spans,
  SUM(((t.segment_end - t.segment_start) > 30)::int)          AS over_30s_spans
FROM lily_transcripts t
WHERE t.session_id = :session_id
  AND t.speaker_label <> 'LILY';
