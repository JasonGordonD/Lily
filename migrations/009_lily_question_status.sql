-- 009_lily_question_status.sql — question lifecycle status (say-gate WO,
-- Dr. Tijoux consolidated directive, 2026-07-14).
--
-- Burn protocol: when the outbound leak filter fires while a question is
-- armed/prefetched (its answer may have been spoken on air), the agent
-- marks that bank row status='burned' (LILY_BURN log line) and pulls a
-- replacement through the existing bank/prefetch path. The bank fetcher
-- (lily_persistence.lily_fetch_bank_question) serves ONLY status='active'
-- rows.
--
-- SHARED COLUMN — future tier-retirement sub-agent: this same status
-- column is the designated home for tier retirement (e.g.
-- status='retired'); do NOT add a second lifecycle column. Any value
-- other than 'active' means "not servable".
--
-- SCOPE: the status column is GLOBAL — a burned question is out for every
-- group. Per-group burn scope rides lily_asked_history later (documented
-- limitation; the global burn is the safe over-approximation).

ALTER TABLE lily_questions ADD COLUMN IF NOT EXISTS status text DEFAULT 'active';
