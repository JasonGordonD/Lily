# LILY — Multi-Player Voice Trivia Host

Lily hosts live trivia nights for 2–6 people sharing one room and one microphone.
She knows who is speaking (real-time diarization), addresses players by name, scores
per player, adjudicates who answered first, generates her own questions on the fly,
and runs an opt-in, consent-gated 18+ mode. One agent, one prompt file plus one
additive adult layer, one game-state object.

Built from the PRMPT fleet's paid-for lessons: Lovebirds' production one-mic
diarization, name binding, voiceprint persistence, TTS wrapper and its bug fixes —
every lift lands as a native `lily_`-prefixed implementation (prmpt_common is
do-not-touch: no imports, no vendoring).

## Stack

| Layer | Choice |
|---|---|
| Framework | `livekit-agents==1.6.4` (plugin family pinned to match; JRVS is the fleet reference install) |
| STT | Speechmatics — `en`, diarization, ENHANCED, `speaker_sensitivity=0.5`, `prefer_current_speaker=True`, `max_speakers=7` (fixed at construction — no in-flight update path exists at 1.6.4), `ignore_speakers=["__ASSISTANT__"]` |
| Vocal LLM | `gemini-3.5-flash` — every spoken turn; explicit `safety_settings` (adult-product context), `thinking_config={"thinking_level": "low"}`, `max_output_tokens ≥ 600`, default sampling |
| Reasoning LLM | `gemini-3.1-pro-preview` — background node, own google-genai client (HTTP isolation): question prefetch (N+1) + verification at prefetch time; never speaks |
| TTS | ElevenLabs v3 via `lily_tts.py` (`/v1/text-to-speech/{voice_id}/stream`; the dialogue endpoint stays off per fleet revert). Voice: Raven's (env `LILY_VOICE_ID`, falls back to `RAVEN_VOICE_ID`) |
| VAD | Silero — barge-in enabled; STT is never gated during TTS |
| Persistence | Supabase (`lily_*` tables), fail-fast init, checkpoint on score change / 60s / key events |

## Architecture invariants

- **No generation gate, no trigger loop, no watchdog.** Lily speaks by default —
  silence is her failure mode (the deliberate inversion of Lovebirds' Raven).
- **The scorekeeper owns order; the LLM owns correctness.** First-committed-answer
  is a timestamp comparison in `lily_scorekeeper.py`, never an LLM judgment.
- **Answer window** opens on question TTS playout completion (SpeechHandle — 1.6.4
  has no dedicated playout event), runs a bounded duration; finals-only scoring, one
  candidate per player, segments outside the window are game-inert.
- **Two-tier adjudication:** Tier-1 conservative fuzzy/phonetic match against
  `acceptable_answers` (uncertainty escalates, never rejects); Tier-2 is one
  non-spoken LLM call that judges against the supplied canonical answer only — it
  never re-derives the fact.
- **Sticky commands enforced in code:** "skip" and "back to normal" (adult-mode
  revert) flip deterministic flags at the transcript-event layer; the prompt is
  texture, not the mechanism. The adult layer is additively injected/removed on the
  sticky `mode` flag — removal fully reverts her.
- **tts_node punctuation-flush guard is mandatory** — suspense holds ("The answer
  is…") deadlock the SegmentSynchronizer without it.
- **Honest failure:** prefetch/verification failures write a status note into the
  state block; Lily reports the breakage in character rather than confabulating.
- **Event-bound truth:** she never announces a score the scorekeeper hasn't
  committed, never claims the next question is ready unless prefetch landed.

## Latency discipline

Nothing blocking runs on the event loop's hot path, and slow calls are moved
off the reveal moment:

- **Checkpoints are threaded** (`asyncio.to_thread`) — the synchronous
  Supabase client on the event loop stalled the live audio pipeline a full
  cross-region round trip per score change before this.
- **Speculative Tier-2 judging:** ambiguous answers are judged in the
  background DURING the answer window (single-attempt calls, cached per
  candidate); the reveal consumes cached verdicts, so the judge round trip
  is off the reveal path. The batched at-reveal call survives only as a
  fallback when no speculation ran. Order stays timestamp-decided either way.
- **TTS connection pre-warm** at session start — the greeting's first
  synthesis skips the TCP+TLS handshake to ElevenLabs.
- **Rolling latency telemetry:** per-turn `llm_node_ttft` / `tts_node_ttfb` /
  `e2e_latency` averages ride the 60s heartbeat into
  `lily_sessions.metadata.pipeline_latency` (and the final row at close), so
  lag is a SQL query mid-game:
  `select session_id, updated_at, metadata->'pipeline_latency' from
  lily_sessions order by updated_at desc limit 10;`
- Known remaining baseline: ElevenLabs v3 sentence-chunked HTTP synthesis is
  the fleet standard (the low-latency model families don't support the v3
  audio tags her register depends on; the dialogue endpoint stays off per
  the fleet revert).

## Files

```
lily_agent.py        entrypoint + LilyAgent + LilyGame (windows, prefetch, publishing, tools)
lily_scorekeeper.py  N-player roster, per-player state, answer windows, system-directed
                     classifier, state-block builder — pure local state, zero LLM calls
lily_binding.py      lily_bind_speaker name extraction (2s fragment accumulation, stopwords)
lily_addressee.py    addressee-label corpus (B1) pure logic: clarify-reply parser,
                     seconds-into-window, implicit label derivation (stdlib-only)
lily_evaluation.py   Tier-1 matcher + Tier-2 judge contract
lily_reasoning.py    background node: prefetch + verification + judge transport
lily_persistence.py  Supabase: sessions, transcripts, answers audit, voiceprints, KB bank
lily_memory.py       persistent cross-session memory: session summaries, the
                     [RETURNING TABLE] block, KB-bank adult-mode guard (stdlib-only)
lily_tts.py          ElevenLabs v3 wrapper (lbs_tts lift; byte-alignment carry, 5K split)
lily_config.py       ALL env access lives here
prompts/lily_system.txt      the whole character (incl. <tts_guidelines>, <voice_output>)
prompts/layer_lily_adult.md  additive adult layer (register shift; group-directed only)
migrations/001_lily_schema.sql          six lily_ tables (no pgvector)
migrations/002_lily_questions_seed.sql  30 curated questions (demo insurance)
migrations/003_lily_memory.sql          lily_memories + lily_questions.adult guard column
migrations/004_lily_questions_expansion.sql  200 curated_v2 bank questions
migrations/005_lily_addressee_log.sql   addressee-label corpus (applied to production 2026-07-14)
migrations/006_lily_session_reports.sql lily_session_reports (write side; assessment filled later)
tests/               110 tests, run with plain `python -m pytest tests/` — no livekit, no network
```

## Persistent memory (rematch)

Lily remembers a table across sessions, keyed on a stable **group identity**
resolved at job start (logged as `LILY_MEMORY | GROUP_ID | source=...`):

1. `lily_group_id` from the first non-agent participant's token metadata —
   the frontend passes a device-scoped UUID as JSON
   `{"lily_group_id": "<uuid>"}` (absent/unparseable metadata is tolerated);
2. `LILY_GROUP_ID` env override;
3. room name (legacy fallback — random per session, so nothing re-keys on it).

**Tables:** `lily_memories` (one row per session, upserted idempotently on
`session_id` from both `finish_game` and the shutdown callback: final
`players [{name,score,streak}]`, `winner`, `question_count`, `highlights`
callouts, and a deterministic template `summary` — no LLM call) plus the
existing `lily_group_facts` (running-bit material) and
`lily_speaker_voiceprints` (which now re-key correctly across sessions for
instant returning-voice recognition).

**What Lily remembers:** on a returning group, the last 3 games + group facts
are compiled into a compact `[RETURNING TABLE]` system block (~600 chars max,
injected in `llm_node` the same additive way as the adult layer): returning
player names, who won last time, running bits, total games — so she greets
players back by name and does callbacks with rematch energy.

**Adult-column guard (consent-safety):** `lily_questions.adult` marks
adult-register bank rows; `lily_fetch_bank_question` takes the session `mode`
and hard-excludes `adult=true` rows unless `mode == 'adult'` — an adult
question can never surface at a general-mode table.

**Session reports:** one `lily_session_reports` row per session at close
(idempotent upsert on `session_id`): the in-memory transcript (the
scorekeeper's rolling buffer — never re-queried from the DB) plus `game_stats`
(final standings, rounds/questions played, per-player answers
attempted/correct, mode changes, callouts, `duration_s`); `report_status`
stays `'pending'` and `assessment` is filled later by the clinical desk,
never by agent code.

### Addressee-label corpus (B1)

The training-data flywheel for "was that an answer or table talk?":
`lily_addressee_log` gets one row per **finalized** segment while an answer
window is open, plus any finalized segment the agent acts on (scored,
skipped-on, clarified, system-directed). Every write is fire-and-forget via
`asyncio.to_thread` — zero hot-path cost. Each row carries the context a
future classifier needs: `phase`, `answer_window_open`,
`seconds_into_window` (off the scorekeeper's window `opened_at`),
`fuzzy_matched_answer` (Tier-1 verdict against the live question's
`acceptable_answers`; null with no live question), `system_directed_hit`
(the existing vocative-"Lily" classifier), and
`agent_action ∈ scored|ignored|clarified|adjudicated_other`.

Labels land three ways (`label_source`):

- **`implicit_scored_unappealed`** — at adjudication commit, the winning
  utterance and every scored-incorrect utterance get `label=host_directed`
  (an UPDATE on the row id kept at insert time, never a double insert).
- **`implicit_appealed`** — the documented hook for an appeal that re-enters
  Tier-2 and overturns an attribution: the corrected label replaces the
  implicit one (appeals are currently handled in-prompt; no code path yet).
- **`explicit_clarify`** — ground truth from the clarify moment: the
  `lily_log_clarify(player_name)` tool fires whenever Lily asks a player
  "is that your answer or are you thinking out loud?". It marks the player
  pending-clarify, emits the `clarify` `{name}` packet on `lily.events`
  (frontend pulses that player's chip), and logs the clarified utterance
  with `agent_action=clarified`. The player's NEXT finalized segment
  resolves it: affirmative ("yes", "that's my answer", "final answer") →
  `label=host_directed`; negative/thinking ("no", "just thinking",
  "talking to him") → `label=deliberation`; unparseable → `label=unknown`.
  The reply parser is pure and offline-tested (`lily_addressee.py`).

## Frontend seam (prmpt_ui `(lily)` route group)

- **Participant attributes** (LWW, per question beat): `phase`
  (`lobby|question|answering|reveal|scores|final`), `round`, `question_number`,
  `mode` (`general|adult`, sticky), `players` (JSON `[{name,score,streak,leader}]`),
  `answer_window` (JSON `{open,duration_ms,opened_at}`), `last_active_at`
  (epoch-seconds heartbeat).
- **Room metadata**: `{question, reveal:{answer,winner,correct}, wager}` via
  `ctx.api.room.update_room_metadata` (rtc has no room-metadata setter);
  `wager` drives the frontend's final-round palette shift.
- **`lily.events`** reliable packets (discriminator key `type`, matched to the
  shipped prmpt_ui parser — kind-name drift from the original contract note is
  deliberate): `player_bind` `{player:{name},name,speaker_label}`; `reveal`
  `{correct,winner}` keyed to TTS playback; `best_wrong_answer`
  `{player,answer}`; `biggest_comeback` `{player,detail}`; `finale`
  `{standings}` — fired at or before the `phase=final` flip; `clarify`
  `{name}` when Lily asks the "answer or thinking out loud?" question
  (2026-07-14 amendment — carries `clarify` on both discriminators).
- **RPCs registered**: `lily_control.start`, `lily_control.skip` (identical to the
  spoken "skip": no commentary, no spotlight — the adult-mode consent affordance).

## Environment

`LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` · `GOOGLE_API_KEY` ·
`ELEVEN_API_KEY` (never `ELEVENLABS_API_KEY`) · `LILY_VOICE_ID` (falls back to
`RAVEN_VOICE_ID`) · `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` · optional:
`LILY_KB_ONLY=1` (curated-bank-only question supply — the demo-day fallback),
`LILY_ANSWER_WINDOW_SECONDS`, `LILY_ROUNDS`, `LILY_QUESTIONS_PER_ROUND`,
`LILY_THINKING_BED_PATH`, `LILY_STINGER_CORRECT_PATH`, `LILY_STINGER_INCORRECT_PATH`,
`LILY_JOB_MEMORY_LIMIT_MB`. No secrets in this repo — configure via the deployment
secrets manager (`lk agent update-secrets` with an explicit `--id`; `--overwrite`
is banned in the shared project — LuvByrds is production tenancy).

**Dispatch name is exactly `Lily` (capital L)** in both `WorkerOptions(agent_name=...)`
and `livekit.toml` — a mismatch means rooms spin forever with no agent joining.

## Pre-demo smoke test

0. One full session watching worker memory (1.6.4 memory-monitor issue; limits set
   explicitly in WorkerOptions). 1. Dispatch targets `Lily` exactly. 2. Cold session:
   3 speakers bound conversationally in the lobby. 3. Collision: narrated order call
   matches reality. 4. "Lily, are you there?" during an open window must NOT score.
5. Funny wrong answer celebrated, not corrected flatly. 6. Suspense hold renders as a
   pause, not a stall. 7. Adult mode: five consecutive adult questions, all NON-EMPTY
   (empty = safety-settings regression); "back to normal" reverts instantly.
8. Reconnect: scores intact from checkpoint. 9. Second browser tab mid-game:
   scoreboard populates from state sync on arrival.
