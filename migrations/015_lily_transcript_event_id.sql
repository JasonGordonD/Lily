-- 015: idempotent transcript batch retries.
--
-- A network error can happen after Postgres committed a batch but before
-- the client received the response. event_id lets the agent safely retry
-- with an upsert instead of either dropping rows or creating duplicates.

alter table lily_transcripts
  add column if not exists event_id uuid;

create unique index if not exists lily_transcripts_event_id_uidx
  on lily_transcripts (event_id);
