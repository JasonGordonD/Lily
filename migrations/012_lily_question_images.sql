-- 012_lily_question_images.sql — picture-round image columns + attempt audit
-- (WO-LILY-OMNIBUS-002 sub-agents H/J).
--
-- lily_questions gains the image triplet already reserved by the question
-- schema (lily_reasoning._QUESTION_RESPONSE_SCHEMA). A bank row that
-- carries an image IS the cache — the CACHE-FIRST rule (lily_images.py):
-- any image need checks the row's image_url BEFORE generating or fetching.
--
--   image_url           public URL in the `lily-images` Supabase Storage
--                       bucket (content-addressed path {source}/{sha1}.{ext})
--   image_source        'generated' | 'web' | 'none' — provenance, matching
--                       the question-schema enum
--   image_license_note  free-text provenance/license note: web images carry
--                       the Exa source page + original image URL; generated
--                       images carry the model + prompt head

alter table lily_questions
  add column if not exists image_url text,
  add column if not exists image_source text default 'none',
  add column if not exists image_license_note text;

-- Sub-agent J — visible error rows (no-silent-crash), native port of the
-- JRVS media_gen_attempts stack where the rule originated: EVERY image
-- generation/fetch attempt — success OR failure — writes one row here;
-- rejection/error rows carry the actual provider message in
-- failure_reason. A failed image can never disappear silently.
create table if not exists lily_image_attempts (
  id bigint generated always as identity primary key,
  session_id text,
  question_id text,
  source text not null,          -- 'generated' | 'web'
  prompt text not null,          -- generation prompt / sourced entity
  status text not null,          -- 'success' | 'rejected' | 'error'
  failure_reason text,           -- actual provider/pipeline message
  model text,
  image_url text,
  created_at timestamptz not null default now()
);

create index if not exists lily_image_attempts_session_idx
  on lily_image_attempts (session_id, created_at desc);
