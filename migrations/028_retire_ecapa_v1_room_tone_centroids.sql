-- 028_retire_ecapa_v1_room_tone_centroids.sql — WO-LILY-VOICE-TRUTH-001 V1(d)
-- (renumbered from 027 by WO-LILY-COMPOSITION-FOLLOWUP-001 C6: it collided
-- with 027_lily_llm_usage_effort.sql; deploy.yml applies migrations/*.sql in
-- glob order, and two 027s made that order lexical luck).
--
-- WHAT THE BUILD DOES WITH THIS FILE: deploy.yml's "Apply migrations to
-- empty PostgreSQL" step DOES run it — against a fresh, EMPTY database,
-- where the guarded ALTERs are no-ops and the UPDATE matches zero rows
-- (harmless; the earlier header's "not applied by the build" claim was
-- wrong). The LIVE retirement stays operator-run: apply this by hand
-- against the production catalog after the ecapa-192-v2 build is up.
-- NO destructive writes: nothing is deleted, truncated, or overwritten —
-- the rows below are RETIRED (status='retired', retired_at=now()) and
-- stay in place as the receipt.
-- WHY. Every 'ecapa-192-v1' centroid was computed by the pre-VOICE-TRUTH
-- probe, which embedded the first 2.5s (match) / 8s (enroll) of WALL-CLOCK
-- audio from track_subscribed with no speech gate. In every instrumented
-- session that window closed 20+ seconds before any human spoke, so the
-- centroids encode ROOM TONE, not a voice: same-device sessions scored 0.99
-- against each other, 0/7 cross-device matches ever, and a probe carrying
-- zero seconds of the player scored 0.70 against his 26-sample centroid
-- (Auditor B, SQL + executed probes). The matcher now reads model_tag
-- 'ecapa-192-v2' only (lily_config.voice_identity_model_tag), so these rows
-- are never consulted again; retirement is the audit trail.
--
-- ROWS (SELECT id, group_id, sample_count, model_tag FROM lily_voice_identity
-- WHERE model_tag='ecapa-192-v1', read-only, 2026-09-06):
--   194ad75c-5b50-4832-8789-b0b8408f7bee  grp_0b07f989673dcf11e62da96343a39fd4006c1405  n=26  active
--   d3a5ce9c-3ad9-412f-83e0-7bbb915b4efb  lily-A8D30C-c9474149                          n=1   active
--   3806b687-618d-48a9-a422-3d4ef28e43cf  lily-1D27C8-974ff7ce                          n=1   active
--   a665f656-8326-47ee-9892-659913b8b441  grp_5863973edc6707da45b52e49857fecbebe4ce969  n=1   ALREADY retired (2026-08-09) — untouched
--
-- The status/retired_at columns already exist (021); the guarded ALTERs are
-- no-ops on the live schema and make the script safe on a fresh database.

alter table public.lily_voice_identity
  add column if not exists status text not null default 'active';
alter table public.lily_voice_identity
  add column if not exists retired_at timestamptz;

update public.lily_voice_identity
   set status     = 'retired',
       retired_at = now(),
       updated_at = now()
 where model_tag = 'ecapa-192-v1'
   and status = 'active'
   and id in (
     '194ad75c-5b50-4832-8789-b0b8408f7bee',
     'd3a5ce9c-3ad9-412f-83e0-7bbb915b4efb',
     '3806b687-618d-48a9-a422-3d4ef28e43cf'
   );

-- Verify (expected: 4 rows, all status='retired', retired_at not null):
-- select id, group_id, sample_count, model_tag, status, retired_at
--   from public.lily_voice_identity where model_tag = 'ecapa-192-v1';

-- ---------------------------------------------------------------------------
-- HOTFIX-ENGINE-LABEL-001 (2026-09-06 12:21 UTC) — lily_speaker_voiceprints
-- row 427 joins the retirement set. Same rule: RETIRE, do not delete; the
-- row stays in place as the receipt. Applied only on the operator's word.
--
-- WHY. Row 427 (group c6ee161e-edd6-4d56-a8d9-b758babba7cd, speaker_label
-- 'S1', player_name NULL, created 2026-08-14, identifiers rewritten
-- 2026-09-06 12:01:57Z) held
-- Speechmatics identifiers written under the engine's own diarization label
-- by the 12:01 session's enrollment path. Injected as a known speaker, it
-- failed StartRecognition schema validation ("speakers.0.label: Must not
-- validate the schema (not)") and three sessions ran deaf. The code guard
-- (lily_stt_tuning.lily_filter_enrollable_speakers: engine labels and
-- null player_name never injected) is the mechanical block; this
-- retirement is the audit trail. The columns do not exist on this table
-- yet, so the guarded ALTERs create them.

alter table public.lily_speaker_voiceprints
  add column if not exists status text not null default 'active';
alter table public.lily_speaker_voiceprints
  add column if not exists retired_at timestamptz;

update public.lily_speaker_voiceprints
   set status     = 'retired',
       retired_at = now(),
       updated_at = now()
 where id = 427
   and speaker_label = 'S1'
   and player_name is null
   and status = 'active';

-- Verify (expected: 1 row, status='retired', retired_at not null):
-- select id, group_id, speaker_label, player_name, status, retired_at
--   from public.lily_speaker_voiceprints where id = 427;
