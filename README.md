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
- **Outbound speech passes the say gate** (`lily_say_gate.py`): the designated
  choke point for everything Lily may say out loud — the P4 markdown/emoji
  strip (emphasis asterisks, backticks, headers, bullets and emoji never reach
  the synthesizer, while `[bracket]` audio tags — ElevenLabs v3 controls —
  pass through verbatim; emoji-only turns strip to empty and fall into the
  empty-candidate retry), plus the say-gate WO extensions: idempotent
  speech-act keys (double greeting / BUG-2 double question delivery), the
  state-block leak filter, need-to-know ambient context, and the burn
  protocol. See "The say gate" below.
- **Tool gating principle:** gate tools that mutate game outcomes or emit
  game events; leave observational/memory writes free. `lily_note_fact` is
  deliberately ungated — its primary habitat is the pre-game lobby, and a
  fact noted during phase confusion mutates nothing.
- **Reasoning-node calls are structured output** (P1): generation and
  verification set `response_mime_type="application/json"` **and** a
  `response_schema` (question schema carries current + reserved fields:
  `choices`, `image_url`/`image_source`, `proposed_category`), so the output is
  parsed with a plain `json.loads` — the regex/fence-stripping path
  (`lily_parse_question_json`) is retired to a defensive last resort.
- **Honest failure:** prefetch/verification failures write a status note into the
  state block; Lily reports the breakage in character rather than confabulating.
- **Event-bound truth:** she never announces a score the scorekeeper hasn't
  committed, never claims the next question is ready unless prefetch landed.

## Loop engagement (2026-07-14 persistence-audit root-cause fix)

The live audit found every session with `round=0` / `question_number=0`,
`lily_answers` empty, and scores committed only through `lily_award_bonus`:
**the deterministic pipeline (start_game → arm → ask → window → adjudicate)
never engaged** — `start_game` was RPC-only and Lily freestyled the quiz.
The pipeline now has four entry points, in order of preference:

1. **`lily_begin_round` function tool** — Lily calls this the moment the
   lobby has real energy (first genuine group laugh). This is the primary
   in-character way to flip out of the lobby; the prompt contract tells
   her the engine only runs through it.
2. **Deterministic spoken path** — "start the game" / "start the quiz" /
   "start the trivia" / "let's start" / "let's play" / "start round one"
   fire a fragment-proof `start_game` control command at the
   transcript-event layer (ignored once running).
3. **`lily_control.start` RPC** — the frontend "start" button. Kept for
   any UI that wants an explicit host-side gate.
4. **Auto-start safety net** — if ≥2 speakers are bound
   (`LILY_AUTO_START_MIN_PLAYERS`), the first question is prefetched, and
   the lobby grace window has elapsed
   (`LILY_AUTO_START_LOBBY_GRACE_SECONDS`, default 60s), the game starts
   automatically. This exists so a voice-only table that never calls the
   tool AND never touches the UI still reaches question one — the exact
   failure class that produced 15+ 2026-07-14 sessions with
   `lily_sessions.round=0` and `question_number=0` despite hours of
   audio and populated `final_standings`.

The question-spoken gate (which decides "did Lily just perform the armed
question, so open the answer window?") is now a tiered preference, not a
hard gate (`lily_evaluation.lily_question_spoken_ratio`, logged as
`LILY_WINDOW | OPEN | reason=... ratio=...`): ≥0.6 distinctive-token
overlap opens as `verbatim`, ≥0.3 as `paraphrase` (both with a floor of
two matched tokens so a single incidental word never opens it), and after
2 finished agent turns with a question armed in phase `question` the
window opens as `fallback_any_agent_speech` (warning-logged) — the
pipeline can never again stall on Lily's phrasing. The old 60% hard gate
blocked the window on her paraphrase habit and left the game running
through `lily_award_bonus` only. The prompt now also requires her to
perform the NEXT QUESTION word-for-word and never to invent questions
mid-game (the old "generating questions" section is scoped to the
broken-question-machine fallback only).

With the pipeline engaged, `lily_answers` rows (one per adjudicated
attempt, schema `(session_id, player_name, question_id, question_index,
transcript, verdict, eval_tier, awarded_points, ts)`) and real
`question_count` values in `lily_memories` (read straight off
`scorekeeper.question_number`) flow from the existing write paths.
Tests: `tests/test_round_loop.py`.

## The say gate (speech-boundary bug class, 2026-07-14 WO)

The live bug class — double greeting, double question delivery (BUG-2), a
state block read aloud, and an answer leak (the Bosporus answer spoken
BEFORE its question) — is closed by one gateway: `lily_say_gate.py`
(pure stdlib, offline-tested) plus its wiring in `lily_agent.py`.

### Idempotent speech acts

Game-critical speech acts carry state keys — `session_greet`,
`session_rejoin`, `q_{N}_delivery`, `q_{N}_reveal`, `round_{N}_scores`,
`finale` — in a per-session `SpeechActRegistry` on `LilyGame`. Keys are
claimed with an atomic check-and-set **at dispatch time** — never at
playback completion, which is exactly the race window that produced the
live duplicates. Lifecycle: **claim at dispatch → confirm at playout
completion → release on playback failure** (a claimed-but-never-played
act releases so a retry can redeliver — the 19:27:52 swallowed-delivery
class; a confirmed act stays done forever). All code-triggered speech
routes through `LilyGame.gated_say(key, act, instructions, source)`:
a first claim logs `LILY_SAY | act=... | key=... | source=...`, a second
claim logs `LILY_SAY_SUPPRESSED | reason=dup | key=...` and does NOT
speak. The session opener has two trigger paths (agent `on_enter` and the
entrypoint) — both dispatch the same instructions under `session_greet`,
so the loser of the race is silent; a reconnect uses its own
`session_rejoin` key and never trips `session_greet`. Keyless dispatches
(steal window, skip, mode reverts, the prefetch nudge, game start) still
log `LILY_SAY` for the audit trail.

### BUG-2: one authoritative question delivery

The `lily_begin_round` tool result carries the question payload (prompt +
category) and the post-tool turn is the SOLE deliverer — `start_game`
dispatches no racing instructed reply for the `host_tool` source, and the
prompt states the contract (tool-call turn: transition line only; the
next turn asks the question exactly once; the reveal repeats the answer,
never the question). Enforcement is physical: `tts_node` detects an
outbound question performance with the same verbatim detector that opens
the answer window (`lily_question_spoken_ratio ≥ 0.6`) and claims
`q_{N}_delivery`; a failed claim means question N was already delivered,
and the duplicate turn is replaced with silence (no retry — suppressed,
not swallowed).

### Leak filter

The injected state block rides as SYSTEM-role context wrapped in a
sentinel envelope (`<lily_state>…</lily_state>`). `tts_node` runs
`lily_filter_leaks` before the hygiene strip: whole envelopes, envelope
fragments (chunk-boundary partials like `<lily_state`), and any line
carrying a bracketed metadata marker (`[GAME STATE]`, `[room read:`,
`[env:`, `[RETURNING TABLE]`) are deterministically removed and logged as
`LILY_SAY_SUPPRESSED | reason=leak`. Ordinary `[bracket]` audio tags and
clean text pass untouched.

### Need-to-know ambient context

The ambient state block **never** carries `canonical_answer`,
`acceptable_answers`, `reveal_color`, or the full prefetched-question
JSON: the scorekeeper's `current_question` line is prompt-only, and the
`NEXT QUESTION` line carries prompt + category (+ `choices` when the
format has them — spoken content, not answer material). The answer
reaches the vocal node only through the reveal-time instructed reply, and
the Tier-2 judge receives it in its dedicated non-spoken call. The vocal
node cannot leak what it does not hold.

### Burn protocol

A leak-filter hit while a question is armed/prefetched means its answer
may have gone out on air: the question is burned — `LILY_BURN |
question_id=...`, bank rows (`kb_` ids) marked `lily_questions.status =
'burned'` (migration 009), generated questions discarded, the prompt
added to the session's avoid list — and a replacement is pulled through
the existing bank/prefetch path. The bank fetcher serves only
`status='active'` rows (a missing column reads as active). The status
column is SHARED with the future tier-retirement sub-agent (e.g.
`status='retired'`); scope is global today — per-group burn rides
`lily_asked_history` later.

### Callout gating

Every tool that emits game-event packets on a live round carries the same
`game_started=True` gate as `lily_award_bonus`, refusing with an
LLM-readable recovery path ("call lily_begin_round first"), never a
silent no-op: `lily_award_bonus` and `lily_log_clarify`.
`lily_bind_speaker`'s `player_bind` packet is a roster event, not a game
outcome — binding is core lobby behavior and stays ungated, per the tool
gating principle above (as does `lily_note_fact`, deliberately).

Tests: `tests/test_say_gate.py` (registry + leak filter, pure) and
`tests/test_say_gate_dispatch.py` (dispatch dedupe, BUG-2 contract,
need-to-know, burn, clarify gate).

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
- **Preemptive generation repair (P2):** the state-block/adult-layer/memory
  injections moved from `llm_node` into `on_user_turn_completed`, applied to
  both the turn context and the persistent context with stable item ids and
  rewrite-only-on-change — so 1.6.4's equivalence check validates the
  preemptive LLM run whenever game state held still across the turn boundary
  (previously every run was discarded: 13 "chat context changed" warnings per
  session, double LLM cost). `llm_node` keeps one documented injection for
  instruction-driven turns (reveal/skip/steal/start — they bypass the hook and
  must see the just-armed question); those turns also pause preemptive
  generation (`LilyGame.instructed_reply`) instead of paying for runs that are
  dead by construction, resuming on playout completion.
- **Dedicated reasoning-node token budget (P1 truncation root cause):** on
  Gemini 3.x thinking tokens count toward `max_output_tokens`; the reasoning
  node shared the vocal node's 800-token budget and 3.1-pro's medium thinking
  starved the question JSON into mid-object truncation. Generation and
  verification now run on `LILY_REASONING_MAX_OUTPUT_TOKENS` (default 4096 —
  prefetch is off the hot path), the Tier-2 judge on
  `LILY_JUDGE_MAX_OUTPUT_TOKENS` (default 1024 — latency-relevant, small
  verdict).
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
lily_bank.py         bank curation: banking-on-generation with near-dup detection,
                     per-group asked history, gated category proposals (stdlib-only
                     pure logic + thin Supabase I/O)
lily_bank_tuning.py  difficulty self-tuning + retirement decisions (pure) and the
                     session-end tuning run (thin DB I/O)
lily_say_gate.py     outbound-speech gate: markdown/emoji strip ([tag]-preserving),
                     SpeechActRegistry (idempotent speech acts), state-block leak
                     filter + sentinel envelope (the designated choke point; stdlib-only)
lily_tts.py          ElevenLabs v3 wrapper (lbs_tts lift; byte-alignment carry, 5K split)
lily_config.py       ALL env access lives here
lily_audeering_client.py     devAIce Web API client + room-audio capture pipeline
                             (native lift of mjrvs_audeering_client + pipeline)
lily_audeering_consumers.py  gates, room-read banding, child-signal ladder, scene,
                             rubric loader + import-time zero-scalar lint, state store
prompts/lily_system.txt      the whole character (incl. <tts_guidelines>, <voice_output>)
prompts/layer_lily_adult.md  additive adult layer (register shift; group-directed only)
prompts/lily_room_read_rubric.txt  room-read rubric (loader-appended; zero-scalar linted)
migrations/001_lily_schema.sql          six lily_ tables (no pgvector)
migrations/002_lily_questions_seed.sql  30 curated questions (demo insurance)
migrations/003_lily_memory.sql          lily_memories + lily_questions.adult guard column
migrations/004_lily_questions_expansion.sql  200 curated_v2 bank questions
migrations/005_lily_addressee_log.sql   addressee-label corpus (applied to production 2026-07-14)
migrations/006_lily_session_reports.sql lily_session_reports (write side; assessment filled later)
migrations/007_lily_memories_player_names.sql  lily_memories.player_names audit column (+GIN index)
migrations/008_lily_acoustic_trajectories.sql  per-turn acoustic snapshots + addressee
                                               acoustic_snapshot column
migrations/009_lily_question_status.sql  lily_questions.status lifecycle column
                                         (burn protocol; shared with tier retirement)
migrations/010_lily_asked_history.sql    per-group served-question ledger (no-repeat
                                         guard + tuning exposure floor)
migrations/011_lily_category_candidates.sql  gated category proposals tally
tests/               349 tests, run with `python -m pytest tests/` — no network; needs
                     livekit-agents 1.6.4 + google-genai installed
                     (test_award_gate.py / test_context_blocks.py /
                     test_say_gate_dispatch.py import livekit)
```

## Persistent memory (rematch)

Lily remembers a table across sessions, keyed on a stable **group identity**.
The resolution chain (every step observable — `LILY_MEMORY | GROUP_ID |
source=... group_id=...` is logged every session, upgrades as
`LILY_MEMORY | GROUP_ID_UPGRADE | source=... old=... new=...`):

1. **(a) `lily_group_id` metadata** — strongest signal. Read from BOTH the
   dispatch/job metadata (`ctx.job.metadata`; the token route mirrors
   participant metadata into `RoomAgentDispatch`, available immediately) and
   the first non-agent participant's token metadata (JSON
   `{"lily_group_id": "<uuid>"}`). Live evidence showed the participant is
   often NOT in `remote_participants` yet when `ctx.connect()` returns, so
   the scan polls up to 3s for the first participant, and a
   `participant_connected` hook upgrades a weak id if a metadata-carrying
   participant joins later (`source=participant_metadata_late`).
2. **(b) voiceprint match** (at game start, roster stabilized): this
   session's `stt.get_speaker_ids()` identifiers are matched by exact string
   overlap against `lily_speaker_voiceprints` rows loaded by roster
   player-name — a hit reuses that prior `group_id`
   (`source=voiceprint_match`). Best-effort: it only hits when Speechmatics
   returns stable identifier strings for a returning voice.
3. **(c) name-set hash fallback**: `grp_` + sha1 of the normalized sorted
   player-name set joined with `|` (e.g. `sha1("carly|kali|rami")`) —
   deterministic, so the same table of names re-keys to the same group every
   night (`source=name_set_hash`). This resolves only after names bind, so
   it runs as a **mid-session upgrade at game start**: the session begins
   under the room name, re-resolves when `start_game` fires, re-keys this
   session's rows already written (`lily_sessions`, this session's
   `lily_group_facts`, and — only when upgrading off the room-random id —
   `lily_speaker_voiceprints`; `LILY_MEMORY | REKEY | table=...`), reloads
   the `[RETURNING TABLE]` memory if no questions have been played, and
   re-fires enrollment so voiceprints land under the resolved id.
4. **(d)** `LILY_GROUP_ID` env override, then room name (random per session
   — nothing re-keys on it; the upgrade path exists exactly to escape it).

**Tables:** `lily_memories` (one row per session, upserted idempotently on
`session_id` from both `finish_game` and the shutdown callback: final
`players [{name,score,streak}]`, `winner`, `question_count` — read straight
off `scorekeeper.question_number` — `highlights` callouts, a deterministic
template `summary`, and `player_names`, the normalized sorted name-set audit
column from migration 007; if production hasn't applied 007 yet the write
retries once without the column) plus `lily_group_facts` (running-bit
material) and `lily_speaker_voiceprints`.

**Group facts now have writers:** the `lily_note_fact(player_name, fact)`
tool (the prompt tells Lily to log each player's lobby fact and any
callback-worthy detail) and `best_wrong_answer` callouts both persist to
`lily_group_facts` under the RESOLVED group id (deduped per session,
fire-and-forget, `LILY_MEMORY | GROUP_FACT`); `lily_load_group_memory`
already reads them back into the `[RETURNING TABLE]` block on the next
night.

**Voiceprint enrollment** (`lily_speaker_voiceprints`) fires the moment the
FIRST binding commits, again at game start / group-id upgrade, and once more
— awaited, inside the shutdown gate, so teardown can't race it — at session
close for late binders. The upsert is idempotent on
`(group_id, speaker_label)`, and the group id is read at write time (a
callable), so a mid-session upgrade re-keys in-flight enrollments too.
No-silent-crash: every failure path logs a structured
`LILY_ENROLL | FAILED | reason=...` (no client, plugin API drift, no
identifiers yet, no usable rows, exception); success logs
`LILY_ENROLL | OK | trigger=... group=... speakers=...`.
**1.6.4 API notes:** `stt.get_speaker_ids()` is **async** at
`livekit-plugins-speechmatics 1.6.4` and returns useful identifiers only
after ~5 spoken words per speaker (the multi-trigger schedule exists
precisely so an early empty result self-heals); the code tolerates a future
plugin making it synchronous, and logs `reason=no_get_speaker_ids_api` if a
bump removes it. On a rematch, injected `known_speakers` carry player-name
labels, so returning voices may surface with the player name as the label —
the enrollment row mapper handles both spellings.

**What Lily remembers:** on a returning group, the last 3 games + group facts
are compiled into a compact `[RETURNING TABLE]` system block (~600 chars max,
injected in `llm_node` the same additive way as the adult layer): returning
player names, who won last time, running bits, total games — so she greets
players back by name and does callbacks with rematch energy. A block that
arrives via the mid-session group-id upgrade injects on her next turn the
same way. **Neutral-greeting rule:** with no memory data she is instructed
never to claim she remembers the table AND never to announce it's their
first time — history is referenced only when a `[RETURNING TABLE]` block
actually exists.

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

## Bank curation loop (WO-LILY-OMNIBUS-002 D/E/F)

The curated bank (`lily_questions`) is a living asset: it grows from
generation, forgets nothing per group, tunes its own difficulty labels, and
graduates player-demanded categories — all observable via `LILY_BANK |` and
`LILY_TUNE |` log markers.

### Banking-on-generation + dedup (D, `lily_bank.py`)

Every generated question that passes verification is inserted into
`lily_questions` (`source='generated'`, `status='active'`, `adult` set from
session mode) — this is the path by which the bank self-grows. Near-dup
detection runs at EVERY bank insert:

- **exact**: identical normalized-text hash (lowercase,
  punctuation/whitespace-stripped, sha1) against any existing row, any
  category;
- **fuzzy**: `difflib` ratio >= 0.87 on normalized texts, same category only.

Dups are discarded and logged `LILY_BANK | DUP_DISCARDED`, never inserted.

### Per-group asked history (D, migration 010)

`lily_asked_history` gets one row per question SERVED (armed for delivery):
resolved `group_id`, `question_id`, normalized-text hash, `session_id`.
Loaded at session start (and reloaded on a group-id upgrade; a rekey moves
this session's rows), it drives the no-repeat guard:

- bank draws exclude the group's served `kb_` ids and text hashes
  (`lily_fetch_bank_question` `exclude_ids`/`exclude_hashes`);
- generated output is hash-checked against the history — a cross-session
  repeat is discarded (`LILY_BANK | HISTORY_REPEAT_DISCARDED`) and the bank
  fallback serves instead (the generator's textual avoid-list only carries
  this session's prompts).

### Difficulty self-tuning + retirement (E, `lily_bank_tuning.py`)

Session-end job, fire-and-forget (never blocks or fails the shutdown gate).
Aggregates `lily_answers` per bank question across ALL sessions, with an
exposure floor of **5 servings** counted from `lily_asked_history`. Per
question, ONE move per run:

- success > 75% → `difficulty_tier` down one (min 1)
- success < 30% → `difficulty_tier` up one (max 4)
- success < 10% or > 95% → `status='retired'` (outranks a tier move; rides
  the shared migration-009 status column, so retired rows are unservable)

Decisions are pure functions (offline-testable decision table); applied
moves log `LILY_TUNE | TIER_DOWN/TIER_UP/RETIRE | ...`.

### Gated category proposals (F, migration 011)

Generation may return `proposed_category` (reserved field in the question
schema). Each proposal upserts `lily_category_candidates` (use_count +
distinct proposing groups), but the question SERVES under its round FAMILY
until the candidate is **promoted: use_count >= 10 AND >= 3 distinct
groups**. Promoted extras appear as one lobby state-block line; Lily never
announces unpromoted categories.

## Acoustic pipeline (Audeering devAIce)

Lily reads the ROOM, not the transcript: rolling ≥5s windows of room audio
are uploaded to the audEERING devAIce Web API (v4.9.0) and consumed as
natural-language hosting signals plus one safety-critical action surface.
Native lift of the JRVS canary (`mjrvs_audeering_client.py` /
`mjrvs_audeering_consumers.py`, WO-JRVS-AUDEERING-MODULES-D-001) — Lily
files are `lily_audeering_client.py` (HTTP client + capture pipeline) and
`lily_audeering_consumers.py` (gates, banding, ladder, rubric, state store).

**Modules requested** (exact upload config; billing is audio-seconds — JRVS
Probe-C confirmed per-second, not per-module, so this full set is 1× quota):

```json
{"expression": {"expressionModel": "large"}, "prosody": {}, "audioQuality": {},
 "aed": {}, "scene": {"outputSubScene": true},
 "speakerAttributes": {"speakerAttributesModel": "large"}}
```

`asr` and `speakerVerification` are EXCLUDED. `speakerAttributesModel`
"large" is MANDATORY (the small model returns null child scores on short
segments and would starve the safety ladder). The capture window is floored
at 5s in `lily_config` — the scene model is optimized for >5s windows, one
classification per window (no continuous sub-windowing).

**Capture wiring (the Tijoux lesson):** the `track_subscribed` handler is
registered AFTER `session.start()` returns (handlers registered earlier fire
into a half-initialized session), and a safety-net scan attaches
already-subscribed tracks that landed during the start window. Multi-mic
tables attach every non-agent audio track.

**D-cross rules (JRVS, carried verbatim):**

1. **Zero scalars in the prompt.** The LLM never sees `arousal=0.71` — only
   `[room read: agitated / on edge]`. The rubric
   (`prompts/lily_room_read_rubric.txt`, appended to `lily_system.txt` at
   loader level) is a PHRASE LIST ONLY, and an import-time lint in
   `lily_audeering_consumers` raises on ANY digit in it — a scalar in the
   rubric fails the container at boot.
2. **Quality gate first.** `audioQuality.snr < AUDEERING_MIN_SNR_DB`
   (default 12) suppresses all affect lines for that window. Doc caveat
   encoded in the gate: the SNR model is strong on background noise
   (CCC 0.94) and weak on speech-distortion (CCC 0.38) — it is a
   background-noise gate only; do not tighten it chasing distortion.
   Transit scenes loosen the bar by `AUDEERING_SNR_TRANSIT_ADJUST`
   (default −2; the JRVS D2b scene→gate pattern).
3. **AED gate:** music co-active with speech suppresses affect reads
   (party rooms — singing must never read as an emotional event). AED
   thresholds ride the doc defaults (applied server-side).
4. **Baseline-relative reads / smoothing on descriptors ONLY.** The
   room-level AVD (`expression.dimension`, −1..1; `category` scores are NOT
   consumed) is smoothed over `AUDEERING_AVD_SMOOTH_WINDOW` (default 4)
   bounded deques, banded into descriptors, injected as
   `[room read: ...]` lines in the `[GAME STATE]` block. All axes inside
   `AUDEERING_AVD_NEUTRAL_BAND` → inject NOTHING.
5. **Safety triggers run OUTSIDE the smoother.** The child ladder keeps its
   own per-segment streak counters — widening the smooth window cannot
   delay it (regression-pinned in `tests/test_audeering.py`).
6. **Never block a turn.** Uploads are fire-and-forget; the agent reads the
   latest state synchronously in `build_state_block`.
7. **Consumer exceptions never stop raw-signal recording.**
8. **Circuit breaker on missing `AUDEERING_API_KEY`** — best-effort: one
   structured `LILY_AUDEERING_BREAKER` log line, uploads disabled, the
   session runs unaffected.

**Room read → hosting moves** (rubric mapping): "flat / low energy" →
easier question + spotlight move; "hot / riding high" → tighten and ride
it; "valence sagging" (on a hard-question streak) → drop a gimme;
"agitated / on edge" → cool it down. Scene sub-labels map to hosting
calibration: small indoor → living-room intimacy, large indoor → bar
energy, transport → shorter questions + more repetition tolerance — as a
low-priority `[env: ...]` line, at most once per state-block refresh.

**Child-signal ladder → adult-mode veto (SAFETY-CRITICAL; ships with the
pipeline, never after).** Signal: `speakerAttributes.gender.child`
(schema `{gender: {female, male, child}` summing to 1, `age: number|null}`),
one result per VAD speech segment — a young voice produces its own
segment-level scores, and the sustained-N streak counts segments.
Tiers: HIGH ≥ `AUDEERING_CHILD_HALT_THRESHOLD_HIGH` (0.85) sustained N=2;
BORDERLINE ≥ `AUDEERING_CHILD_HALT_THRESHOLD_BORDERLINE` (0.5) sustained
N=2. HIGH segments also advance the borderline streak (JRVS ladder fix —
oscillation around the high threshold cannot evade both tiers).
**Null-safe:** a null child score (too-short segment) neither advances NOR
resets a streak. The ladder runs BEFORE the quality/music gates — a child
on noisy audio or a child singing is never a blind spot. Enable flags
default ON for Lily (`AUDEERING_CHILD_HALT_ENABLED`,
`AUDEERING_CHILD_STEP_UP_ENABLED`; JRVS shipped them false pending an
action surface — Lily has one).

Actions (veto-only, BOTH tiers):

- **Adult mode ACTIVE + ladder trips** → exit through the SAME sticky-flag
  path as the spoken "back to normal": `sk.set_mode("general")` +
  attribute publish + deterministic revert flow (`LilyGame.on_child_signal`,
  wired as the acoustic state's `on_child_signal` callback). Instant,
  in-character line WITHOUT explaining the mechanism, general category next.
- **Adult mode REQUESTED while tripped** → the `lily_enter_adult_mode` tool
  checks `acoustic.child_veto_active()` and refuses, telling Lily to keep
  the general deck with a light in-character deflection.

Framing, stamped doc-verbatim at every emit site
(`lily_audeering_consumers.PERCEIVED_FRAMING`): the module estimates "how
the speaker sounds, not necessarily the actual attributes of the speaker";
age MAE is ±8.46yr — the signal can EXIT or BLOCK adult mode, NEVER
authorize it; whole-room verbal consensus remains necessary and is no
longer sufficient. Age/gender are otherwise telemetry-only, stamped
`PERCEIVED_NOT_VERIFIED`, never spoken, never in the prompt.

**Persistence (migration 008):** one `lily_acoustic_trajectories` row per
finalized user turn (fire-and-forget, `to_thread`) carrying the latest
capture's `category` / `dimension` / `prosody` / `features` jsonb (index on
`session_id, turn_index`; drt_acoustic_trajectories clone). Every
`lily_addressee_log` row now also carries `acoustic_snapshot` — non-null
jsonb when the pipeline is healthy, an EXPLICIT SQL null (key always set,
never absent) when the breaker is open.

**Env** (all via `lily_config`; also threaded through
`.github/workflows/deploy.yml` — key from secrets, tunables from vars):
`AUDEERING_API_KEY`, `AUDEERING_MAX_UPLOADS_PER_SESSION` (default 240 —
20 min billable ceiling), `AUDEERING_WINDOW_SECONDS_F` (default 5, floored
at 5), `AUDEERING_CAPTURE_INTERVAL_SECONDS` (5),
`AUDEERING_MIN_SNR_DB` (12), `AUDEERING_SNR_TRANSIT_ADJUST` (−2),
`AUDEERING_AVD_SMOOTH_WINDOW` (4), `AUDEERING_AVD_NEUTRAL_BAND` (0.15),
`AUDEERING_CHILD_HALT_THRESHOLD_HIGH` (0.85),
`AUDEERING_CHILD_HALT_THRESHOLD_BORDERLINE` (0.5),
`AUDEERING_CHILD_HALT_SUSTAINED_N` (2), `AUDEERING_CHILD_HALT_ENABLED`
(true), `AUDEERING_CHILD_STEP_UP_ENABLED` (true).

Out of scope (deliberate): per-speaker AVD attribution, the `asr` module,
speaker verification, Hume, any gender-conditional behavior.

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
`LILY_AUTO_START_MIN_PLAYERS` / `LILY_AUTO_START_LOBBY_GRACE_SECONDS`
(lobby auto-start safety net), `LILY_GROUP_ID` (group-identity override),
`LILY_THINKING_BED_PATH`, `LILY_STINGER_CORRECT_PATH`, `LILY_STINGER_INCORRECT_PATH`,
`LILY_JOB_MEMORY_LIMIT_MB`, `LILY_REASONING_MAX_OUTPUT_TOKENS` (default 4096) /
`LILY_JUDGE_MAX_OUTPUT_TOKENS` (default 1024) — dedicated reasoning/judge budgets
(thinking tokens count toward `max_output_tokens` on Gemini 3.x) · acoustic
pipeline: `AUDEERING_API_KEY` plus the `AUDEERING_*` tunables listed in the
acoustic-pipeline section (missing key = breaker open, session unaffected).
No secrets in this repo — configure via the deployment
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
