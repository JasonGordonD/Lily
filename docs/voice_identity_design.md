# WO-LILY-VOICE-IDENTITY-001 — device-independent voice recognition

**Status:** core matcher landed (`lily_voice_identity.py`, tested); model
choice, audio-path extraction, DDL, and privacy/consent are open decisions
below. **Owner:** Lily backend + Doc (DDL) + operator (model / consent).

## The problem this fixes

Live 2026-08-06 19:40 session: a returning player said *"do you know who I
am… you should know my voice… I played with you before,"* and Lily could
not recognize them. Two root causes:

1. **Recognition is device-linked, not voice-first.** Cross-session memory
   keys on a `lily_group_id` UUID the browser stores in `localStorage`
   (`prmpt_ui/src/lib/lily/group-id.ts`) and passes in the join token. A
   new device / cleared storage / private window ⇒ new UUID ⇒ no link ⇒
   memoryless (`_resolve_initial_group_id` falls to the throwaway
   room-name).
2. **Speechmatics blobs can't bridge sessions.** The vendor refreshes its
   speaker-identifier strings every session (prod-verified: one voice,
   seven sessions, seven distinct strings — `lily_agent.py`
   `DEVICE_VERIFY_LABEL_MATCH`). So the native voiceprints only ever work
   as a *same-session* verification gate, never as a cross-session matcher.

Result: "know my voice" is impossible today by construction. Closing it
needs **our own durable speaker embedding**, matched by similarity,
independent of both the device link and the vendor's per-session blobs.

The immediate honesty stopgap already shipped (`RECOGNITION-HONESTY`): when
a returner-claim lands with a blank table card, Lily never denies or argues
— she names the gap honestly. This doc is the real recognition fix.

## Architecture

```
  audio  ──►  embedding model  ──►  probe embedding (D-dim float vector)
 (session)     (speaker verif.)              │
                                             ▼
                              lily_voice_identity.lily_match_voice
                                  probe × stored centroids (cosine + margin)
                                             │
                        ┌────────────────────┼─────────────────────┐
                        ▼                                          ▼
                  MATCH → group_id                          NO MATCH
             (feeds the EXISTING candidate                cold-start /
              quarantine: stage → verify →                ask who's playing;
              promote → load memory)                      enroll at close
```

Two seams already in the codebase make this a graft, not a rebuild:

- **The candidate-quarantine model** (`stage_device_candidate` →
  `verify_device_candidate` → `_promote_device_candidate`). A voice match
  becomes a new *candidate source* alongside the device link: stage the
  matched group's memory, and — because the match itself is the biometric
  proof — promote immediately (no vendor-label round-trip needed).
- **An existing audio tap.** The audEERING pipeline
  (`lily_audeering_consumers`) already captures rolling audio windows for
  the child-signal veto. The embedding extractor rides the same tap rather
  than adding a second audio subscription.

### Enrollment / match flow

1. **At session close**, compute an embedding from the session's captured
   speech per bound player and fold it into that person's stored centroid
   (`lily_update_centroid` — running mean, so more sessions sharpen it).
   Enroll only above a speech-duration floor (short utterances give noisy
   embeddings). RETIRE, never delete, on a forget request.
2. **At session start** (or first solid utterance), compute a probe
   embedding and call `lily_match_voice` against the candidate centroid set
   for this deployment. A confident match (absolute floor **and** margin
   over runner-up) stages+promotes that group's memory; otherwise cold
   start and let enrollment happen at close.

### Why the matcher is pure (already built)

`lily_voice_identity.py` is stdlib-only and model-agnostic: it takes float
vectors, L2-normalizes, scores cosine similarity, and gates on an absolute
threshold **plus** a runner-up margin (a false merge — greeting a stranger
by a housemate's name — is far costlier than a miss, which degrades to
"ask who's playing"). It is fully unit-tested now and survives whatever
model and storage backend get chosen. Everything below is what it does
*not* decide.

## Open decisions (need Doc / operator)

### 1. Embedding model — operator call

| Option | Dim | Notes |
|---|---|---|
| ECAPA-TDNN (SpeechBrain) | 192 | Strong accuracy; Torch dependency, ~20MB, CPU-runnable per-utterance. |
| pyannote/embedding | 512 | Good; heavier stack, HF-gated weights. |
| Resemblyzer (GE2E) | 256 | Light, pure-ish; lower accuracy in noise. |
| Vendor API (e.g. Speechmatics/other speaker-ID) | — | No local model, but recurring cost + another network dep on the audio path. |

Recommendation: **ECAPA-TDNN, computed post-session** (off the vocal path;
latency-insensitive), probe at session-start from the first ~5–8s of
speech. Confirm the dependency footprint is acceptable in the deploy image.

### 2. Where extraction runs

Post-session enrollment + start-of-session probe keeps the model **off the
real-time vocal path** entirely (no added spoken-turn latency). The
start-of-session probe is the only on-join cost and is bounded (one forward
pass on a few seconds of audio). Recommend this over per-utterance on-path
embedding.

### 3. DDL — for Doc

New durable identity table (pgvector). Draft for review:

```sql
-- Requires: create extension if not exists vector;
create table lily_voice_identity (
  id           uuid primary key default gen_random_uuid(),
  group_id     text not null,                 -- the durable person/group key
  centroid     vector(192) not null,          -- dim = chosen model's D
  sample_count integer not null default 1,    -- enrollments folded in
  model_tag    text not null,                 -- e.g. 'ecapa-192-v1' (dim/model provenance)
  status       text not null default 'active' -- active | retired (forget)
                 check (status in ('active','retired')),
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  retired_at   timestamptz
);
create index on lily_voice_identity (group_id);
create index on lily_voice_identity (status, model_tag);
-- ANN index once the pool is large enough to matter:
-- create index on lily_voice_identity using ivfflat (centroid vector_cosine_ops);
```

Matching can run either **in Postgres** (`order by centroid <=> $probe
limit k`, then apply the margin gate in `lily_match_voice`) or **in
Python** (load active centroids for the deployment, rank in-process). Start
in Python for correctness/testability; move top-k to pgvector when the pool
grows. `model_tag` gates the pool so a model swap never compares across
incompatible embedding spaces.

### 4. Privacy / consent — operator call (blocking)

Durable, cross-device voiceprints are biometric data. Before enrollment
ships, the operator must decide:

- **Consent surface.** A one-time "recognize me by voice next time?"
  opt-in, or on by default with an opt-out? The existing `forget_me`
  cascade must also retire the voice identity (add
  `lily_voice_identity.status='retired'` to `lily_forget`).
- **Retention** window and jurisdictional constraints (BIPA-class laws
  treat voiceprints as regulated biometrics).
- **Scope of the centroid pool.** Global vs per-tenant matching (a global
  pool raises both accuracy and privacy stakes).

This is the one genuinely blocking gate — recognition should not enroll
durable biometrics until consent is settled.

## What has shipped vs what remains

- ✅ **Matcher core** — `lily_voice_identity.py` (cosine + margin +
  running-mean centroid), 14 tests. Model-agnostic, pure.
- ⬜ Embedding model dependency + extractor on the audEERING tap (needs #1/#2).
- ⬜ `lily_voice_identity` table + persistence read/write (needs #3, Doc).
- ⬜ Match→candidate wiring in `entrypoint` / `stage_device_candidate`.
- ⬜ `forget_me` retires the voice identity (needs #4).
- ⬜ Consent surface in prmpt_ui + backend gate (needs #4, blocking).
