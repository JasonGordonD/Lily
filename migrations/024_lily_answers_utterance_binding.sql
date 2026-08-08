-- 024_lily_answers_utterance_binding.sql — bind an answer row to the
-- UTTERANCE it came from, and record why it was written
-- (WO-LILY-HOTFIX-006 N9).
--
-- THE JUPITER CASE. Session lily-D99BE7, 21:10:13. Rami said "Okay. It's
-- Jupiter." Lily said "Jupiter was spot on, Rami, but just a split second
-- late!" — the conversational lane knew the answer was correct and knew who
-- said it. The ledger for q_1052 recorded Rami's answer as "Go." — his
-- earlier start command — marked incorrect, zero points. His actual answer
-- never entered the ledger at all; a different utterance was captured in
-- its place.
--
-- Two minds, one table: one lane knew the right answer arrived from the
-- right player, the other scored an unrelated word and closed the question.
-- Everything downstream — score, standings, glass — inherited the wrong
-- mind.
--
-- The code fix binds adjudication to a specific utterance rather than to a
-- speaker slot or "most recent fragment". These columns are what make that
-- binding SURVIVE into the record, which is the half that matters after the
-- night is over:
--
--   utterance_id  WHICH spoken utterance this verdict was rendered against.
--                 Without it the ledger can say what was scored but never
--                 prove it was the thing the player actually said, and the
--                 Jupiter defect is undiagnosable from the data — it took a
--                 transcript sitting beside the table to catch it.
--
--   cause         WHY the row exists: an ordinary in-window adjudication,
--                 or 'late_answer' — a correct answer that arrived past the
--                 buzzer, recorded with its real text so a miss is an
--                 honest recorded outcome rather than a silent loss.
--
-- Both nullable with no default. Pre-024 rows genuinely do not know which
-- utterance they came from, and lily_write_answer already carries a
-- fail-soft that strips these keys on a pre-DDL database, so the agent runs
-- unchanged against either schema — it simply cannot evidence the binding
-- until this lands.

alter table lily_answers
  add column if not exists utterance_id text,
  add column if not exists cause text;

-- The audit this is for: given a question, which utterance did each verdict
-- actually score? A NULL utterance_id on a post-024 row means the binding
-- was lost and is worth a look.
create index if not exists idx_lily_answers_session_question
  on lily_answers (session_id, question_id);
