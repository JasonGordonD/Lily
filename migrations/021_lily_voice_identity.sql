-- 021_lily_voice_identity.sql — durable, device-independent voice
-- recognition (WO-LILY-VOICE-IDENTITY-001).
--
-- Closes the "you should know my voice" gap: cross-session memory is keyed
-- on a browser-stored group_id, and Speechmatics refreshes its speaker
-- blobs every session, so a returning voice on a new device / cleared
-- storage is unrecognizable today. This table stores OUR OWN durable
-- speaker embedding (a running-mean centroid of a person's enrolled unit
-- vectors) so the voice itself can be matched by cosine similarity,
-- independent of the device link and the vendor's per-session blobs.
--
-- Matching + centroid math: lily_voice_identity.py (cosine + runner-up
-- margin). Read/write: lily_persistence.lily_load/upsert/retire_voice_identity.
-- RETIRE (status='retired'), never delete — the forget_me cascade's
-- biometric arm; matching only ever reads status='active'.
--
-- Embedding space is pinned by model_tag (model + dim). Matching only
-- compares centroids sharing a tag, so a model swap starts a fresh pool
-- instead of comparing across incompatible spaces.
--
-- NOTE (dim): vector(192) matches the pinned ECAPA-TDNN model
-- (spkrec-ecapa-voxceleb, 192-dim). A different model needs a matching dim
-- here and a new model_tag.
--
-- Requires the pgvector extension (first pgvector use in this schema).

create extension if not exists vector;

create table if not exists lily_voice_identity (
  id            uuid primary key default gen_random_uuid(),
  group_id      text        not null,
  centroid      vector(192) not null,
  sample_count  integer     not null default 1,
  model_tag     text        not null default 'ecapa-192-v1',
  status        text        not null default 'active'
                  check (status in ('active', 'retired')),
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  retired_at    timestamptz,
  -- One active centroid per (person, embedding space): the code does a
  -- select-then-update-or-insert on this pair.
  unique (group_id, model_tag)
);

create index if not exists lily_voice_identity_group_idx
  on lily_voice_identity (group_id);
create index if not exists lily_voice_identity_active_idx
  on lily_voice_identity (status, model_tag);

-- ANN index — add once the active pool is large enough that a full scan of
-- one model_tag's centroids stops being trivial. Cosine ops match the
-- matcher's metric.
-- create index lily_voice_identity_centroid_idx
--   on lily_voice_identity using ivfflat (centroid vector_cosine_ops);

-- RLS: service-role writes only, consistent with the other lily_ tables.
-- (Enable + policy to be applied with the project's standard lily_ RLS.)
